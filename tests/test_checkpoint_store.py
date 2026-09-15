"""
tests/test_checkpoint_store.py —— core/checkpoint_store(SqliteSaver 持久化)单元测试

覆盖(全部离线: 不调用 LLM API / 不联网 / sqlite 一律用 :memory: 内存库, 不产生磁盘文件):
    1. 默认数据库文件命名(agent_checkpoints.db)与 CHECKPOINT_DB_PATH / CHECKPOINT_PERSIST
       环境变量解析(get_db_path / persistence_enabled);
    2. CheckpointStore: 登记表与 langgraph checkpoints/writes 表结构、会话登记
       (create/get/list/update, 倒序、无记录 no-op)、has_checkpoint;
    3. SqliteSaver 持久化语义: 同一 sqlite 连接上"重启(saver/图重建)后状态仍在、可继续";
    4. 端到端断点续研: build_graph 运行中模拟崩溃(报告节点 LLM 故障)→ 重建图 + 新 saver
       以同一 thread_id 恢复 → 最终报告/素材/主题完整, 崩溃前素材不丢失;
    5. 兼容性守护: build_graph 不传 checkpointer 时保持纯内存运行(旧流程不受影响);
    6. 模块 import 无副作用(不创建磁盘数据库文件)。

运行: python -m pytest tests/test_checkpoint_store.py -q(或随全量 python -m pytest tests)
"""
import json
import os
import sqlite3
from typing import TypedDict

import pytest

import core.checkpoint_store as cps
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


class _MiniState(TypedDict, total=False):
    """小图状态: 单一 int 通道(每节点覆盖写)。"""
    v: int


# =====================================================================
# 通用测试替身 / 小工具
# =====================================================================
def _memory_conn() -> sqlite3.Connection:
    """独立的内存 sqlite 连接(check_same_thread=False, 与 SqliteSaver 约定一致)。"""
    return sqlite3.connect(":memory:", check_same_thread=False)


class _R:
    def __init__(self, content):
        self.content = content


class NodeAwareLLM:
    """按 system 提示词区分节点并按脚本应答的假 LLM(离线, 支持注入崩溃)。

    用于整图断点续研测试: LangGraph 恢复时会重跑"中断的节点", 具体重跑哪个节点由
    checkpoint 决定 —— 本假 LLM 不关心调用顺序, 只按节点身份应答, 保证断言稳定。
    """

    model_name = "mock-model"

    _MARKERS = {
        "planner_node": "调研规划器",
        "tool_node": "工具调度器",
        "reflection_node": "反思评估节点",
        "report_node": "报告生成节点",
    }

    def __init__(self, search_query="测试搜索词", crash_report=False):
        self.search_query = search_query
        self.crash_report = crash_report
        self.attempts = 0
        self.calls: dict = {k: 0 for k in self._MARKERS}

    def _classify(self, messages):
        system_text = str(messages[0][1] or "")
        for name, marker in self._MARKERS.items():
            if marker in system_text:
                return name
        return None

    def invoke(self, messages):
        self.attempts += 1
        node = self._classify(messages)
        if node is not None:
            self.calls[node] += 1
        if node == "planner_node":
            return _R(json.dumps({"sub_tasks": [f"子任务-{self.search_query}"]}, ensure_ascii=False))
        if node == "tool_node":
            return _R(json.dumps({"tool": "bocha_web_search", "query": self.search_query},
                                 ensure_ascii=False))
        if node == "reflection_node":
            return _R(json.dumps({"sufficient": True, "reason": "素材已覆盖(假 LLM 判定)",
                                  "missing_topics": []}, ensure_ascii=False))
        if node == "report_node":
            if self.crash_report:
                raise RuntimeError("模拟中断: 报告节点 LLM 故障(断点续研测试用)")
            return _R("# 调研报告(恢复完成)\n本报告基于假搜索素材生成, 仅用于离线测试。")
        # 理论上不可达: 节点身份无法识别
        raise AssertionError(f"无法识别的调用: {str(messages[-1][1])[:80]}")


def _fake_search(query):
    return f"搜索结果-{query}(离线假数据)"


def _mini_graph(checkpointer, crash_n2=False):
    """构造单节点链式小图(每节点覆盖写 int 字段), 返回编译后的图。"""

    def node_1(state):
        return {"v": (state.get("v") or 0) + 1}

    def node_2(state):
        if crash_n2:
            raise RuntimeError("n2 模拟中断: 节点执行失败(测试用)")
        return {"v": (state.get("v") or 0) + 10}

    g = StateGraph(_MiniState)
    g.add_node("n1", node_1)
    g.add_node("n2", node_2)
    g.add_edge(START, "n1")
    g.add_edge("n1", "n2")
    g.add_edge("n2", END)
    return g.compile(checkpointer=checkpointer)


def _initial_state(query):
    return {
        "user_query": query,
        "sub_tasks": [],
        "collected_info": [],
        "reflection": "",
        "final_report": "",
        "iteration_count": 0,
        "uploaded_files": [],
        "steps_log": [],
    }


# =====================================================================
# 路径 / 开关解析
# =====================================================================
def test_default_db_filename_and_path():
    assert cps.DEFAULT_DB_FILENAME == "agent_checkpoints.db", "数据库文件必须命名为 agent_checkpoints.db"
    path = cps.get_default_db_path()
    assert path.endswith("agent_checkpoints.db")
    assert os.path.basename(os.path.dirname(path)) == os.path.basename(
        os.path.dirname(os.path.dirname(os.path.abspath(cps.__file__)))
    )  # 默认落在项目根目录


def test_get_db_path_env_override(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB_PATH", "sub/checkpoints/my.db")
    assert cps.get_db_path() == os.path.abspath("sub/checkpoints/my.db")
    monkeypatch.delenv("CHECKPOINT_DB_PATH", raising=False)
    assert cps.get_db_path() == cps.get_default_db_path()


def test_persistence_enabled_flag(monkeypatch):
    monkeypatch.delenv("CHECKPOINT_PERSIST", raising=False)
    assert cps.persistence_enabled() is True, "默认开启持久化"
    monkeypatch.setenv("CHECKPOINT_PERSIST", "false")
    assert cps.persistence_enabled() is False
    monkeypatch.setenv("CHECKPOINT_PERSIST", "1")
    assert cps.persistence_enabled() is True


def test_module_import_creates_no_db_file(monkeypatch):
    """模块 import / 路径解析本身不得创建数据库文件(连接只在真正使用时建立)。"""
    default_path = cps.get_default_db_path()
    if os.path.exists(default_path):  # 本地曾手动运行过应用属正常, 跳过(CI 干净环境必测)
        pytest.skip("检测到本地遗留的 agent_checkpoints.db(手动运行过应用), 跳过无副作用断言")
    assert not os.path.exists(default_path)


# =====================================================================
# CheckpointStore: 表结构与会话登记
# =====================================================================
def test_store_creates_tables_in_memory():
    store = cps.CheckpointStore(":memory:")
    try:
        saver = store.saver  # 懒构造 SqliteSaver(依赖可用时)
        assert isinstance(saver, SqliteSaver)
        # 触发 langgraph 表初始化后再断言表结构
        assert store.has_checkpoint("不存在的线程") is False
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert cps.SESSIONS_TABLE in tables          # 会话登记表(本模块自有)
        assert {"checkpoints", "writes"} <= tables    # langgraph checkpoint 表
    finally:
        store.close()


def test_create_get_list_update_session():
    store = cps.CheckpointStore(":memory:")
    try:
        tid_a = store.create_session("历史主题A")
        tid_b = store.create_session("进行中主题B")
        assert tid_a and tid_b and tid_a != tid_b

        rows = store.list_sessions(limit=10)
        assert [r["thread_id"] for r in rows] == [tid_b, tid_a], "按最近更新倒序"
        assert all(r["status"] == cps.SESSION_STATUS_RUNNING for r in rows)
        assert rows[0]["user_query"] == "进行中主题B"

        got = store.get_session(tid_a)
        assert got is not None and got["user_query"] == "历史主题A"
        assert store.get_session("不存在") is None

        # 更新字段: 只更新传入项
        store.update_session(tid_a, status=cps.SESSION_STATUS_DONE, rounds=3, materials=5)
        got = store.get_session(tid_a)
        assert got["status"] == cps.SESSION_STATUS_DONE
        assert got["rounds"] == 3 and got["materials"] == 5
        store.update_session(tid_a, rounds=4)
        assert store.get_session(tid_a)["rounds"] == 4
        assert store.get_session(tid_a)["status"] == cps.SESSION_STATUS_DONE

        # 不存在的线程更新是 no-op(不抛异常)
        store.update_session("幽灵线程", status=cps.SESSION_STATUS_DONE, rounds=9)
        assert store.get_session("幽灵线程") is None
    finally:
        store.close()


def test_create_session_accepts_custom_thread_id():
    store = cps.CheckpointStore(":memory:")
    try:
        assert store.create_session("主题", thread_id="my-custom-id") == "my-custom-id"
        assert store.get_session("my-custom-id")["user_query"] == "主题"
    finally:
        store.close()


def test_has_checkpoint_follows_graph_activity():
    store = cps.CheckpointStore(":memory:")
    try:
        tid = store.create_session("小图测试")
        assert store.has_checkpoint(tid) is False, "尚无 checkpoint 时不可恢复"
        app = _mini_graph(store.saver)
        app.invoke({"v": 0}, {"configurable": {"thread_id": tid}})
        assert store.has_checkpoint(tid) is True, "跑过节点后应存在 checkpoint"
    finally:
        store.close()


# =====================================================================
# SqliteSaver 持久化语义: 同库"重启"(新 saver/新图)后状态可读可续
# =====================================================================
def test_saver_state_survives_reopen_on_same_memory_db():
    conn = _memory_conn()
    cfg = {"configurable": {"thread_id": "s1"}}

    # ---- 第一次运行: n1 完成后, n2 中途崩溃(任务中断, n1 的成果已 checkpoint) ----
    saver1 = SqliteSaver(conn)
    app1 = _mini_graph(saver1, crash_n2=True)
    with pytest.raises(RuntimeError, match="n2 模拟中断"):
        app1.invoke({"v": 0}, cfg)

    # ---- 模拟进程重启: 同一数据库(连接)上新建 saver 与重新编译的图 ----
    saver2 = SqliteSaver(conn)
    assert saver2.get_tuple(cfg) is not None, "重启后应能读到旧 checkpoint"
    app2 = _mini_graph(saver2)
    snap = app2.get_state(cfg)
    assert snap.values["v"] == 1, "崩溃前已完成节点(n1)的结果应被持久化"

    # 继续同一线程: 只补跑中断的节点(n2), 不重跑已完成的 n1
    app2.invoke(None, cfg)
    snap2 = app2.get_state(cfg)
    assert snap2.values["v"] == 11, "续跑应基于持久化状态执行 n2(+10), 而不是从 0 重跑 n1/n2"


# =====================================================================
# 端到端断点续研: 真实 research 图 崩溃 → 重建 → 恢复
# =====================================================================
def test_research_run_crash_then_resume_on_same_thread(monkeypatch):
    """完整 build_graph 跑一轮后在报告节点模拟崩溃, 重启后用同一 thread_id 恢复成功。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)  # 去掉重试退避等待

    conn = _memory_conn()
    query = "断点恢复主题"
    cfg = {"configurable": {"thread_id": "resume-1"}}

    # ---- 第一次运行: 一轮搜集后报告节点崩溃(任务中断) ----
    llm1 = NodeAwareLLM(search_query="原搜索词", crash_report=True)
    graph1 = None
    from graph_builder import build_graph

    graph1 = build_graph(llm1, web_search_tool=_fake_search, checkpointer=SqliteSaver(conn))
    with pytest.raises(RuntimeError, match="模拟中断"):
        list(graph1.stream(_initial_state(query), config=cfg, stream_mode="updates"))

    snap_crash = graph1.get_state(cfg)
    assert snap_crash.values.get("user_query") == query
    assert len(snap_crash.values.get("collected_info") or []) == 1, "崩溃前素材应已落 checkpoint"
    assert "原搜索词" in snap_crash.values["collected_info"][0]

    # ---- 第二次运行(进程重启): 新 saver + 新图, 同一 thread_id 从断点恢复 ----
    llm2 = NodeAwareLLM(search_query="恢复搜索词", crash_report=False)
    graph2 = build_graph(llm2, web_search_tool=_fake_search, checkpointer=SqliteSaver(conn))
    events = list(graph2.stream(None, config=cfg, stream_mode="updates"))
    assert events, "恢复运行应产生节点事件"

    final = graph2.get_state(cfg)
    values = final.values
    # 主题与崩溃前素材不丢
    assert values.get("user_query") == query
    materials = values.get("collected_info") or []
    assert any("原搜索词" in m for m in materials), "崩溃前已搜集素材必须保留"
    # 恢复后成功产出最终报告(且不再需要重新规划)
    assert "恢复完成" in (values.get("final_report") or "")
    assert llm2.calls["planner_node"] == 0, "checkpoint 之后无需重新规划"
    assert llm2.calls["report_node"] >= 1


def test_build_graph_without_checkpointer_stays_pure_memory(monkeypatch):
    """不传 checkpointer 时维持纯内存运行(旧流程守护): 编译结果不带 checkpointer, 可无配置直接跑。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)
    from graph_builder import build_graph

    llm = NodeAwareLLM(search_query="内存模式搜索")
    graph = build_graph(llm, web_search_tool=_fake_search, checkpointer=None)
    assert getattr(graph, "checkpointer", None) is None, "未传 checkpointer 时不得附加持久化"
    events = list(graph.stream(_initial_state("内存模式主题"), stream_mode="updates"))
    node_names = [name for ev in events for name in ev.keys()]
    assert "planner_node" in node_names and "report_node" in node_names
    assert llm.calls["planner_node"] == 1 and llm.calls["report_node"] == 1
    # 纯内存运行同样产出最终报告(通过返回值), 与旧行为一致
    assert graph.invoke(_initial_state("内存模式主题2")).get("final_report", "").strip()


# =====================================================================
# 会话登记簿 + 真实图联动(create_session → 运行 → 完成登记)
# =====================================================================
def test_store_registry_drives_full_run_bookkeeping():
    store = cps.CheckpointStore(":memory:")
    try:
        from graph_builder import build_graph

        tid = store.create_session("登记簿主题")
        llm = NodeAwareLLM(search_query="登记簿搜索")
        graph = build_graph(llm, web_search_tool=_fake_search, checkpointer=store.saver)
        cfg = {"configurable": {"thread_id": tid}}
        list(graph.stream(_initial_state("登记簿主题"), config=cfg, stream_mode="updates"))

        row = store.get_session(tid)
        assert row is not None
        assert row["status"] == cps.SESSION_STATUS_RUNNING, "登记簿状态由 UI 层在成功后置为 done"
        assert store.has_checkpoint(tid) is True
        # 完成后登记簿落账(main.py 成功路径行为)
        store.update_session(tid, status=cps.SESSION_STATUS_DONE,
                             rounds=int(graph.get_state(cfg).values.get("iteration_count") or 0),
                             materials=len(graph.get_state(cfg).values.get("collected_info") or []))
        done_row = store.get_session(tid)
        assert done_row["status"] == cps.SESSION_STATUS_DONE
        assert done_row["rounds"] >= 1 and done_row["materials"] >= 1
        assert store.list_sessions(limit=5)[0]["thread_id"] == tid
    finally:
        store.close()


def test_list_sessions_limit():
    store = cps.CheckpointStore(":memory:")
    try:
        for i in range(15):
            store.create_session(f"主题{i}")
        assert len(store.list_sessions(limit=5)) == 5
        assert store.list_sessions(limit=100)[0]["user_query"] == "主题14"  # 最新在前
        assert len(store.list_sessions()) <= 100
    finally:
        store.close()
