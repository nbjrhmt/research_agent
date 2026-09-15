"""
core/checkpoint_store.py —— LangGraph SqliteSaver 会话 checkpoint 持久化封装(断点续研)

背景:
    原工程边界: LangGraph checkpointer 仅用 InMemorySaver(单进程内存, 服务重启即丢失),
    任务中途异常只靠 temp_upload/partial_*.json 落盘素材兜底。本模块在此基础上做增量:
    Agent 运行中间状态(LangGraph checkpoint)持久化到本地 sqlite 数据库文件(默认项目根目录
    agent_checkpoints.db, 可用环境变量 CHECKPOINT_DB_PATH 覆盖路径), Streamlit / 进程
    重启后仍可按 thread_id 恢复继续执行未完成的调研会话。

设计说明(与工程约束对齐):
    1. 纯 core 层新增模块: 不 import streamlit, 不 import 项目根目录平铺模块
       (graph_builder / main / logging_setup), 可独立 import 与离线单元测试;
    2. 不改动 graph_builder 任何节点逻辑: build_graph 本就支持可选 checkpointer 参数,
       本模块只负责构造并管理 SqliteSaver(checkpointer) + 会话登记表, 由调用方
       (main.py)把 saver 作为 checkpointer 传入;
    3. 兼容旧流程: 不传 checkpointer(或 CHECKPOINT_PERSIST=false / langgraph-checkpoint-
       sqlite 依赖缺失 / 初始化失败)时, 调用方维持原有 InMemorySaver 内存模式, 完全
       不影响旧行为 —— 本模块用 get_checkpoint_store() 返回 None 表达"持久化不可用",
       绝不抛异常阻塞启动;
    4. 依赖 langgraph.checkpoint.sqlite.SqliteSaver 提供方为 pip 包
       langgraph-checkpoint-sqlite(延迟导入, 模块 import 本身不依赖该包);
    5. 会话登记: 同一 sqlite 文件内额外维护 agent_sessions 表(线程 id / 调研主题 /
       状态 / 轮次 / 素材数 / 时间), 供前端"读取历史会话、切换恢复"使用 —— 该表属于
       本模块自己的登记簿, 不侵入 langgraph 的 checkpoints/writes 表;
    6. 线程安全: sqlite3.Connection(check_same_thread=False) + 模块/实例级锁, 适配
       Streamlit 单进程多会话重跑; 多进程部署仍受"单进程边界"约束(见 README 已知局限)。

文件命名与忽略: 数据库文件默认 agent_checkpoints.db(已加入 .gitignore / .dockerignore,
不入库、不打镜像); 容器部署时通过 volume 把宿主目录挂载到容器, 或设 CHECKPOINT_DB_PATH
指向挂载卷路径, 见 README「Agent 会话持久化」小节。

环境变量(可选, 解析统一走 core.config):
    CHECKPOINT_PERSIST   true(默认)/false: false 时 get_checkpoint_store() 返回 None,
                        调用方退回内存模式(与旧版完全一致);
    CHECKPOINT_DB_PATH   sqlite 文件路径(相对/绝对), 默认 <项目根目录>/agent_checkpoints.db。
"""
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from core.config import env_flag, env_str  # 统一环境变量解析(与 core 其它模块一致)

_logger = logging.getLogger("core.checkpoint_store")

# sqlite 包导入延迟到"真正构造 saver"时执行: 模块 import 不依赖 langgraph-checkpoint-sqlite,
# 依赖缺失时由调用方降级为内存模式并给出提示(兼容旧环境, 详见模块 docstring)。
_SAVER_IMPORT_ERROR: Optional[str] = None
try:  # 仅探测 import 路径是否可用(不实例化), 失败记录原因供 UI/日志提示
    from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver  # noqa: F401
except Exception as exc:  # noqa: BLE001 —— 依赖缺失属可降级场景, 不抛异常
    _SAVER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

DEFAULT_DB_FILENAME = "agent_checkpoints.db"  # 数据库文件名(固定命名, 需求约定)
SESSIONS_TABLE = "agent_sessions"             # 会话登记表(本模块自有, 非 langgraph 表)

SESSION_STATUS_RUNNING = "running"  # 会话已开始但尚未完成(中断后可恢复)
SESSION_STATUS_DONE = "done"        # 会话已产出 final_report(完成)

_DB_SESSION_LIMIT = 100  # 登记表保留行数上限(超出删除最旧, 防无限膨胀; 数据量小仅为卫生)


# ============================ 路径与开关 ============================
def get_default_db_path() -> str:
    """默认 sqlite 文件路径: <项目根目录>/agent_checkpoints.db(项目根 = core/ 的上级)。"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, DEFAULT_DB_FILENAME)


def get_db_path() -> str:
    """实际使用的 sqlite 文件路径: 优先 CHECKPOINT_DB_PATH 环境变量, 否则默认路径。

    环境变量支持相对路径(相对当前工作目录解析, 与容器内 -v 挂载配合使用)。
    """
    override = env_str("CHECKPOINT_DB_PATH", "")
    if override:
        return os.path.abspath(override)
    return get_default_db_path()


def persistence_enabled() -> bool:
    """持久化总开关: CHECKPOINT_PERSIST=false 时整体关闭(退回内存模式, 旧行为)。"""
    return env_flag("CHECKPOINT_PERSIST", True)


def saver_import_error() -> Optional[str]:
    """langgraph.checkpoint.sqlite 不可用时的原因(可空); 供 UI 提示降级原因。"""
    return _SAVER_IMPORT_ERROR


# ============================ SqliteSaver 封装(核心) ============================
class CheckpointStore:
    """SqliteSaver 状态持久化封装: 同一 sqlite 连接上叠加"会话登记表"。

    用法(通常不需要直接实例化, 走模块级 get_checkpoint_store() 单例):
        store = CheckpointStore()                       # 默认 agent_checkpoints.db
        store = CheckpointStore(":memory:")             # 纯内存(单元测试, 不产生磁盘文件)
        graph = build_graph(llm, checkpointer=store.saver)   # 把 saver 交给 LangGraph
        thread_id = store.create_session("调研主题")     # 登记新会话
        config = {"configurable": {"thread_id": thread_id}}
        graph.stream(state, config=config)               # 运行中间状态自动写入 sqlite

    线程安全: 内部所有 sqlite 操作经 self._lock 串行化(sqlite3 连接以
    check_same_thread=False 创建, 由本锁保证单进程内跨线程安全)。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_db_path()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._saver: Optional[Any] = None
        self._ensure_registry_schema()

    # ---------- saver / 连接 ----------
    @property
    def saver(self) -> Any:
        """懒构造 SqliteSaver(同一连接); langgraph.checkpoint.sqlite 缺失时在此抛出。"""
        if self._saver is None:
            with self._lock:
                if self._saver is None:
                    if _SAVER_IMPORT_ERROR is not None:
                        raise ImportError(
                            "langgraph.checkpoint.sqlite 不可用(langgraph-checkpoint-sqlite "
                            f"依赖缺失?): {_SAVER_IMPORT_ERROR}"
                        )
                    self._saver = _SqliteSaver(self._conn)
        return self._saver

    @property
    def connection(self) -> sqlite3.Connection:
        """暴露底层 sqlite 连接(仅供本模块内/测试读取表结构等场景使用)。"""
        return self._conn

    # ---------- 会话登记簿(agent_sessions) ----------
    def _ensure_registry_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SESSIONS_TABLE} (
                    thread_id   TEXT PRIMARY KEY,
                    user_query  TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT '{SESSION_STATUS_RUNNING}',
                    rounds      INTEGER NOT NULL DEFAULT 0,
                    materials   INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def create_session(self, user_query: str, thread_id: Optional[str] = None) -> str:
        """登记一个新会话(状态 running), 返回 thread_id。"""
        tid = thread_id or uuid.uuid4().hex[:12]
        now = self._now()
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {SESSIONS_TABLE} "
                "(thread_id, user_query, status, rounds, materials, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0, ?, ?)",
                (tid, str(user_query or "").strip(), SESSION_STATUS_RUNNING, now, now),
            )
            # 卫生: 登记行超过上限时删除最旧(不删除 checkpoint 数据, 只删登记)
            self._conn.execute(
                f"DELETE FROM {SESSIONS_TABLE} WHERE thread_id NOT IN "
                f"(SELECT thread_id FROM {SESSIONS_TABLE} ORDER BY updated_at DESC, "
                f"rowid DESC LIMIT ?)",
                (_DB_SESSION_LIMIT,),
            )
            self._conn.commit()
        return tid

    def get_session(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """读取单个会话登记(无则 None)。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {SESSIONS_TABLE} WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """列出历史会话登记(按最近更新倒序, 上限 limit 条), 供前端选择恢复。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM {SESSIONS_TABLE} ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_session(
        self,
        thread_id: str,
        *,
        status: Optional[str] = None,
        user_query: Optional[str] = None,
        rounds: Optional[int] = None,
        materials: Optional[int] = None,
    ) -> None:
        """更新会话登记(只更新传入的字段; 无记录则忽略, 不报错)。"""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT thread_id FROM {SESSIONS_TABLE} WHERE thread_id = ?", (thread_id,)
            )
            if cur.fetchone() is None:
                return
            sets, args = ['updated_at = ?'], [self._now()]
            if status is not None:
                sets.append("status = ?")
                args.append(status)
            if user_query is not None:
                sets.append("user_query = ?")
                args.append(str(user_query).strip())
            if rounds is not None:
                sets.append("rounds = ?")
                args.append(int(rounds))
            if materials is not None:
                sets.append("materials = ?")
                args.append(int(materials))
            args.append(thread_id)
            self._conn.execute(
                f"UPDATE {SESSIONS_TABLE} SET {', '.join(sets)} WHERE thread_id = ?", args
            )
            self._conn.commit()

    # ---------- checkpoint 查询辅助 ----------
    def has_checkpoint(self, thread_id: str) -> bool:
        """该会话是否已有至少一个可恢复的 LangGraph checkpoint(无则恢复无意义)。"""
        try:
            return self.saver.get_tuple({"configurable": {"thread_id": thread_id}}) is not None
        except Exception:  # noqa: BLE001 —— 读取失败按"不可恢复"处理, 由调用方走新建会话
            _logger.warning("读取会话 %s 的 checkpoint 失败, 按不可恢复处理", thread_id, exc_info=True)
            return False

    def close(self) -> None:
        """关闭连接(进程退出/测试清理用); 关闭后本实例不可再用。"""
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.close()
            finally:
                self._conn = None  # type: ignore[assignment]
                self._saver = None


# ============================ 进程级单例(供 Streamlit/main.py 使用) ============================
_store_guard = threading.Lock()
_store: Optional[CheckpointStore] = None
_store_disabled_reason: Optional[str] = None


def open_checkpoint_store(db_path: Optional[str] = None) -> CheckpointStore:
    """显式创建一个持久化 store(测试 / 需要独立实例的场景用)。

    注: langgraph.checkpoint.sqlite 依赖缺失时, 构造本身不失败(saver 懒构造), 只有真正
    访问 .saver 时才抛 ImportError —— get_checkpoint_store() 已按此降级为 None。
    """
    return CheckpointStore(db_path)


def get_checkpoint_store() -> Optional[CheckpointStore]:
    """进程级单例: 返回可用 store; 持久化关闭/依赖缺失/初始化失败一律返回 None。

    返回 None 时调用方应维持原有内存模式(InMemorySaver), 保证旧流程完全不受影响;
    降级原因可通过 checkpoint_store_disabled_reason() 查询(UI 提示用)。
    """
    global _store, _store_disabled_reason
    if _store is not None:
        return _store
    if not persistence_enabled():
        _store_disabled_reason = "CHECKPOINT_PERSIST=false(已配置为内存模式)"
        return None
    with _store_guard:
        if _store is None:
            try:
                _store = CheckpointStore()
                _ = _store.saver  # 立即实例化 saver: 依赖缺失在此刻暴露并降级
                _logger.info("Agent 会话 checkpoint 持久化已启用: sqlite 文件 %s", _store.db_path)
            except Exception as exc:  # noqa: BLE001 —— 持久化不可用属可降级场景, 不阻塞启动
                _store = None
                _store_disabled_reason = f"{type(exc).__name__}: {exc}"
                _logger.warning("SqliteSaver 持久化不可用, 回退 InMemorySaver 内存模式: %s", exc)
    return _store


def checkpoint_store_disabled_reason() -> Optional[str]:
    """持久化不可用(返回 None 的 get_checkpoint_store)的原因, 供 UI/日志展示。"""
    return _store_disabled_reason


def reset_checkpoint_store() -> None:
    """关闭并清空单例(测试隔离用; 生产代码不应调用)。"""
    global _store, _store_disabled_reason
    with _store_guard:
        if _store is not None:
            try:
                _store.close()
            except Exception:  # noqa: BLE001
                pass
        _store = None
        _store_disabled_reason = None
