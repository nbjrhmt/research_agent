"""
tests/test_api.py —— server.py FastAPI 服务化离线单元测试(pytest, 不调用 LLM API / 不联网)

覆盖(v1.6.0 API 服务化):
    - 路由: POST /api/research(JSON 文本)、POST /api/research/with-files(multipart 上传)、
      GET /api/research/{task_id}、GET /api/research(列表)、GET /health;
    - 任务状态机: pending → running → done(成功)/ failed(异常), 结果与错误字段正确回写;
    - 边界: 不存在的 task_id 返回 404、超大文件返回 413、空 query 返回 422;
    - 文件上传: 上传 CSV 文件走完整预读流程。

说明: 全部用例通过 monkeypatch 假 LLM / 假搜索 / 假记忆库完成, 不产生真实 API 调用;
TestClient(httpx) 直连 FastAPI 应用, 无需启动 uvicorn。
"""
import io
import time

import pytest
from fastapi.testclient import TestClient

import server
from server import app

client = TestClient(app)


# =====================================================================
# 测试替身
# =====================================================================
class _FakeLLM:
    """按脚本顺序返回 content 的假 LLM(planner/tool/reflection 输出 JSON, report 输出文本)。"""

    model_name = "fake-model"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self._i = 0

    def invoke(self, messages):
        out = self.outputs[min(self._i, len(self.outputs) - 1)]
        self._i += 1
        if isinstance(out, Exception):
            raise out
        return type("R", (), {"content": out})()


# 一次成功调研需要的 LLM 输出序列: planner → tool → reflection → report
_SUCCESS_LLM_OUTPUTS = [
    '{"sub_tasks": ["子任务1"]}',
    '{"tool": "bocha_web_search", "query": "测试关键词"}',
    '{"sufficient": true, "reason": "素材已充足", "missing_topics": []}',
    "# 测试调研报告\n\n结论: 测试通过(素材1)。",
]


def _make_fake_llm(outputs):
    """返回 build_llm 工厂(每次调用返回新的独立实例, 支持多任务)。"""
    return lambda: _FakeLLM(outputs)


@pytest.fixture()
def api_env(monkeypatch):
    """把 API 引擎替换为离线替身: 假 LLM / 假搜索 / 关闭记忆库。"""
    monkeypatch.setattr(server, "build_llm", _make_fake_llm(_SUCCESS_LLM_OUTPUTS))
    monkeypatch.setattr(server, "bocha_web_search",
                        lambda q: "【素材】测试搜索结果内容。链接: https://example.com")
    monkeypatch.setattr(server, "get_memory_store", lambda: None)  # 测试不写真实 ChromaDB
    server._store._tasks.clear()  # 清空任务存储, 保证用例间隔离
    return server


def _wait_task(task_id: str, timeout: float = 15.0) -> dict:
    """轮询任务直到 done/failed, 返回最终任务数据。"""
    deadline = time.time() + timeout
    last_status = "unknown"
    while time.time() < deadline:
        resp = client.get(f"/api/research/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        last_status = data["status"]
        if last_status in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未完成, 最后状态: {last_status}")


# =====================================================================
# 路由与状态机
# =====================================================================
def test_health(api_env):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "research-agent"


def test_submit_text_task_and_poll_to_done(api_env):
    """JSON 提交文本调研 → 任务最终 done 且报告非空、素材数>0。"""
    resp = client.post("/api/research", json={"query": "新能源汽车销量分析"})
    assert resp.status_code == 200
    task = resp.json()
    assert task["status"] in ("pending", "running")
    assert task["query"] == "新能源汽车销量分析"
    assert task["report"] is None

    final = _wait_task(task["task_id"])
    assert final["status"] == "done"
    assert "测试调研报告" in final["report"]
    assert final["materials_count"] >= 1
    assert final["rounds"] >= 1
    assert final["error"] is None
    assert final["finished_at"] is not None


def test_task_failure_records_error(api_env, monkeypatch):
    """LLM 全失败 → 任务 failed, error 字段含明确原因, 不抛 HTTP 异常。"""
    monkeypatch.setattr(server, "build_llm",
                        _make_fake_llm([RuntimeError("LLM 服务不可用(测试)")]))
    resp = client.post("/api/research", json={"query": "会失败的主题"})
    assert resp.status_code == 200
    final = _wait_task(resp.json()["task_id"])
    assert final["status"] == "failed"
    assert "LLM 服务不可用" in final["error"]


def test_get_unknown_task_returns_404(api_env):
    resp = client.get("/api/research/not-exist-id")
    assert resp.status_code == 404


def test_invalid_query_returns_422(api_env):
    """空 query 或超长 query 被 Pydantic 校验拦截(422)。"""
    resp = client.post("/api/research", json={"query": ""})
    assert resp.status_code == 422
    resp = client.post("/api/research", json={"query": "x" * 2001})
    assert resp.status_code == 422


def test_list_research_returns_recent_tasks(api_env):
    client.post("/api/research", json={"query": "主题A"})
    client.post("/api/research", json={"query": "主题B"})
    resp = client.get("/api/research?limit=10")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) >= 2
    # 新→旧排序: 列表第一项是最后提交的
    assert tasks[0]["query"] == "主题B"
    assert tasks[1]["query"] == "主题A"


# =====================================================================
# 文件上传
# =====================================================================
def test_submit_task_with_csv_file(api_env):
    """multipart 上传 CSV → 任务完成且素材数≥1(预读生效)。"""
    csv_bytes = b"year,sales\n2023,100\n2024,120\n2025,150\n"
    resp = client.post(
        "/api/research/with-files",
        data={"query": "分析上传的销量数据"},
        files=[("files", ("sales.csv", io.BytesIO(csv_bytes), "text/csv"))],
    )
    assert resp.status_code == 200
    task = resp.json()
    final = _wait_task(task["task_id"])
    assert final["status"] == "done"
    assert final["materials_count"] >= 1


def test_upload_too_large_file_returns_413(api_env):
    """超大文件被拒绝(413), 且不创建任务。"""
    big = b"x" * (server.MAX_FILE_SIZE_MB * 1024 * 1024 + 1)
    resp = client.post(
        "/api/research/with-files",
        data={"query": "大文件测试"},
        files=[("files", ("big.pdf", io.BytesIO(big), "application/pdf"))],
    )
    assert resp.status_code == 413
