# 临时全链路冒烟测试: planner -> tool -> reflection -> (loop) -> report
# 真实调用 DeepSeek LLM 与博查搜索(读取项目根目录 .env 配置)
import sys
import time

from graph_builder import build_graph, build_llm

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


def main():
    t0 = time.time()
    print(">>> 构建 LLM / 图 ...", flush=True)
    llm = build_llm()
    graph = build_graph(llm)
    print(">>> 开始执行 LangGraph 主流程 ...", flush=True)
    report = None
    for event in graph.stream(initial_state, stream_mode="updates"):
        for node_name, payload in event.items():
            if node_name.startswith("__"):
                continue
            for line in payload.get("steps_log") or []:
                print(f"[{node_name}] {line}", flush=True)
            if node_name == "report_node":
                report = payload.get("final_report")
    print(f">>> 主流程结束, 耗时 {time.time() - t0:.0f} 秒", flush=True)
    if report:
        print(">>> 报告前 800 字:\n" + str(report)[:800], flush=True)
    else:
        print("!!! 没有生成报告", flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
