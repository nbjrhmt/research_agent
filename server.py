"""
server.py —— FastAPI 服务化入口(把 LangGraph 调研 Agent 封装为 REST API, v1.6.0)

与 Streamlit 前端(main.py)共用同一套引擎: core/ + graph_builder + tools + memory。
设计目标(面试口径): Agent 从"演示 Demo"变为"可集成服务", 其他系统可直接调用:

    POST /api/research              提交调研任务(JSON: {"query": "..."}) → {task_id}
    POST /api/research/with-files   提交调研任务(multipart: query + files, 支持 PDF/CSV)
    GET  /api/research/{task_id}    查询任务状态与结果(轮询式异步: pending/running/done/failed)
    GET  /api/research              最近任务列表(默认最近 20 条)
    GET  /health                    健康检查

运行: uvicorn server:app --host 127.0.0.1 --port 8000
说明:
    - 任务在进程内线程池后台执行(ThreadPoolExecutor), 状态存内存 dict + 锁;
    - 单进程边界(与项目"单用户本地原型"定位一致), 多实例部署需外置任务队列;
    - 长期记忆: 默认启用(MEMORY_ENABLED), 与 UI 共用同一 ChromaDB 向量库;
    - 文件上传: 落盘 temp_upload/ 后预读为素材, 任务结束自动清理。
"""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from core.config import env_flag
from core.file_store import delete_uploaded_files, save_upload
from core.ingest import ingest_upload
from core.report_verifier import verify_report_citations
from graph_builder import build_graph, build_llm
from logging_setup import get_logger
from tools.search_tool import bocha_web_search

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))  # 尽早读取 .env(与 main.py / graph_builder 一致)

_logger = get_logger("server")

MAX_FILE_SIZE_MB = 30          # 单文件大小上限(与 main.py 一致)
DEFAULT_MAX_WORKERS = 2        # 后台任务并发线程数(单机演示够用, 多实例需外置队列)

# 长期记忆(可选): chromadb 不可用时自动降级为 None(与 main.py 同一单例)
from memory.vector_memory import get_memory_store  # noqa: E402

# ============================ 任务存储(内存态) ============================
# 单进程边界: 任务状态保存在进程内 dict, 服务重启即失(项目定位为单用户本地原型,
# 持久化任务队列属 P2 演进方向, 见 README 已知局限)。
class TaskStore:
    """进程内调研任务存储(内存 dict + 锁, 线程安全)。

    排序用自增 seq(而非秒级时间字符串): 同一秒内创建的任务顺序也确定。
    """

    def __init__(self) -> None:
        self._tasks: dict = {}
        self._seq = 0
        self._lock = threading.Lock()

    def create(self, query: str) -> dict:
        with self._lock:
            self._seq += 1
            task = {
                "task_id": uuid.uuid4().hex[:12],
                "seq": self._seq,
                "status": "pending",           # pending → running → done / failed
                "query": query,
                "report": None,
                "materials_count": 0,
                "rounds": 0,
                "error": None,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
            }
            self._tasks[task["task_id"]] = task
        return dict(task)

    def update(self, task_id: str, **fields) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.update(fields)

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None

    def list(self, limit: int = 20) -> List[dict]:
        with self._lock:
            items = sorted(self._tasks.values(), key=lambda t: t.get("seq") or 0, reverse=True)
            return [dict(t) for t in items[:limit]]


_store = TaskStore()
_executor = ThreadPoolExecutor(max_workers=int(os.getenv("API_MAX_WORKERS", DEFAULT_MAX_WORKERS)))


# ============================ 后台任务执行 ============================
def _run_research_task(task_id: str, query: str, fnames: List[str]) -> None:
    """后台执行一次完整调研(与 main.py 共用 build_graph 引擎), 回写任务状态。

    异常不抛出: 全部失败信息写入任务 error 字段(API 调用方可查询到明确原因)。
    """
    started = time.time()
    _store.update(task_id, status="running",
                  started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    graph = None
    config = None
    try:
        # 1) 上传文件预读为素材(与 main.py 行为一致); 同时收集素材文本供文档分块入库
        initial_materials = []
        uploaded_materials = []   # (fname, material) 供长期记忆 save_document
        for fname in fnames:
            material, _log_line = ingest_upload(fname)
            if material:
                initial_materials.append(material)
                uploaded_materials.append((fname, material))

        # 2) 长期记忆: 上传文档分块入库(RAG 底座) + 图首检索注入
        memory_store = get_memory_store() if env_flag("MEMORY_ENABLED", True) else None
        if memory_store is not None:
            for fname, material in uploaded_materials:
                try:
                    memory_store.save_document(fname, material)
                except Exception as exc:  # noqa: BLE001 —— 记忆失败不阻断任务
                    _logger.warning("API 任务文档分块入库失败(%s): %s", fname, exc)

        # 3) 构建 LLM 与图(API 任务用 InMemorySaver, 单次任务不落 checkpoint)
        llm = build_llm()
        checkpointer = InMemorySaver()
        graph = build_graph(llm, web_search_tool=bocha_web_search,
                            checkpointer=checkpointer, memory_store=memory_store)
        thread_id = uuid.uuid4().hex[:12]
        config = {"configurable": {"thread_id": thread_id}}
        initial_state = {
            "user_query": query,
            "sub_tasks": [],
            "collected_info": initial_materials,
            "reflection": "",
            "final_report": "",
            "iteration_count": 0,
            "uploaded_files": fnames,
            "steps_log": [],
        }

        # 4) 执行图(同步等待, 后台线程内阻塞; 完成/失败都回写状态)
        result = graph.invoke(initial_state, config=config)
        report = str(result.get("final_report") or "").strip()
        materials = list(result.get("collected_info") or [])
        rounds = int(result.get("iteration_count") or 0)

        # 5) 长期记忆回写: 任务完成后保存本次调研供未来复用
        if memory_store is not None and report:
            try:
                memory_store.save_run(query, materials, report)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("API 任务长期记忆保存失败(不影响结果): %s", exc)

        if not report:
            raise RuntimeError("Agent 未生成报告内容(可能素材为空或 LLM 返回空)")

        # 6) 报告引用一致性校验(防幻觉闭环): 确定性核对引用编号是否真实存在
        try:
            citation_check = verify_report_citations(report, materials)
        except Exception:  # noqa: BLE001 —— 校验失败不影响任务结果
            citation_check = {"error": "校验失败"}

        _store.update(task_id, status="done", report=report,
                      materials_count=len(materials), rounds=rounds,
                      citation_check=citation_check,
                      finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _logger.info("API 任务完成: %s (%s 轮 / %s 条素材, 耗时 %.1fs)",
                     task_id, rounds, len(materials), time.time() - started)
    except Exception as exc:  # noqa: BLE001 —— 全部失败信息写入任务 error, 不抛出
        _logger.exception("API 任务失败: %s", task_id)
        _store.update(task_id, status="failed",
                      error=f"{type(exc).__name__}: {exc}",
                      finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        # 清理本次上传文件磁盘副本(素材已进入报告/历史, 保持 temp_upload/ 不膨胀)
        try:
            if fnames:
                delete_uploaded_files(fnames)
        except Exception:  # noqa: BLE001 —— 清理失败不影响任务结果
            pass


# ============================ API 模型与路由 ============================
class ResearchRequest(BaseModel):
    """文本调研请求(JSON)。"""
    query: str = Field(..., min_length=1, max_length=2000, description="调研主题")
    max_iterations: Optional[int] = Field(
        default=None, ge=1, le=20, description="工具迭代轮次上限(可选, 默认全局 MAX_ITERATIONS)")


class TaskOut(BaseModel):
    """任务状态与结果响应。"""
    task_id: str
    status: str
    query: str
    report: Optional[str] = None
    materials_count: int = 0
    rounds: int = 0
    error: Optional[str] = None
    citation_check: Optional[dict] = None   # 报告引用一致性校验结果(v1.6.0)
    created_at: str
    finished_at: Optional[str] = None


app = FastAPI(
    title="Research Agent API",
    description="本地 AI 调研 Agent 的 REST 接口: 提交主题/文件 → 自动规划、搜索、反思迭代 → 返回结构化调研报告。",
    version="1.6.0",
)


@app.post("/api/research", response_model=TaskOut)
def start_research(req: ResearchRequest):
    """提交文本调研任务(JSON), 立即返回 task_id(后台执行, 轮询查询结果)。"""
    task = _store.create(req.query)
    _executor.submit(_run_research_task, task["task_id"], req.query, [])
    return task


@app.post("/api/research/with-files", response_model=TaskOut)
def start_research_with_files(query: str = Form(..., min_length=1, max_length=2000),
                              files: List[UploadFile] = File(default=[])):
    """提交带文件(PDF/CSV)的调研任务(multipart/form-data)。"""
    fnames = []
    for up in files:
        data = up.file.read() if up.file else b""
        if len(data) > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"文件过大: {up.filename}(>{MAX_FILE_SIZE_MB}MB)")
        try:
            fnames.append(save_upload(data, up.filename))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"保存上传文件失败: {up.filename}: {exc}")
    task = _store.create(query)
    _executor.submit(_run_research_task, task["task_id"], query, fnames)
    return task


@app.get("/api/research/{task_id}", response_model=TaskOut)
def get_research(task_id: str):
    """查询任务状态与结果(轮询: 未完成时 status=pending/running, 完成后 status=done/failed)。"""
    task = _store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return task


@app.get("/api/research", response_model=List[TaskOut])
def list_research(limit: int = 20):
    """最近任务列表(默认最近 20 条, 新→旧)。"""
    return _store.list(limit=min(max(limit, 1), 100))


@app.get("/health")
def health():
    """健康检查(部署探针)。"""
    return {"status": "ok", "service": "research-agent", "version": "1.6.0"}


# ============================ 上传落盘(复用 core.file_store) ============================


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("API_PORT", "8000")))
