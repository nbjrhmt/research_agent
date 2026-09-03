"""
graph_builder.py —— LangGraph 图构建: 定义 State、节点、边、条件路由

主流程(规格文档系统架构):
    START
      │
      ▼
    planner_node 规划节点: 把用户需求拆成子调研任务
      │
      ▼
    tool_node 工具调用节点: 每轮只调用一次工具(搜索/读PDF/执行代码), 素材累加, 轮次+1
      │
      ▼
    reflection_node 反思评估节点: 依据已有素材判断信息是否充足
      │
      ▼
    ┌─ 条件分支 ────────────────────────────────┐
    │ A) iteration_count >= 8      → 报告节点   │
    │ B) 反思标记【任务信息充足】    → 报告节点   │
    │ C) 信息不足且未到上限         → 回 tool_node│
    └──────────────────────────────────────────┘
      │
      ▼
    report_node 报告节点: 仅基于素材输出结构化调研报告
      │
      ▼
    END

另含: 读 .env 构建 LLM; 带重试的 JSON 输出解析(解析失败最多重试 2 次, 仍失败终止任务)。
"""
import json
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from state_schema import AgentState
from tools.code_exec_tool import exec_python_code
from tools.pdf_reader import read_pdf
from tools.search_tool import bocha_web_search

# ============================ 常量 ============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(BASE_DIR, "prompts")

load_dotenv(os.path.join(BASE_DIR, ".env"))  # 尽早读取 .env

MAX_ITERATIONS = 10     # 安全规则 #1: 工具调用最多 10 轮, 达到直接停止并生成报告
MAX_JSON_RETRY = 2      # 安全规则 #3: LLM JSON 解析失败最多重试 2 次, 重试失败终止任务

# 反思文本中出现这些字样之一 → 判定"信息充足", 进入报告节点
SUFFICIENT_MARKERS = (
    "任务信息充足", "信息已充足", "信息已经充足", "资料已充足",
    "资料足够", "无需继续搜索", "不需要继续搜集", "可以生成报告",
)

# 每轮最多拆解的子任务数
MAX_SUB_TASKS = 10


# ============================ 小工具函数 ============================
def _read_prompt_file(name: str) -> str:
    """读取 prompts/ 目录下的提示词文件"""
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as f:
        return f.read()


def build_llm() -> ChatOpenAI:
    """读取 .env 构建 LLM(OpenAI 及一切 OpenAI 兼容服务: DeepSeek/智谱/通义等)"""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key.startswith("你的"):
        raise RuntimeError("未配置 OPENAI_API_KEY: 请先编辑项目根目录的 .env 文件填入真实 Key, "
                           "然后刷新页面重新执行。")
    kwargs: Dict[str, Any] = {
        "model": os.getenv("LLM_MODEL", "").strip() or "gpt-4o-mini",
        "api_key": api_key,
        "temperature": 0.0,  # 调研整理场景要忠实素材, 关闭随机性
    }
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _to_text(content: Any) -> str:
    """兼容 ChatOpenAI 返回的两种 content 类型(str 或 消息块列表)"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _extract_json(text: str) -> dict:
    """从 LLM 输出中截取第一个 {...} 并解析(容忍前后有解释文字/代码围栏)"""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"输出中没有找到 JSON 对象, 输出片段: {text[:200]}")
    return json.loads(text[start:end + 1])


def _llm_json_ask(llm: ChatOpenAI, system_prompt: str, user_content: str) -> dict:
    """
    要求 LLM 只输出一个 JSON 对象; 解析失败自动重试, 最多重试 MAX_JSON_RETRY 次,
    全部失败则抛异常 → 终止整个任务(由 main.py 捕获并提示错误)。
    """
    last_error = "未知错误"
    for attempt in range(MAX_JSON_RETRY + 1):  # 首次尝试 + 2 次重试
        messages = [("system", system_prompt)]
        if attempt == 0:
            messages.append(("human", user_content))
        else:
            messages.append(("human", user_content + "\n\n【系统提醒】你上一次的输出不是合法 JSON"
                                                      f"(错误: {last_error})。请只输出一个 JSON 对象, "
                                                      "不要输出任何解释、Markdown 或额外文字。"))
        raw = _to_text(llm.invoke(messages).content)
        try:
            obj = _extract_json(raw)
            if not isinstance(obj, dict):
                raise ValueError("JSON 根节点必须是对象 {...}")
            return obj
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"LLM 工具调用 JSON 解析失败, 已重试 {MAX_JSON_RETRY} 次仍失败, 任务终止。"
                       f"最后一次错误: {last_error}")


def _clip(text: Any, limit: int) -> str:
    """把任意文本截断到 limit 字符(超长时末尾注明)"""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n……(已截断, 原内容共 {len(text)} 字符)"


def _join_material(materials: List[str], per_item: int = 2000, total: int = 40000) -> str:
    """把全部素材拼成一段提示词文本, 带编号并控制总长度"""
    parts: List[str] = []
    used = 0
    for i, material in enumerate(materials, start=1):
        piece = _clip(material, per_item)
        if used + len(piece) > total:
            parts.append(f"……(其余 {len(materials) - i + 1} 条素材因长度限制省略)")
            break
        parts.append(f"【素材{i}】\n{piece}")
        used += len(piece)
    return "\n\n".join(parts) if parts else "(暂无任何素材)"


# ============================ 节点函数 ============================
def make_planner_node(llm: ChatOpenAI):
    """① 规划节点: 大模型把需求拆成若干"用于搜集信息"的子任务, 不直接回答问题"""
    system_prompt = (
        "你是一个调研规划器。严格只输出一个 JSON 对象, 格式: "
        '{"sub_tasks": ["子任务1", "子任务2", "..."]}。'
        "子任务描述要具体到可以直接当作搜索关键词使用; 禁止编造任何知识; "
        "不要输出 JSON 以外的任何文字。"
    )

    def planner_node(state: AgentState) -> dict:
        user_query = str(state.get("user_query") or "").strip()
        prompt_text = _read_prompt_file("planner_prompt.txt").format(user_query=user_query)
        obj = _llm_json_ask(llm, system_prompt, prompt_text)

        raw_tasks = obj.get("sub_tasks") or []
        if isinstance(raw_tasks, str):  # 模型偶尔把数组写成字符串
            raw_tasks = [raw_tasks]
        tasks = [str(t).strip() for t in raw_tasks if str(t).strip()]
        tasks = tasks[:MAX_SUB_TASKS]
        if not tasks:  # 兜底: 拆不出任务就用原问题当搜索任务
            tasks = [user_query]

        logs = [f"规划节点: 需求已拆解为 {len(tasks)} 个子调研任务(仅用于搜集信息):"]
        logs += [f"  - {t}" for t in tasks]
        return {"sub_tasks": tasks, "steps_log": logs}

    return planner_node


def make_tool_node(llm: ChatOpenAI, web_search_tool=bocha_web_search):
    """② 工具调用节点: LLM 决策本轮调用哪个工具, 执行后把原始素材累加到 collected_info"""
    system_prompt = (
        "你是工具调度器, 负责为调研任务挑选工具搜集素材。\n"
        "可选工具(每次只调用一个):\n"
        "1) bocha_web_search —— 联网搜索(博查 Web Search API, 实时网页标题/链接/摘要), JSON 格式:\n"
        '   {"tool": "bocha_web_search", "query": "搜索关键词"}\n'
        "   注意: 若搜索失败(网络/Key/额度问题), 工具会返回以【工具异常】开头的说明文字, "
        "   此时不要编造内容, 可换关键词重试或先分析已有素材。\n"
        "2) read_pdf —— 重新读取本次上传的某个 PDF 文件, JSON 格式:\n"
        '   {"tool": "read_pdf", "filename": "xxx.pdf"}\n'
        "   注意: 上传文件在任务开始时已自动读取进素材, 一般不需要重复调用。\n"
        "3) exec_python_code —— 对上传的 CSV 做统计分析/绘图, JSON 格式:\n"
        '   {"tool": "exec_python_code", "code": "代码"}\n'
        "   代码里 pd、plt 已自动导入(DATA_DIR 是上传目录、SAVE_DIR 是图表保存目录), 禁止再写 import:\n"
        "     df = pd.read_csv(DATA_DIR + '/文件名.csv')\n"
        "     print(df.shape); print(df.columns.tolist()); print(df.head(10).to_string())\n"
        "     print(df.describe())\n"
        "     需要图表: df['列'].value_counts().plot(kind='bar'); plt.savefig(SAVE_DIR + '/chart.png')\n"
        "选工具的原则: 优先补最缺的信息; 有 CSV 且用户要求统计/计算/绘图时优先用第 3 个;\n"
        "资料不足时用第 1 个搜索(参考上一轮反思给出的关键词)。\n"
        "严格只输出上述三种格式之一的 JSON 对象, 不要输出任何其他文字。"
    )

    def tool_node(state: AgentState) -> dict:
        round_no = int(state.get("iteration_count") or 0) + 1          # 本轮是第几轮
        user_query = str(state.get("user_query") or "")
        sub_tasks = state.get("sub_tasks") or []
        reflection = str(state.get("reflection") or "(第一轮, 还没有反思意见)")
        materials = state.get("collected_info") or []
        uploaded_files = state.get("uploaded_files") or []

        # 只给 LLM 看最近 8 条素材的首行预览, 防止把全文再塞一遍浪费上下文
        preview_lines = []
        for m in materials[-8:]:
            first_line = str(m).splitlines()[0] if str(m).strip() else "(空素材)"
            preview_lines.append(f"    - {_clip(first_line, 150)}")

        user_content = f"""当前进度: 第 {round_no} / {MAX_ITERATIONS} 轮(达到上限必须停止)。
用户调研需求: {user_query}
子任务列表: {sub_tasks if sub_tasks else '(规划节点未返回, 按原需求执行)'}
上一轮反思意见: {reflection}
已有素材总条数: {len(materials)}
最近素材首行预览:
{chr(10).join(preview_lines) if preview_lines else '    (还没有素材)'}
本地上传文件清单(可分析对象): {uploaded_files if uploaded_files else '(本次没有上传文件)'}

请决策本轮唯一一次工具调用, 只输出 JSON。"""

        obj = _llm_json_ask(llm, system_prompt, user_content)
        tool_name = str(obj.get("tool") or "bocha_web_search").strip()
        logs: List[str] = []
        entry: str = ""

        try:
            if tool_name in ("bocha_web_search", "web_search"):
                query = str(obj.get("query") or user_query or "相关资料").strip()[:200]
                content = web_search_tool(query)
                entry = f"【素材-bocha_web_search】搜索关键词: {query}\n{content}"
                logs.append(f"工具调用(第 {round_no} 轮): bocha_web_search 「{_clip(query, 60)}」")
                logs.append(_clip(content, 300))

            elif tool_name == "read_pdf":
                filename = os.path.basename(str(obj.get("filename") or "").strip())
                if not filename or filename not in uploaded_files:
                    raise ValueError(f"文件名「{filename}」不在本次上传清单 {uploaded_files} 内, 只允许读取 temp_upload/ 中本次上传的文件。")
                content = read_pdf(filename)
                entry = f"【素材-read_pdf】文件: {filename}\n{content}"
                logs.append(f"工具调用(第 {round_no} 轮): read_pdf 「{filename}」")
                logs.append(_clip(content, 300))

            elif tool_name == "exec_python_code":
                code = str(obj.get("code") or "").strip()
                if not code:
                    raise ValueError("exec_python_code 缺少 code 参数, 必须给出要执行的代码。")
                content = exec_python_code(code)
                entry = f"【素材-exec_python_code】\n{content}"
                logs.append(f"工具调用(第 {round_no} 轮): exec_python_code(数据分析代码)")
                logs.append(_clip(content, 400))

            else:
                raise ValueError(f"未知工具名「{tool_name}」, 可选: bocha_web_search / read_pdf / exec_python_code。")
        except Exception as exc:  # 工具调用失败也要捕获, 异常转成素材文字, 不中断主流程
            entry = f"【素材-工具调用失败】第 {round_no} 轮, 工具 {tool_name} 调用失败: {type(exc).__name__}: {exc}"
            logs.append(entry)

        logs.append(f"本轮完成: 素材累计 {len(materials) + 1} 条")
        return {
            "collected_info": [entry],
            "iteration_count": round_no,   # 迭代计数器 += 1(由本节点统一写入)
            "steps_log": logs,
        }

    return tool_node


def make_reflection_node(llm: ChatOpenAI):
    """③ 反思评估节点: 只能依据已搜集素材, 判断信息是否充足 / 下一步还缺什么"""
    system_prompt = (
        "你是反思评估节点, 只能依据【已搜集资料】判断, 禁止编造任何资料中不存在的信息。\n"
        "若用户上传了文件且相关素材尚未获取/分析, 应视为信息不足。\n"
        "按给定模板回答即可, 不要额外输出 JSON。"
    )

    def reflection_node(state: AgentState) -> dict:
        round_no = int(state.get("iteration_count") or 0)
        user_query = str(state.get("user_query") or "")
        materials = state.get("collected_info") or []

        if not materials:
            reflection = "【信息不足】目前还没有搜集到任何素材, 需要立即开始搜集资料。"
        else:
            prompt_text = _read_prompt_file("reflection_prompt.txt").format(
                user_query=user_query,
                collected_info=_join_material(materials, per_item=2000, total=40000),
            )
            reflection = _to_text(llm.invoke([("system", system_prompt), ("human", prompt_text)]).content)

        reflection = reflection.strip() or "(反思节点未返回内容, 默认视为信息不足。)"
        reflection = _clip(reflection, 4000)

        sufficient = any(marker in reflection for marker in SUFFICIENT_MARKERS)
        verdict = "✅ 反思结论: 信息充足, 下一跳进入报告节点。" if sufficient \
            else f"⚠️ 反思结论: 信息不足, 回到工具节点继续搜集(已进行 {round_no}/{MAX_ITERATIONS} 轮)。"
        return {
            "reflection": reflection,
            "steps_log": [f"反思节点(第 {round_no} 轮后): {verdict}", _clip(reflection, 500)],
        }

    return reflection_node


def make_report_node(llm: ChatOpenAI):
    """④ 报告节点: 仅从已搜集素材提取/归纳/整理/分析, 严禁编造, 输出结构化 Markdown 报告"""
    system_prompt = (
        "你是报告生成节点。只允许使用给定素材中的信息, 严禁编造任何素材中不存在的事实。\n"
        "输出结构清晰的 Markdown 调研报告, 可包含: 标题层级、要点列表、对比表格、来源链接引用。\n"
        "若素材中存在【工具异常】【搜索失败】【安全拦截】等失败记录, 请在第一章节'资料获取情况'中如实说明, "
        "不要用猜测填补缺失信息。不要输出素材之外的任何内容。"
    )

    def report_node(state: AgentState) -> dict:
        user_query = str(state.get("user_query") or "")
        materials = state.get("collected_info") or []
        prompt_text = _read_prompt_file("report_prompt.txt").format(
            user_query=user_query,
            collected_info=_join_material(materials, per_item=3000, total=60000),
        )
        report = _to_text(llm.invoke([("system", system_prompt), ("human", prompt_text)]).content).strip()
        if not report:
            raise RuntimeError("报告节点没有生成任何内容, 任务终止(可重试)。")
        return {
            "final_report": report,
            "steps_log": [f"报告节点: 已基于 {len(materials)} 条素材生成调研报告, 任务完成。"],
        }

    return report_node


def route_after_reflection(state: AgentState) -> str:
    """
    反思节点之后的条件分支:
        A) iteration_count >= 8        → 报告节点
        B) 反思标记信息充足           → 报告节点
        C) 信息不足且未到上限         → 回到工具节点
    """
    if int(state.get("iteration_count") or 0) >= MAX_ITERATIONS:
        return "report_node"
    reflection = str(state.get("reflection") or "")
    if any(marker in reflection for marker in SUFFICIENT_MARKERS):
        return "report_node"
    return "tool_node"


def build_graph(llm: ChatOpenAI, web_search_tool=bocha_web_search):
    """
    组装并编译 LangGraph: planner → tool → reflection ⇄ (tool) → report

    :param llm:            已配置好的 ChatOpenAI 实例
    :param web_search_tool: 联网搜索可调用对象(query 为唯一位置参数, 返回素材文本);
                            默认使用 tools.search_tool.bocha_web_search(博查 API),
                            测试时可注入假搜索便于离线验证
    """
    graph = StateGraph(AgentState)

    graph.add_node("planner_node", make_planner_node(llm))
    graph.add_node("tool_node", make_tool_node(llm, web_search_tool=web_search_tool))
    graph.add_node("reflection_node", make_reflection_node(llm))
    graph.add_node("report_node", make_report_node(llm))

    graph.add_edge(START, "planner_node")
    graph.add_edge("planner_node", "tool_node")
    graph.add_edge("tool_node", "reflection_node")
    graph.add_conditional_edges(
        "reflection_node",
        route_after_reflection,
        {"tool_node": "tool_node", "report_node": "report_node"},
    )
    graph.add_edge("report_node", END)

    # ---------------- 长期记忆接入点(可选模块, MVP 先注释) ----------------
    # 主流程跑通后, 在这里补记忆检索/保存钩子, 例如:
    #   任务开始前: 用 MemoryStore.search(user_query) 找到相似历史素材 → 预置进 collected_info
    #   任务结束后: store.save_run(user_query, materials, report)
    return graph.compile()
