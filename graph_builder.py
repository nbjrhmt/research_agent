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
    reflection_node 反思评估节点: 结构化判断(JSON: sufficient/missing_topics)信息是否充足
      │
      ▼
    ┌─ 条件分支 ────────────────────────────────┐
    │ A) iteration_count >= MAX_ITERATIONS → 报告│
    │ B) 反思判定 sufficient=true        → 报告  │
    │ C) 信息不足且未到上限              → tool  │
    └──────────────────────────────────────────┘
      │
      ▼
    report_node 报告节点: 仅基于素材输出结构化调研报告(结论必须标注素材编号+来源)
      │
      ▼
    END

2026 加固说明(与主流程兼容):
    1. LLM 调用统一走 _invoke_llm: 客户端超时(request_timeout) + 瞬时错误(网络/
       429限流/5xx)简单指数退避重试, 全部失败才抛出终止任务;
    2. 素材拼接由"纯字符截断"升级为"真实 token 计数(tiktoken)预算裁剪", LLM 输出
       增加 max_tokens 上限, 并按上下文预算做余量告警;
    3. 反思节点改为结构化 JSON 判定 {sufficient, reason, missing_topics};
       【P0-1 时序契约: 先判断、后截断】——充足性判断基于全量素材("判断信封"随素材
       全量动态扩容, 只有越过模型上下文硬预算才截断判断输入), 展示文本的截断严格
       发生在判定完成之后, 杜绝素材提前截断导致误判信息不足;
       【P0-3 失败兜底: 先内部重试、后降级、防空转】——JSON 解析失败先在节点内部重试
       REFLECT_JSON_RETRY 次反思请求, 重试失败才降级为信息不足; 连续失败达到
       MAX_REFLECT_FAILURES 后强制进入报告节点, 不再空耗迭代轮次;
    4. 全部 system prompt 抽离到 prompts/*.txt; 报告提示词强制要求逐条结论标注
       【素材N】编号与来源 URL;
    5. 素材新增去重(文本级, P2 计划升级语义去重); LangGraph checkpointer 可选接线: 传入
       持久化 saver(如 core/checkpoint_store 封装的 SqliteSaver, 本地 agent_checkpoints.db)
       即可在进程重启后恢复未完成的调研会话; 不传 checkpointer 时维持原内存模式
       (任务异常时保留已搜集素材, 重启即失, 见 build_graph 注释)。
"""
import json
import os
import time
from typing import Any, Dict, List

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from core.config import env_flag, env_float, env_int  # 统一环境变量解析(2026 重构 P1)
from logging_setup import get_logger
from state_schema import AgentState
from tools.code_exec_tool import exec_python_code
from tools.pdf_reader import read_pdf
from tools.search_tool import bocha_web_search

_logger = get_logger("graph_builder")

# ============================ 常量 ============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(BASE_DIR, "prompts")

load_dotenv(os.path.join(BASE_DIR, ".env"))  # 尽早读取 .env

DEFAULT_MODEL = "gpt-4o-mini"  # 默认模型名(唯一出处; main.py 侧边栏展示也引用本常量)

MAX_ITERATIONS = 10     # 安全规则 #1: 工具调用最多 10 轮, 达到直接停止并生成报告
MAX_JSON_RETRY = 2      # 安全规则 #3: LLM JSON 解析失败最多重试 2 次, 重试失败终止任务

# ---- P0-3 加固: 反思节点 JSON 解析失败的"内部重试反思请求"机制 ----
# 反思节点单独走自己的重试参数(与通用 MAX_JSON_RETRY 机制相同, 独立命名便于单节点控制):
#   ① 先做 1~2 次内部重试反思请求(纠正模型输出非法 JSON);
#   ② 重试仍失败才降级为"信息不足"继续搜集;
#   ③ 连续降级达到 MAX_REFLECT_FAILURES 后强制进入报告节点, 避免把 MAX_ITERATIONS
#      轮次全部空耗在"每次反思都解析失败"的无意义循环上(浪费 LLM API 额度)。
REFLECT_JSON_RETRY = 2    # 反思 JSON 解析失败后, 内部重试反思请求的次数(首次 + 2 次 = 至多 3 次请求)
MAX_REFLECT_FAILURES = 2  # 连续结构化判定失败上限: 达到后条件路由强制进入 report_node

# 每轮最多拆解的子任务数
MAX_SUB_TASKS = 10

# ---- LLM 调用统一配置(可用 .env 覆盖, 见 .env.example; 解析统一走 core.config) ----
LLM_TIMEOUT = env_float("LLM_TIMEOUT", 60.0)            # 单次请求超时(秒)
LLM_MAX_OUTPUT_TOKENS = env_int("LLM_MAX_OUTPUT_TOKENS", 8192)  # 输出 token 上限
LLM_CLIENT_RETRIES = env_int("LLM_CLIENT_RETRIES", 2)   # SDK 客户端层重试次数
LLM_RETRY_MAX = env_int("LLM_MAX_RETRIES", 3)           # 应用层统一重试次数(含简单退避)
LLM_CONTEXT_TOKENS = env_int("LLM_CONTEXT_TOKENS", 60000)  # 估算上下文窗口(token)

# ---- 素材 token 预算(反思/报告节点单次喂给模型的素材量) ----
# P0-1 加固说明: 反思节点"先判断、后截断"——充足性判定阶段的素材预算不再是固定 30000
# 上限, 而是按素材全量动态计算的"判断信封"(见 _reflection_judgment_envelope):
# 全量素材放得进模型上下文硬预算就一律不截断地交给反思判断, 只有越过硬预算才截断,
# 避免素材提前截断导致 LLM 误判"信息不足"而空转搜集轮次。
_MATERIAL_REFLECT_ITEM_TOKENS = 2000    # 参考值(历史单条上限): 反思判断信封内单条不再单独前置限幅
_MATERIAL_REFLECT_TOTAL_TOKENS = 30000  # 反思判断信封的下限基线(低于此基线不因省钱而截断)
_MATERIAL_REPORT_ITEM_TOKENS = 3000     # 报告: 单条素材最多保留
_MATERIAL_REPORT_TOTAL_TOKENS = 40000   # 报告: 素材合计最多保留


def _context_cap() -> int:
    """按估算上下文窗口与输出上限, 计算单次 prompt 可安全使用的素材预算上限。"""
    return max(8000, LLM_CONTEXT_TOKENS - LLM_MAX_OUTPUT_TOKENS - 2000)


# ============================ 日志与提示词加载 ============================
_PROMPT_CACHE: Dict[str, str] = {}


def _load_prompt(name: str) -> str:
    """统一模板加载: 读取 prompts/ 下模板文件(带缓存, 所有节点共用入口)。"""
    if name in _PROMPT_CACHE:
        return _PROMPT_CACHE[name]
    path = os.path.join(PROMPT_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise RuntimeError(f"缺少提示词模板文件 prompts/{name}, 请检查 prompts/ 目录完整性。")
    _PROMPT_CACHE[name] = text
    return text


# ============================ token 计数工具 ============================
_TOKEN_ENCODING_NAME = "cl100k_base"
_enc_cache: Dict[str, Any] = {}
_enc_warned = False


def _get_encoding(model_name: str | None = None):
    """获取 tiktoken 编码器(模型已知时优先按模型取, 失败回退默认编码)。"""
    key = model_name or _TOKEN_ENCODING_NAME
    if key in _enc_cache:
        return _enc_cache[key]
    global _enc_warned
    enc = None
    try:
        import tiktoken

        if model_name:
            try:
                enc = tiktoken.encoding_for_model(model_name)
            except Exception:
                enc = tiktoken.get_encoding(_TOKEN_ENCODING_NAME)
        else:
            enc = tiktoken.get_encoding(_TOKEN_ENCODING_NAME)
    except Exception:
        enc = None
        if not _enc_warned:
            _enc_warned = True
            _logger.warning("tiktoken 不可用, token 计数降级为字符估算(建议 pip install -r requirements.txt)")
    _enc_cache[key] = enc
    return enc


def _count_tokens(text: str, model_name: str | None = None) -> int | None:
    """真实 token 计数; tiktoken 不可用时返回 None(由调用方降级估算)。"""
    enc = _get_encoding(model_name)
    if enc is None:
        return None
    try:
        return len(enc.encode(text or "", disallowed_special=()))
    except Exception:
        return None


def _estimate_tokens(text: str) -> int:
    """无 tiktoken 时的粗略估算: CJK 字符约 1 token/字, ASCII 约 4 字符/token。"""
    text = text or ""
    cjk = sum(1 for ch in text if ord(ch) > 127)
    ascii_len = len(text) - cjk
    return max(1, int(cjk + ascii_len / 4.0))


def _token_len(text: str, model_name: str | None = None) -> int:
    tokens = _count_tokens(text, model_name)
    return tokens if tokens is not None else _estimate_tokens(text)


def _truncate_to_tokens(text: str, limit_tokens: int, model_name: str | None = None) -> str:
    """按真实 token 预算裁剪单条素材(字符级二分逼近, 裁剪后保留说明尾注)。"""
    text = text or ""
    if len(text) <= 0:
        return text
    total = _count_tokens(text, model_name)
    if total is None:  # 无 tiktoken: 按字符估算裁剪
        char_limit = max(1, limit_tokens * 4)
        if len(text) <= char_limit:
            return text
        cut = text[:char_limit]
        return cut + f"\n……(已截断: 原内容约 {len(text)} 字符, 按 token 预算约保留前 {limit_tokens} tokens)"
    if total <= limit_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # 二分查找 token 数不超过上限的最大前缀
        mid = (lo + hi + 1) // 2
        if (_count_tokens(text[:mid], model_name) or 0) <= limit_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:max(lo, 1)]
    return cut + f"\n……(已截断: 原内容共 {len(text)} 字符 / 约 {total} tokens, 按 token 预算仅保留前 {limit_tokens} tokens)"


def _join_material(materials: List[str], per_item_tokens: int, total_tokens: int,
                   model_name: str | None = None) -> str:
    """把全部素材拼成一段提示词文本, 带编号并【按真实 token 预算】控制总长度。

    单条素材超预算 → 先逐条裁剪; 合计超预算 → 停止并注明省略条数。
    """
    total_tokens = max(1000, min(total_tokens, _context_cap()))
    parts: List[str] = []
    used = 0
    dropped = 0
    for i, material in enumerate(materials, start=1):
        piece = _truncate_to_tokens(material, per_item_tokens, model_name)
        piece_tokens = _token_len(piece, model_name)
        if used + piece_tokens > total_tokens:
            dropped = len(materials) - i + 1
            break
        parts.append(f"【素材{i}】\n{piece}")
        used += piece_tokens
    text = "\n\n".join(parts) if parts else "(暂无任何素材)"
    if dropped:
        text += f"\n……(其余 {dropped} 条素材因上下文 token 预算省略)"
    return text


def _log_prompt_budget(node_name: str, prompt_text: str, model_name: str | None = None) -> None:
    """上下文余量告警: 素材 token 用量接近/超出安全预算时记录日志。"""
    used = _token_len(prompt_text, model_name)
    safety = max(4000, LLM_CONTEXT_TOKENS - LLM_MAX_OUTPUT_TOKENS - 2000)
    ratio = used / safety if safety > 0 else 1.0
    _logger.info("%s 节点素材/提示词约 %s tokens(安全预算 %s)", node_name, used, safety)
    if ratio >= 0.9:
        _logger.warning(
            "%s 节点上下文余量不足: 已用约 %s/%s tokens, 输出预算 %s tokens。"
            "建议调低素材预算或增大 LLM_CONTEXT_TOKENS/换更大上下文模型。",
            node_name, used, safety, LLM_MAX_OUTPUT_TOKENS,
        )


# ============================ P0-1: 反思"先判断、后截断"时序专用工具 ============================
def _corpus_token_total(materials: List[str], model_name: str | None = None) -> int:
    """素材全量无损盘点: 逐条真实 token 计数并求和, 【不做任何截断】。

    反思节点在"充足性判断"前先调用本函数盘点全量素材规模,
    用于决定判断信封——保证判断基于尽可能完整的素材, 而不是被提前截断的片段。
    """
    total = 0
    for m in materials or []:
        text = str(m or "")
        if not text.strip():
            continue
        total += _token_len(text, model_name)
    return total


def _reflection_judgment_envelope(full_tokens: int) -> int:
    """计算反思充足性判断的"判断信封"(token)。

    P0-1 时序契约: 先判断(充足性) → 后截断(展示文本)。
    本函数保证判断阶段不被"软预算"提前截断素材:
        - 信封下限 = _MATERIAL_REFLECT_TOTAL_TOKENS(基础预算);
        - 若素材全量 token 数在模型上下文硬预算(_context_cap)以内, 信封直接扩到全量
          (+400 余量覆盖编号/标题格式开销), 即判断输入不截断任何素材;
        - 只有全量素材确实超过模型上下文硬预算时, 判断输入才做截断
          (截断发生在唯一不可避免的硬上限处, 并会注明省略条数, 由轮次上限兜底)。
    """
    return min(_context_cap(), max(_MATERIAL_REFLECT_TOTAL_TOKENS, int(full_tokens) + 400))


def _is_duplicate_material(entry: str, materials: List[str]) -> bool:
    """素材去重判定: 与已有素材压缩全部空白后完全一致视为重复(纯文本比对)。

    P2 待优化点(本版本只记录、不实现): 仅文本比对无法识别"语义近似但写法不同"的素材,
    后续可引入 embedding 相似度(如阈值 0.92)做语义去重, 见 CHANGELOG P2-5。
    """
    if not (entry or "").strip():
        return False
    normalized = _normalize(entry)
    return any(_normalize(m) == normalized for m in (materials or []))


# ============================ 小工具函数 ============================
# 注: 环境变量解析已统一收敛到 core/config.py(env_str/env_int/env_float/env_flag,
# 2026 重构 P1), 旧版本文件内的 _env_int/_env_flag 重复实现已删除; 本文件常量
# 全部保留为模块级属性(测试可 monkeypatch), 仅数据源改为 core.config 解析函数。

# 是否允许 Agent 调用代码沙盒工具(exec_python_code)。可运行"仅基于搜索/PDF 素材调研"的
# 纯只读模式: .env 里设 ALLOW_CODE_EXEC=false 即可整体关闭沙盒代码执行(见 README FAQ)。
ALLOW_CODE_EXEC = env_flag("ALLOW_CODE_EXEC", True)


def build_llm() -> ChatOpenAI:
    """读取 .env 构建 LLM(OpenAI 及一切 OpenAI 兼容服务: DeepSeek/智谱/通义等)。

    统一配置: request_timeout 超时、max_tokens 输出上限、max_retries 客户端重试,
    均可由 .env 覆盖(LLM_TIMEOUT / LLM_MAX_OUTPUT_TOKENS / LLM_CLIENT_RETRIES)。
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key.startswith("你的"):
        raise RuntimeError("未配置 OPENAI_API_KEY: 请先编辑项目根目录的 .env 文件填入真实 Key, "
                           "然后刷新页面重新执行。")
    kwargs: Dict[str, Any] = {
        "model": os.getenv("LLM_MODEL", "").strip() or DEFAULT_MODEL,
        "api_key": api_key,
        "temperature": 0.0,  # 调研整理场景要忠实素材, 关闭随机性
        "request_timeout": LLM_TIMEOUT,          # 单次请求超时, 防慢接口长时间卡死
        "max_tokens": LLM_MAX_OUTPUT_TOKENS,     # 输出 token 上限(报告防超长/防上下文溢出)
        "max_retries": LLM_CLIENT_RETRIES,       # SDK 客户端层重试(连接/429/5xx 自动退避)
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


def _invoke_llm(llm: ChatOpenAI, messages: list) -> str:
    """统一 LLM 调用入口: 带超时(客户端配置) + 瞬时错误简单指数退避重试。

    覆盖场景: 网络抖动 / 429 限流 / 5xx 服务端错误 / 超时;
    最多重试 LLM_RETRY_MAX 次(1s/2s/4s…退避, 上限 8s), 全部失败抛出最后一次异常,
    由调用方(main.py 顶层兜底)提示任务终止。
    """
    attempt = 0
    while True:
        try:
            return _to_text(llm.invoke(messages).content)
        except Exception as exc:  # noqa: BLE001 —— 网络/限流/服务端错误统一重试
            attempt += 1
            if attempt > LLM_RETRY_MAX:
                _logger.exception("LLM 调用重试 %s 次后仍失败, 终止本次调用", LLM_RETRY_MAX)
                raise
            delay = min(2 ** attempt, 8.0)
            _logger.warning(
                "LLM 调用失败(%s), 第 %s/%s 次重试, %.1fs 后重试: %s: %s",
                type(exc).__name__, attempt, LLM_RETRY_MAX, delay,
                type(exc).__name__, str(exc)[:200],
            )
            time.sleep(delay)


def _extract_json(text: str) -> dict:
    """从 LLM 输出中截取第一个 {...} 并解析(容忍前后有解释文字/代码围栏)"""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"输出中没有找到 JSON 对象, 输出片段: {text[:200]}")
    return json.loads(text[start:end + 1])


def _llm_json_ask(llm: ChatOpenAI, system_prompt: str, user_content: str,
                  max_retries: int = MAX_JSON_RETRY) -> dict:
    """
    要求 LLM 只输出一个 JSON 对象; JSON 解析失败自动"内部重试"最多 max_retries 次
    (每次重试都会附带上次解析错误提醒, 纠正模型输出), 全部失败则抛异常(部分节点自行兜底)。

    :param max_retries: 解析失败后的内部重试次数。默认 MAX_JSON_RETRY(安全规则 #3);
                        反思节点按 P0-3 显式传入 REFLECT_JSON_RETRY, 保证"解析失败先内部
                        重试反思请求, 重试失败后才降级为信息不足"。
    """
    last_error = "未知错误"
    for attempt in range(max_retries + 1):  # 首次尝试 + max_retries 次内部重试
        messages = [("system", system_prompt)]
        if attempt == 0:
            messages.append(("human", user_content))
        else:
            messages.append(("human", user_content + "\n\n【系统提醒】你上一次的输出不是合法 JSON"
                                                      f"(错误: {last_error})。请只输出一个 JSON 对象, "
                                                      "不要输出任何解释、Markdown 或额外文字。"))
        raw = _invoke_llm(llm, messages)
        try:
            obj = _extract_json(raw)
            if not isinstance(obj, dict):
                raise ValueError("JSON 根节点必须是对象 {...}")
            return obj
        except Exception as exc:  # noqa: BLE001 —— JSON 解析失败记录后走内部重试分支
            last_error = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"LLM 工具调用 JSON 解析失败, 已重试 {max_retries} 次仍失败, 任务终止。"
                       f"最后一次错误: {last_error}")


def _clip(text: Any, limit: int) -> str:
    """把任意文本截断到 limit 字符(超长时末尾注明)"""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n……(已截断, 原内容共 {len(text)} 字符)"


def _normalize(text: Any) -> str:
    """归一化素材文本(压缩空白/换行), 用于素材去重比较。"""
    return "".join(str(text or "").split())


def _coerce_bool(value: Any) -> bool:
    """把 LLM 结构化输出里的 sufficient 字段规整为 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是", "充足", "足够")
    return False


def _clean_keywords(value: Any, max_items: int = 8, max_len: int = 200) -> List[str]:
    """规整反思输出的 missing_topics 列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    out = []
    for item in items:
        s = str(item or "").strip()
        if s:
            out.append(s[:max_len])
        if len(out) >= max_items:
            break
    return out


# ============================ 节点函数 ============================
def make_planner_node(llm: ChatOpenAI):
    """① 规划节点: 大模型把需求拆成若干"用于搜集信息"的子任务, 不直接回答问题"""
    system_prompt = _load_prompt("planner_system.txt")

    def planner_node(state: AgentState) -> dict:
        user_query = str(state.get("user_query") or "").strip()
        prompt_text = _load_prompt("planner_prompt.txt").format(user_query=user_query)
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
    system_prompt = _load_prompt("tool_system.txt")
    if not ALLOW_CODE_EXEC:  # P1: 管理员通过 .env 关闭沙盒代码执行后, 提示词层面先劝阻模型
        system_prompt += ("\n\n注意: 本次运行环境已由管理员关闭代码执行工具(ALLOW_CODE_EXEC=false), "
                          "禁止选择 exec_python_code, 请只使用 bocha_web_search / read_pdf。")

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
                if not ALLOW_CODE_EXEC:  # 已通过 .env(ALLOW_CODE_EXEC=false)整体关闭沙盒执行
                    raise ValueError("exec_python_code 已被管理员关闭(ALLOW_CODE_EXEC=false), "
                                     "本轮不执行任何代码, 请改用搜索或分析已有素材。")
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

        # 素材去重: 与已有素材完全同质(压缩空白后)的新条目直接跳过, 避免重复内容占用上下文
        if entry:
            if _is_duplicate_material(entry, materials):
                logs.append("本轮工具结果与已有素材重复, 已自动去重(本轮仍计入轮次, 由反思判断是否继续)")
                return {"collected_info": [], "iteration_count": round_no, "steps_log": logs}

        logs.append(f"本轮完成: 素材累计 {len(materials) + 1} 条")
        return {
            "collected_info": [entry],
            "iteration_count": round_no,   # 迭代计数器 += 1(由本节点统一写入)
            "steps_log": logs,
        }

    return tool_node


def make_reflection_node(llm: ChatOpenAI):
    """
    ③ 反思评估节点: 只能依据已搜集素材判断; 输出结构化 JSON
       {sufficient, reason, missing_topics}。

    ★ P0-1 时序契约(严格先判断、后截断, 禁止先截断再判断):
        阶段一【判断】: 先对素材做"全量无损盘点"(不截断), 无素材直接判定信息不足;
                       有素材则按"判断信封"(全量素材放得进模型上下文硬预算就全量给到,
                       只有越过硬预算才截断)调用 LLM 做结构化充足性判定;
        阶段二【截断】: 充足性判定全部完成后, 才允许对"展示用文本"(reflection 字段/
                       日志)做 _clip 截断——素材本体与判定输入绝不在判断前被截断。

    ★ P0-3 失败兜底(先内部重试、后降级、防空转):
        JSON 解析失败先做 REFLECT_JSON_RETRY 次"内部重试反思请求"(纠正模型输出),
        重试仍失败才降级为"信息不足"继续搜集; 连续降级次数(reflection_failures)
        达到 MAX_REFLECT_FAILURES 后, 条件路由强制进入报告节点, 不再把
        MAX_ITERATIONS 轮次空耗在必然失败的反思上(节省 LLM API 额度)。
    """
    system_prompt = _load_prompt("reflection_system.txt")

    def reflection_node(state: AgentState) -> dict:
        round_no = int(state.get("iteration_count") or 0)
        user_query = str(state.get("user_query") or "")
        materials = state.get("collected_info") or []
        model_name = getattr(llm, "model_name", None)
        prev_failures = int(state.get("reflection_failures") or 0)

        # ============ 阶段一: 判断 ============
        # (1) 素材全量无损盘点: 只统计 token 规模, 绝不截断任何素材
        full_tokens = _corpus_token_total(materials, model_name)

        if full_tokens == 0:
            # 没有素材(或全为空字符串): 离线即可判定信息不足, 不发 LLM 请求、不截断
            sufficient = False
            failures = 0
            reason = "目前还没有搜集到任何素材, 需要立即开始搜集资料。"
            missing_topics: List[str] = [user_query or "相关资料"]
        else:
            # (2) 先判断: 按"判断信封"组 prompt —— 全量素材在硬预算内则不截断任何素材
            envelope = _reflection_judgment_envelope(full_tokens)
            prompt_text = _load_prompt("reflection_prompt.txt").format(
                user_query=user_query,
                collected_info=_join_material(
                    materials,
                    per_item_tokens=envelope,   # 判断信封内单条不再前置限幅(素材整体是判断对象)
                    total_tokens=envelope,
                    model_name=model_name,
                ),
            )
            _log_prompt_budget("reflection", prompt_text, model_name)
            try:
                # P0-3: JSON 解析失败先在节点内部重试反思请求 REFLECT_JSON_RETRY 次
                obj = _llm_json_ask(llm, system_prompt, prompt_text,
                                    max_retries=REFLECT_JSON_RETRY)
                sufficient = _coerce_bool(obj.get("sufficient"))
                failures = 0  # 判定成功: 连续失败计数清零
                reason = str(obj.get("reason") or "").strip()[:1000]
                missing_topics = _clean_keywords(obj.get("missing_topics"))
            except Exception as exc:  # noqa: BLE001
                # 内部重试全部失败才降级: 本轮按"信息不足"继续搜集(避免误停), 失败计数 +1
                failures = prev_failures + 1
                sufficient = False
                _logger.exception("反思节点结构化判定失败(内部已重试 %s 次), 第 %s 次连续失败, "
                                  "本轮按'信息不足'继续搜集", REFLECT_JSON_RETRY, failures)
                reason = (f"(反思结构化输出失败: {type(exc).__name__}, 内部重试 "
                          f"{REFLECT_JSON_RETRY} 次后仍失败, 已按信息不足处理)")
                missing_topics = []

        # ============ 阶段二: 充足性判定已完成, 下面才允许生成/截断展示文本 ============
        if sufficient:
            reflection_text = f"反思结论：信息充足。{reason}"
            verdict = "✅ 反思结论: 信息充足, 下一跳进入报告节点。"
        else:
            kw_text = "、".join(missing_topics[:5]) or "与调研主题相关的资料"
            reflection_text = f"反思结论：信息不足。{reason}\n建议继续搜索关键词: {kw_text}"
            if failures >= MAX_REFLECT_FAILURES:
                # 连续失败达到上限: 由 route_after_reflection 强制进入报告节点(防空转)
                verdict = (f"⛔ 反思结论: 结构化判定连续失败 {failures}/{MAX_REFLECT_FAILURES} 次, "
                           "为避免空耗迭代轮次, 将基于现有素材进入报告节点。")
                reflection_text += ("\n(系统提示: 反思判定已连续失败多次, 为节省 API 额度本轮起"
                                    "不再继续搜集, 直接基于现有素材生成报告。)")
            else:
                verdict = (f"⚠️ 反思结论: 信息不足, 回到工具节点继续搜集"
                           f"(已进行 {round_no}/{MAX_ITERATIONS} 轮)。")

        reflection = _clip(reflection_text, 4000)  # 判定完成后才截断展示文本(先判断、后截断)
        return {
            "reflection": reflection,
            "reflection_sufficient": bool(sufficient),  # 结构化判定结果供路由直接使用
            "reflection_failures": failures,            # 连续判定失败计数(成功自动清零)
            "steps_log": [f"反思节点(第 {round_no} 轮后): {verdict}", _clip(reflection, 500)],
        }

    return reflection_node


def make_report_node(llm: ChatOpenAI):
    """
    ④ 报告节点: 仅从已搜集素材提取/归纳/整理/分析, 严禁编造;
       报告提示词强制要求每条结论标注素材编号【素材N】与来源 URL。
    """
    system_prompt = _load_prompt("report_system.txt")

    def report_node(state: AgentState) -> dict:
        user_query = str(state.get("user_query") or "")
        materials = state.get("collected_info") or []
        model_name = getattr(llm, "model_name", None)
        prompt_text = _load_prompt("report_prompt.txt").format(
            user_query=user_query,
            collected_info=_join_material(
                materials,
                per_item_tokens=_MATERIAL_REPORT_ITEM_TOKENS,
                total_tokens=_MATERIAL_REPORT_TOTAL_TOKENS,
                model_name=model_name,
            ),
        )
        _log_prompt_budget("report", prompt_text, model_name)
        report = _invoke_llm(llm, [("system", system_prompt), ("human", prompt_text)]).strip()
        if not report:
            raise RuntimeError("报告节点没有生成任何内容, 任务终止(可重试)。")
        return {
            "final_report": report,
            "steps_log": [f"报告节点: 已基于 {len(materials)} 条素材生成调研报告(结论均标注素材编号), 任务完成。"],
        }

    return report_node


def route_after_reflection(state: AgentState) -> str:
    """
    反思节点之后的条件分支(检查顺序自上而下, 命中即返回):
        A) iteration_count >= MAX_ITERATIONS → 报告节点(强制轮次上限)
        B) reflection_failures >= MAX_REFLECT_FAILURES → 报告节点(P0-3: 反思判定连续失败,
           不再空耗剩余轮次, 基于现有素材直接出报告, 节约 LLM API 额度)
        C) 反思结构化判定 reflection_sufficient=true → 报告节点
        D) 信息不足(或判定缺失)且未到上限 → 回到工具节点
    """
    if int(state.get("iteration_count") or 0) >= MAX_ITERATIONS:
        return "report_node"
    if int(state.get("reflection_failures") or 0) >= MAX_REFLECT_FAILURES:
        return "report_node"
    if state.get("reflection_sufficient") is True:
        return "report_node"
    return "tool_node"


def build_graph(llm: ChatOpenAI, web_search_tool=bocha_web_search, checkpointer=None):
    """
    组装并编译 LangGraph: planner → tool → reflection ⇄ (tool) → report

    :param llm:            已配置好的 ChatOpenAI 实例
    :param web_search_tool: 联网搜索可调用对象(query 为唯一位置参数, 返回素材文本);
                            默认使用 tools.search_tool.bocha_web_search(博查 API),
                            测试时可注入假搜索便于离线验证
    :param checkpointer:   可选的 LangGraph checkpointer, 传入后编译为可断点/持久化恢复的图:
                           - 不传(默认 None): 纯内存运行, 无断点语义 —— 与本函数历史行为
                             完全一致(旧调用方零影响);
                           - 传 InMemorySaver: 可断点取回状态(main.py 异常兜底用), 重启即失;
                           - 传持久化 saver(如 core/checkpoint_store 封装的 SqliteSaver →
                             本地 agent_checkpoints.db): 中间状态落盘 sqlite, 进程/服务重启后
                             仍可按同一 thread_id 恢复继续未完成的调研会话(main.py 断点续研,
                             2026 增量实现; 详见 core/checkpoint_store.py 与 README)

    ★ 持久化边界备注(见 README「已知项目局限」): 本图不做任何持久化约定, 持久化与否
      完全取决于调用方传入的 checkpointer:
      - InMemorySaver: 会话状态只保存在单进程内存 —— Streamlit 服务重启/进程退出后
        图运行状态即丢失, 仅适合单机演示; 任务中途异常的素材靠 _save_partial_run
        落盘 temp_upload/partial_*.json 兜底;
      - SqliteSaver(core/checkpoint_store): 状态持久化到本地 sqlite 文件, 重启后可恢复;
        仍为单进程边界(多实例并发写同一 sqlite 文件不受支持, 见 README 已知局限)。
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
    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()
