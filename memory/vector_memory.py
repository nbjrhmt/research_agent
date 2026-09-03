"""
vector_memory.py —— ChromaDB 长期记忆(可选模块)

════════════════════════════════════════════════════════════════════
⚠️⚠️  当前状态: MVP 阶段【注释停用】, 不参与主流程。  ⚠️⚠️
════════════════════════════════════════════════════════════════════

设计目标(规格文档):
    把每一次任务的 query / 搜集素材 / 最终报告 存入 ChromaDB;
    新任务启动时先检索相似历史调研, 把历史素材加入 State, 减少重复搜索。

启用步骤(主流程跑通后再做):
    1) 在 graph_builder.py / main.py 中找到标注「长期记忆接入点」的注释段, 按说明取消注释;
    2) 调用示例:
         from memory.vector_memory import MemoryStore
         store = MemoryStore()                        # 首次运行会自动创建 chroma_db 目录
         hits  = store.search(user_query, top_k=3)    # 任务开始前: 检索相似历史素材
         store.save_run(user_query, materials, report)  # 任务结束后: 保存本次成果
    3) 把检索到的历史素材(带【历史记忆】前缀)加入 State 的 collected_info, 并写入日志。
"""
import os
import time
import uuid

# chromadb 延迟导入: 本模块未被引用时不影响 MVP 启动
# import chromadb

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_DIR = os.path.join(BASE_DIR, "chroma_db")  # 向量库持久化目录

COLLECTION_NAME = "research_memory"


class MemoryStore:
    """历史调研任务素材的向量记忆库(ChromaDB 本地持久化)。"""

    def __init__(self, persist_directory: str = CHROMA_DIR) -> None:
        import chromadb  # 延迟导入, 仅在真正启用记忆时加载

        os.makedirs(persist_directory, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_directory)
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},  # 余弦相似度
        )

    def save_run(self, query: str, materials: list, report: str) -> str:
        """
        保存一次完整调研: 素材拼接成一份文档存入, 便于以后检索复用。
        """
        doc_body = "\n\n".join(materials)
        doc = f"【调研主题】{query}\n【素材内容】\n{doc_body}\n【最终报告】\n{report}"
        doc_id = uuid.uuid4().hex
        self.collection.upsert(
            ids=[doc_id],
            documents=[doc[:30000]],  # 控制单条记录长度
            metadatas=[{"query": query, "time": time.strftime("%Y-%m-%d %H:%M:%S")}],
        )
        return doc_id

    def search(self, query: str, top_k: int = 3) -> list:
        """
        检索与当前主题最相似的历史调研素材(供新任务复用)。
        返回: [{"id":..., "content":..., "query":...}, ...]
        """
        if self.collection.count() == 0:
            return []
        result = self.collection.query(query_texts=[query], n_results=min(top_k, self.collection.count()))
        items = []
        for doc_id, doc, meta in zip(
            (result.get("ids") or [[]])[0],
            (result.get("documents") or [[]])[0],
            (result.get("metadatas") or [[]])[0],
        ):
            items.append({
                "id": doc_id,
                "content": doc or "",
                "query": (meta or {}).get("query", ""),
            })
        return items

    def clear_all(self) -> int:
        """清空全部历史记忆(按需手动调用)"""
        count = self.collection.count()
        if count:
            self.collection.delete(ids=self.collection.get()["ids"])
        return count
