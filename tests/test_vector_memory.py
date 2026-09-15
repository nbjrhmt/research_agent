"""
tests/test_vector_memory.py —— memory/vector_memory 离线单元测试(pytest, 不调用 LLM API / 不联网)

覆盖(v1.6.0 长期记忆/RAG 底座):
    - chunk_text 纯函数: 空文本 / 短文本 / 长文本换行断点 / 无换行硬切 / 重叠推进防死循环;
    - MemoryStore(临时目录 PersistentClient, 不经进程级单例):
        任务级记忆 save_run + search(kind=run) 中文相似命中;
        上传文档分块 save_document + search(kind=doc_chunk) 命中 + 块数统计;
        kind 过滤隔离(run 检索不返回 doc_chunk);
        count / clear_all 管理;
        空库检索返回空列表;
    - get_memory_store 单例: 初始化失败返回 None 且可查原因(monkeypatch 重置全局)。

说明: 全部用例使用临时目录做向量库, 不写项目 chroma_db/ 目录; 测试结束后由
pytest tmp_path 自动清理。
"""
import threading

import pytest

import memory.vector_memory as vm
from memory.vector_memory import (  # noqa: E402
    MemoryStore,
    chunk_text,
    get_memory_store,
    get_memory_store_error,
)


# =====================================================================
# chunk_text 纯函数
# =====================================================================
def test_chunk_text_empty_and_short():
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text("短文本") == ["短文本"]
    assert chunk_text("刚好等于分块大小的文本"[:6], chunk_size=6) == ["刚好等于分块大小"[:6]]


def test_chunk_text_splits_at_newline_preferred():
    """长文本优先在换行处断开: 每个块内部不含未断开的超长无换行段。"""
    text = "第一段内容\n" * 10
    chunks = chunk_text(text, chunk_size=40, overlap=0)
    assert len(chunks) > 1
    for ch in chunks:
        assert len(ch) <= 40 + 1  # 断点含换行符, 允许 +1


def test_chunk_text_hard_cut_without_newline():
    """无换行的超长文本按 chunk_size 硬切, 且每块长度不超上限。"""
    text = "A" * 1000
    chunks = chunk_text(text, chunk_size=100, overlap=0)
    assert len(chunks) == 10
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_text_overlap_keeps_context():
    """overlap>0 时相邻块有重叠内容(保留上下文衔接)。"""
    text = "内容内容内容内容内容内容内容内容内容内容"  # 30 字
    chunks = chunk_text(text, chunk_size=10, overlap=3)
    assert len(chunks) >= 2
    # 相邻两块应共享重叠片段(后一块开头 = 前一块结尾附近内容)
    assert chunks[0][-3:] in chunks[1]


def test_chunk_text_never_infinite_loop():
    """极端 overlap 参数下 start 必须单调前进(防死循环)。"""
    text = "X" * 500
    chunks = chunk_text(text, chunk_size=50, overlap=49)  # overlap 接近 chunk_size
    assert len(chunks) >= 2
    joined = "".join(chunks)
    assert len(joined) >= len(text)  # 拼接后不丢内容(重叠只会多不会少)


# =====================================================================
# MemoryStore(session 级内存向量库, 不经进程级单例)
# =====================================================================
@pytest.fixture(scope="session")
def store():
    """session 级 EphemeralClient 内存向量库(默认 embedding 模型已本地缓存)。

    所有用例共享同一实例、每用例自动清空, 避免重复初始化 ChromaDB(约 2s/次)。
    """
    return MemoryStore(persist_directory=None)


@pytest.fixture(autouse=True)
def _clean_store(store):
    """每个用例开始前清空向量库, 保证用例间状态隔离。"""
    store.clear_all()
    yield


def test_save_run_and_search_chinese(store):
    """任务级记忆: 中文主题保存后可按相似 query 命中, 返回来源 query 与分数。"""
    store.save_run(
        "新能源汽车销量分析",
        ["【素材】比亚迪 2025 年销量 400 万辆", "【素材】宁德时代电池装机量数据"],
        "# 新能源汽车调研报告\n销量……",
    )
    hits = store.search("新能源汽车销量", top_k=3, kind="run")
    assert hits, "相似主题应命中历史任务记忆"
    assert hits[0]["kind"] == "run"
    assert hits[0]["query"] == "新能源汽车销量分析"
    assert hits[0]["content"]
    assert hits[0]["score"] is not None


def test_save_document_chunks_and_search(store):
    """文档分块 RAG: 全文分块入库后, 按主题检索命中相关块, 且块数 = 实际入库数。"""
    text = ("锂离子电池技术原理。\n" * 30) + "固态电池是下一代电池技术方向。\n" * 30
    n = store.save_document("电池白皮书.pdf", text, chunk_size=100, overlap=10)
    assert n >= 2, "长文本应被切成多块入库"

    hits = store.search("固态电池", top_k=3, kind="doc_chunk")
    assert hits, "相关主题应命中文档块"
    assert hits[0]["kind"] == "doc_chunk"
    assert hits[0]["source_name"] == "电池白皮书.pdf"
    assert "固态电池" in hits[0]["content"]


def test_search_kind_isolation(store):
    """kind 过滤隔离: run 检索不返回文档块, doc_chunk 检索不返回任务记忆。"""
    store.save_run("主题A", ["素材"], "报告A")
    store.save_document("文档B.txt", "内容内容内容内容内容内容" * 20, chunk_size=50, overlap=5)

    run_hits = store.search("主题A", top_k=5, kind="run")
    doc_hits = store.search("主题A", top_k=5, kind="doc_chunk")
    assert run_hits and all(h["kind"] == "run" for h in run_hits)
    assert all(h["kind"] == "doc_chunk" for h in doc_hits)


def test_search_empty_store_returns_empty(store):
    assert store.search("任何主题", top_k=3) == []
    assert store.count() == 0


def test_save_document_empty_text(store):
    assert store.save_document("空文档.txt", "   ") == 0
    assert store.count() == 0


def test_count_and_clear_all(store):
    store.save_run("主题1", ["素材"], "报告1")
    store.save_run("主题2", ["素材"], "报告2")
    store.save_document("文档.txt", "内容内容内容内容内容内容内容内容内容内容" * 10,
                        chunk_size=20, overlap=2)
    total = store.count()
    assert total >= 3
    cleared = store.clear_all()
    assert cleared == total
    assert store.count() == 0


def test_store_thread_safety_smoke(store):
    """多线程并发写入不抛异常(写操作带锁)。"""
    errors = []

    def _write(i):
        try:
            store.save_run(f"并发主题{i}", [f"素材{i}"], f"报告{i}")
        except Exception as exc:  # noqa: BLE001 —— 测试断言用
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.count() >= 5


# =====================================================================
# get_memory_store 单例(容错降级)
# =====================================================================
def test_get_memory_store_success(monkeypatch):
    """单例可正常创建(monkeypatch 重置全局, 内存模式)。"""
    monkeypatch.setattr(vm, "_memory_store", None)
    monkeypatch.setattr(vm, "_memory_store_error", None)
    store = get_memory_store(persist_directory=None)
    assert store is not None
    assert get_memory_store_error() is None
    # 同一进程再次获取返回同一实例
    assert get_memory_store(persist_directory=None) is store


def test_get_memory_store_failure_returns_none(monkeypatch):
    """初始化失败: 返回 None, 且可查询失败原因(UI 展示用), 不再重试。"""
    monkeypatch.setattr(vm, "_memory_store", None)
    monkeypatch.setattr(vm, "_memory_store_error", None)

    def _boom(*args, **kwargs):
        raise RuntimeError("chromadb 初始化失败(测试)")

    monkeypatch.setattr(vm, "MemoryStore", _boom)
    store = get_memory_store(persist_directory=None)
    assert store is None
    assert "chromadb 初始化失败" in (get_memory_store_error() or "")


def test_chunk_text_import_public_api():
    """公开 API 形态(供面试讲解/README 引用): chunk_text / MemoryStore / get_memory_store。"""
    assert callable(chunk_text)
    assert callable(MemoryStore)
    assert callable(get_memory_store)
