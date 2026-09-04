"""
tests/_e2e_test.py —— 全链路冒烟测试: planner -> tool -> reflection -> (loop) -> report
真实调用 LLM 与博查搜索(读取项目根目录 .env 配置)。

⚠️ 注意: 本脚本会【真实消耗 LLM / 博查 API 额度】(每次跑通约 2~10 次接口调用),
请确认 Key 有额度再执行。

用法(任意目录均可, 脚本会自动把项目根目录加入 sys.path):
    python tests/_e2e_test.py

健壮性: 主流程带顶层异常捕获 —— 失败时打印完整错误, 并通过 LangGraph
checkpointer 尽力取回已搜集素材, 保存到 temp_upload/partial_*.json, 不会直接崩溃丢数据。
"""
import os
import sys
import time
import traceback
import uuid

# 允许从任意工作目录运行: 把项目根目录(本文件的上两级)加入 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from logging_setup import get_logger  # noqa: E402

_logger = get_logger("e2e_test")

QUERY = "Python 3.13 免费线程(free-threading)模式是什么, 如何开启?"

initial_state = {
    "user_query": QUERY,
    "sub_tasks": [],
    "collected_info": [],
    "reflection": "",
    "final_report": "",
    "iteration_count": 0,
    "uploaded_files": [],
    "steps_log": [],
}


def _save_partial(snapshot_values: dict, error_text: str) -> str:
    """异常兜底: 把 checkpointer 取回的素材落盘, 返回文件路径。"""
    import json

    save_dir = os.path.join(_PROJECT_ROOT, "temp_upload")
    os.makedirs(save_dir, exist_ok=True)
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": error_text[:500],
        "user_query": snapshot_values.get("user_query", QUERY),
        "sub_tasks": snapshot_values.get("sub_tasks") or [],
        "iteration_count": snapshot_values.get("iteration_count") or 0,
        "collected_info": snapshot_values.get("collected_info") or [],
    }
    fname = f"partial_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json"
    path = os.path.join(save_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def main() -> int:
    from langgraph.checkpoint.memory import InMemorySaver

    from graph_builder import build_graph, build_llm

    t0 = time.time()
    print(">>> 构建 LLM / 图(checkpointer 兜底已开启) ...", flush=True)
    llm = build_llm()
    checkpointer = InMemorySaver()
    thread_id = uuid.uuid4().hex[:12]
    config = {"configurable": {"thread_id": thread_id}}
    graph = build_graph(llm, checkpointer=checkpointer)
    print(">>> 开始执行 LangGraph 主流程 ...", flush=True)
    report = None
    try:
        for event in graph.stream(initial_state, config=config, stream_mode="updates"):
            for node_name, payload in event.items():
                if node_name.startswith("__"):
                    continue
                for line in payload.get("steps_log") or []:
                    print(f"[{node_name}] {line}", flush=True)
                if node_name == "report_node":
                    report = payload.get("final_report")
    except Exception as exc:  # noqa: BLE001 —— 顶层异常捕获: 保留已搜集素材
        error_text = f"{type(exc).__name__}: {exc}"
        print(f"!!! 主流程异常: {error_text}", flush=True)
        traceback.print_exc()
        try:
            snapshot = graph.get_state(config)
            values = getattr(snapshot, "values", None)
            if values is None and isinstance(snapshot, tuple) and snapshot:
                values = snapshot[0]
            materials = (values or {}).get("collected_info") or []
            if materials:
                path = _save_partial(values or {}, error_text)
                print(f">>> 已通过 checkpointer 保留 {len(materials)} 条已搜集素材: {path}", flush=True)
            else:
                print(">>> 异常前未产生任何素材, 无需保留", flush=True)
        except Exception as exc2:  # noqa: BLE001
            print(f"!!! 取回素材失败: {exc2}", flush=True)
        _logger.exception("e2e 主流程异常终止: %s", exc)
        return 1

    print(f">>> 主流程结束, 耗时 {time.time() - t0:.0f} 秒", flush=True)
    if report:
        print(">>> 报告前 800 字:\n" + str(report)[:800], flush=True)
        return 0
    print("!!! 没有生成报告", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
