"""
memory/vector_memory.py —— ChromaDB 向量记忆与文档检索(RAG 底座, v1.6.0 接入主流程)

能力(两类记忆, 共享一个向量集合, 用 metadata.kind 区分):
    - 任务级长期记忆(kind=run): 每次调研完成后 save_run(query, materials, report)
      保存一份"主题 + 素材要点 + 报告"的完整记录; 新任务开始时按 user_query 检索
      相似历史调研(kind=run), 把历史素材注入本轮, 减少重复搜索 —— 长期记忆;
    - 上传文档分块检索(kind=doc_chunk): save_document 把 PDF/CSV 全文按块
      (chunk_size / overlap) 向量化入库; 新任务开始时检索与主题最相关的文档块
      注入素材 —— 最小 RAG 链路: 切分 → 向量化 → 检索 → 增强生成。

容错降级:
    - chromadb 未安装 / 初始化失败时, get_memory_store() 返回 None, 调用方
      (graph_builder / main) 自动降级为"无记忆模式", 不阻断主流程;
    - 所有写操作带线程锁(Streamlit 多会话/后台线程可安全调用)。

对外函数:
    get_memory_store(persist_directory=CHROMA_DIR) -> MemoryStore | None
        进程级单例(线程安全; 首次初始化失败后本进程不再重试, 返回 None)
    chunk_text(text, chunk_size=800, overlap=100) -> list[str]
        纯函数文本分块(可独立单测)

工程边界备注:
    - 存储目录默认项目根目录 chroma_db/(已 .gitignore), 不入库、不打 Docker 镜像;
    - 单条向量化文本上限 MAX_DOC_LEN 字符, 超长截断(检索用相似度, 截断不影响主题命中);
    - ChromaDB 为本地原型级存储, 未做多用户隔离(与项目"单用户本地原型"定位一致)。
"""
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

_logger = logging.getLogger("memory.vector_memory")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_DIR = os.path.join(BASE_DIR, "chroma_db")  # 向量库持久化目录(不入库)

COLLECTION_NAME = "research_memory"
DEFAULT_CHUNK_SIZE = 800        # 单块字符数(中文约 1 字符/token, 对应约 800 token)
DEFAULT_CHUNK_OVERLAP = 100     # 块间重叠字符数(保留上下文衔接)
MAX_DOC_LEN = 30000             # 单条向量化文本上限(防超长记录)
KIND_RUN = "run"                # 任务级记忆
KIND_DOC_CHUNK = "doc_chunk"    # 上传文档分块


# ============================ 纯函数: 文本分块 ============================
def chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[str]:
    """把长文本按字符切成若干块(块间 overlap 保留上下文衔接)。

    - 空文本 → []; 文本长度 ≤ chunk_size → 原样单块;
    - 优先在换行符处断开(减少切碎语义), 无换行则按 chunk_size 硬切;
    - 保证 start 单调前进(防死循环), 单块至少前进 chunk_size // 2。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: List[str] = []
    start = 0
    text_len = len(text)
    half = max(1, chunk_size // 2)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        if end >= text_len:
            chunks.append(text[start:])
            break
        # 在块后半段找最后一个换行符作为断点(整块无换行时硬切)
        cut = text.rfind("\n", start + half, end)
        if cut < start + half:
            cut = end
        else:
            cut += 1  # 断点含换行符, 下一块从换行后开始
        chunks.append(text[start:cut])
        next_start = cut - overlap
        if next_start <= start:  # 重叠过长时至少前进到断点, 保证单调
            next_start = cut
        start = next_start
    return chunks


# ============================ 向量记忆存储 ============================
class MemoryStore:
    """历史调研任务素材 + 上传文档分块的向量记忆库(ChromaDB 本地持久化)。

    :param persist_directory: 向量库目录(传 None 使用纯内存 EphemeralClient, 测试/临时用);
                              默认项目根目录 chroma_db/
    :param collection_name:   集合名
    :param embedding_function: 可选的自定义 embedding 函数(默认 None = ChromaDB 内置模型);
                              测试可传轻量确定性函数避免模型下载
    """

    def __init__(self, persist_directory: str | None = CHROMA_DIR,
                 collection_name: str = COLLECTION_NAME,
                 embedding_function: Any = None) -> None:
        import chromadb  # 延迟导入: 未启用记忆时无需加载(失败由 get_memory_store 兜底)

        if persist_directory is None:
            self._client = chromadb.EphemeralClient()  # 纯内存模式(测试/临时用)
        else:
            os.makedirs(persist_directory, exist_ok=True)
            self._client = chromadb.PersistentClient(path=persist_directory)
        kwargs: Dict[str, Any] = {"metadata": {"hnsw:space": "cosine"}}  # 余弦相似度
        if embedding_function is not None:
            kwargs["embedding_function"] = embedding_function
        self._collection = self._client.get_or_create_collection(
            name=collection_name, **kwargs)
        self._lock = threading.Lock()

    # ---------------- 写入 ----------------
    def save_run(self, query: str, materials: list, report: str) -> str:
        """保存一次完整调研为任务级记忆(供未来相似主题复用)。

        :return: 新记录 id
        """
        doc_body = "\n\n".join(str(m)[:2000] for m in (materials or []))
        doc = (f"【调研主题】{query}\n【素材要点】\n{doc_body}\n【最终报告】\n{report[:8000]}")
        doc_id = uuid.uuid4().hex
        with self._lock:
            self._collection.upsert(
                ids=[doc_id],
                documents=[doc[:MAX_DOC_LEN]],
                metadatas=[{
                    "kind": KIND_RUN,
                    "query": str(query)[:500],
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                }],
            )
        return doc_id

    def save_document(self, source_name: str, text: str,
                      chunk_size: int = DEFAULT_CHUNK_SIZE,
                      overlap: int = DEFAULT_CHUNK_OVERLAP) -> int:
        """把文档全文分块向量化入库(RAG 底座)。

        :param source_name: 来源标识(如文件名), 会写入每条 chunk 的 metadata
        :return: 实际入库的块数(空文本返回 0)
        """
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            return 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        ids: List[str] = []
        docs: List[str] = []
        metas: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            ids.append(uuid.uuid4().hex)
            docs.append(chunk[:MAX_DOC_LEN])
            metas.append({
                "kind": KIND_DOC_CHUNK,
                "source_name": str(source_name)[:200],
                "chunk_index": i,
                "time": now,
            })
        with self._lock:
            self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
        return len(chunks)

    # ---------------- 检索 ----------------
    def search(self, query: str, top_k: int = 3, kind: Optional[str] = None) -> list:
        """检索与 query 最相似的历史记录。

        :param kind: 只检索该类型(kind=run 历史任务记忆 / doc_chunk 文档片段);
                     None = 全库检索
        :return: [{"id", "content", "kind", "query"|"source_name", "score"}, ...]
        """
        total = self._collection.count()
        if total == 0:
            return []
        kwargs: Dict[str, Any] = {
            "query_texts": [str(query)],
            "n_results": min(top_k, total),
        }
        if kind:
            kwargs["where"] = {"kind": kind}
        result = self._collection.query(**kwargs)
        items = []
        for idx, doc_id in enumerate((result.get("ids") or [[]])[0]):
            docs = (result.get("documents") or [[]])[0]
            metas = (result.get("metadatas") or [[]])[0]
            dists = (result.get("distances") or [[]])[0]
            meta = metas[idx] if idx < len(metas) else {}
            item: Dict[str, Any] = {
                "id": doc_id,
                "content": docs[idx] if idx < len(docs) else "",
                "kind": (meta or {}).get("kind"),
                "score": round(float(dists[idx]), 4) if idx < len(dists) else None,
            }
            if (meta or {}).get("query"):
                item["query"] = meta.get("query")
            if (meta or {}).get("source_name"):
                item["source_name"] = meta.get("source_name")
            items.append(item)
        return items

    # ---------------- 管理 ----------------
    def count(self) -> int:
        """当前集合记录总数。"""
        try:
            return self._collection.count()
        except Exception:  # noqa: BLE001 —— 读库失败按 0 处理, 不阻断
            return 0

    def clear_all(self) -> int:
        """清空全部记忆记录, 返回删除条数。"""
        total = self.count()
        if total:
            with self._lock:
                try:
                    ids = self._collection.get()["ids"]
                    if ids:
                        self._collection.delete(ids=ids)
                except Exception:  # noqa: BLE001 —— 删除失败按 0 处理
                    return 0
        return total


# ============================ 进程级单例(容错降级) ============================
_memory_store: Optional[MemoryStore] = None
_memory_store_error: Optional[str] = None
_memory_store_lock = threading.Lock()


def get_memory_store(persist_directory: str = CHROMA_DIR,
                     embedding_function: Any = None) -> Optional[MemoryStore]:
    """进程级单例获取向量记忆库; 初始化失败返回 None(调用方降级为无记忆模式)。

    首次初始化失败后本进程不再重试(错误原因经 get_memory_store_error() 可查,
    供 UI 展示)。测试可用 monkeypatch 重置模块级 _memory_store/_memory_store_error;
    embedding_function 仅测试传入(轻量确定性函数), 生产默认 None = ChromaDB 内置模型。
    """
    global _memory_store, _memory_store_error
    if _memory_store is not None:
        return _memory_store
    if _memory_store_error is not None:
        return None
    with _memory_store_lock:
        if _memory_store is not None:
            return _memory_store
        if _memory_store_error is not None:
            return None
        try:
            _memory_store = MemoryStore(persist_directory, embedding_function=embedding_function)
            _logger.info("长期记忆已启用: ChromaDB 向量库 %s", persist_directory)
        except Exception as exc:  # noqa: BLE001 —— chromadb 缺失/初始化失败降级
            _memory_store_error = f"{type(exc).__name__}: {exc}"
            _logger.warning("长期记忆不可用(降级为无记忆模式): %s", _memory_store_error)
            return None
    return _memory_store


def get_memory_store_error() -> Optional[str]:
    """返回记忆库初始化失败原因(供 UI/日志展示), 未失败返回 None。"""
    return _memory_store_error
