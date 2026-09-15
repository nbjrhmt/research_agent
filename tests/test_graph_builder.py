"""
tests/test_graph_builder.py —— graph_builder 核心逻辑离线单元测试(pytest, 不调用 LLM API / 不联网)

按需求覆盖:
    P0-1 反思节点时序: 先做充足性判断、后执行(展示)截断 —— 判断输入在素材全量能放进
         上下文硬预算时【不截断】(杜绝"素材提前截断 → 误判信息不足 → 空转轮次");
    P0-3 反思 JSON 解析: 解析失败先内部重试反思请求, 全部失败才降级"信息不足";
         连续失败计数达到上限后条件路由强制进入报告节点(防空转);
    P1-1 核心模块单测: token 裁剪逻辑 / token budget 预算告警 / 反思 JSON 解析 /
         素材去重逻辑 / 规划与报告节点 / 条件路由。

说明: 全部用例通过注入 fake LLM / monkeypatch 预算常量完成, 不产生任何真实 API 调用,
适合 CI 频繁执行; 真实全链路(e2e, 消耗 API 额度)在 tests/_e2e_test.py, 不进 CI。
"""
import json

import pytest

# 项目根目录路径由 tests/conftest.py 统一注入(2026 工程重构 P4), 本文件不再自插 sys.path
import graph_builder as gb  # noqa: E402
from graph_builder import (  # noqa: E402
    MAX_REFLECT_FAILURES,
    _clip,
    _coerce_bool,
    _corpus_token_total,
    _extract_json,
    _is_duplicate_material,
    _join_material,
    _llm_json_ask,
    _log_prompt_budget,
    _reflection_judgment_envelope,
    _truncate_to_tokens,
    make_planner_node,
    make_reflection_node,
    make_report_node,
    make_tool_node,
    route_after_reflection,
)

# =====================================================================
# 通用测试替身
# =====================================================================
def _llm_result(content: str):
    """构造带 .content 的假响应对象。"""
    return type("R", (), {"content": content})()


class ScriptedLLM:
    """按脚本顺序逐个返回 content; 记录每次 invoke 收到的完整 human 消息。"""
    model_name = "mock-model"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.attempts = 0
        self.seen_messages = []   # 每次调用的完整消息列表(system/human)

    def invoke(self, messages):
        self.attempts += 1
        self.seen_messages.append(messages)
        content = self.outputs[min(self.attempts - 1, len(self.outputs) - 1)]
        if isinstance(content, Exception):
            raise content
        return _llm_result(content)


def _reflect_state(materials, iteration=1, failures=0, query="测试主题"):
    return {
        "user_query": query,
        "collected_info": list(materials),
        "iteration_count": iteration,
        "reflection_failures": failures,
    }


# =====================================================================
# P0-1: 反思节点时序 —— 先判断(充足性)、后截断; 判断前禁止素材被提前截断
# =====================================================================
class _ScanJudgmentLLM:
    """扫描判断输入里是否含"尾部标记"的假 LLM: 找到→sufficient=true, 否则 false。

    用于证明: 充足性判断阶段拿到的素材是【完整未截断】的 —— 若素材在判断前被提前
    截断(旧实现按固定 3 万 token 软预算先砍), 位于素材尾部的标记就会丢失, LLM 会
    因此误判"信息不足"。
    """
    model_name = "mock-model"

    def __init__(self, tail_marker: str):
        self.tail_marker = tail_marker
        self.attempts = 0
        self.judge_input = ""

    def invoke(self, messages):
        self.attempts += 1
        self.judge_input = str(messages[-1][1] or "")  # human 消息 = 判断输入
        sufficient = self.tail_marker in self.judge_input
        body = json.dumps({
            "sufficient": sufficient,
            "reason": "素材已覆盖全部要点" if sufficient else "缺少关键信息",
            "missing_topics": [] if sufficient else ["补充关键词"],
        }, ensure_ascii=False)
        return _llm_result(body)


def test_reflection_judges_on_full_materials_beyond_soft_budget(monkeypatch):
    """素材总量超过反思"软预算"时, 判断输入仍必须包含全部素材(含尾部), 不得先截断再判断。

    旧行为: 反思按固定 _MATERIAL_REFLECT_TOTAL_TOKENS 预算先截断素材再问 LLM,
    素材一多, LLM 只能看到前几段 → 误判"信息不足" → 空转搜集轮次。
    修复后: 判断信封随素材全量动态扩容(只受模型上下文硬预算约束), 全量素材完整进入判断。
    """
    monkeypatch.setattr(gb, "_MATERIAL_REFLECT_TOTAL_TOKENS", 30)   # 软预算压到极小
    monkeypatch.setattr(gb, "LLM_CONTEXT_TOKENS", 300_000)          # 上下文硬预算充足
    monkeypatch.setattr(gb, "LLM_MAX_OUTPUT_TOKENS", 8192)

    tail_marker = "P0时序尾部标记Σω"
    # 3 条素材总 token 远超上面的软预算 30; 尾部标记只出现在最后一条素材末尾
    materials = [
        "第%d条素材内容。" % i * 2000 for i in (1, 2)
    ] + ["第三条素材正文内容。" * 2000 + tail_marker]

    fake = _ScanJudgmentLLM(tail_marker)
    node = make_reflection_node(fake)
    out = node(_reflect_state(materials, iteration=2))

    assert fake.attempts == 1, "素材充足时反思应只问一次"
    # 关键断言: 判断输入包含素材尾部的标记 -> 判断前素材没有被截断
    assert tail_marker in fake.judge_input, "判断输入被提前截断, 尾部素材丢失"
    assert "省略" not in fake.judge_input and "已截断" not in fake.judge_input
    # 结构化判定结果为充足 -> 不会误判信息不足
    assert out.get("reflection_sufficient") is True
    assert "信息充足" in out.get("reflection", "")


def test_reflection_never_truncates_state_materials_before_judgment():
    """反思节点返回结果中不得包含 collected_info(不改写/截断 State 里的素材本体)。"""
    materials = ["甲" * 3000, "乙" * 3000, "丙" * 3000]
    state_in = _reflect_state(materials, iteration=1)
    fake = ScriptedLLM(['{"sufficient": false, "reason": "还缺数据", '
                        '"missing_topics": ["补充搜索"]}'])
    out = make_reflection_node(fake)(state_in)
    assert "collected_info" not in out, "反思节点不得返回/改写素材通道"
    assert state_in["collected_info"] == materials, "入参素材对象内容不得被修改"


def test_reflection_display_clip_happens_after_structured_judgment():
    """展示文本的截断必须发生在结构化判定完成之后, 判定结果不受展示截断影响。"""
    long_reason = "充足理由。" * 900   # ~3600+ 字, 超过节点内部 reason 展示上限 1000
    fake = ScriptedLLM([json.dumps({
        "sufficient": True, "reason": long_reason, "missing_topics": [],
    }, ensure_ascii=False)])
    out = make_reflection_node(fake)(_reflect_state(["素材内容"]))
    # 结构化判定先于文本截断完成且不受截断影响
    assert out.get("reflection_sufficient") is True
    text = str(out.get("reflection", ""))
    assert text.startswith("反思结论：信息充足。")        # 判定结论完整保留
    assert len(text) <= 4000                              # 展示文本在判定后才被裁剪
    # 超长 reason 只保留前 1000 字(900 段共 4500 字 -> 展示文本明显变短)
    assert len(text) < len(long_reason)
    assert text.count("充足理由。") <= 210                # 1000 字上限约 200 段


def test_reflection_no_materials_skips_llm_immediately():
    """无素材时离线即可判定信息不足: 不发 LLM 请求(0 次调用), 不触发任何截断。"""
    class _CountingLLM:
        model_name = "mock-model"
        attempts = 0

        def invoke(self, messages):
            self.attempts += 1
            return _llm_result('{"sufficient": true}')

    fake = _CountingLLM()
    out = make_reflection_node(fake)(_reflect_state([], iteration=0))
    assert fake.attempts == 0
    assert out.get("reflection_sufficient") is False
    assert out.get("reflection_failures") == 0


# =====================================================================
# P0-3: 反思 JSON 解析失败 —— 先内部重试, 后降级; 连续失败防空转
# =====================================================================
def test_reflection_json_parse_failure_retries_internally_then_degrades():
    """JSON 解析失败必须先在节点内部重试反思请求(REFLECT_JSON_RETRY 次), 全部失败才降级。"""
    garbage = "抱歉, 我不能输出 JSON…… 这是纯文本回答。"   # 永远解析失败
    fake = ScriptedLLM([garbage] * 5)
    out = make_reflection_node(fake)(_reflect_state(["素材甲", "素材乙"], iteration=3,
                                                    failures=1))
    # 1 次首次请求 + REFLECT_JSON_RETRY 次内部重试
    assert fake.attempts == 1 + gb.REFLECT_JSON_RETRY, f"attempts={fake.attempts}"
    assert out.get("reflection_sufficient") is False          # 全部重试失败才降级
    assert "内部重试" in str(out.get("reflection", ""))       # 降级原因如实注明
    assert out.get("reflection_failures") == 2                # 连续失败计数 +1


def test_reflection_json_success_after_internal_retry_resets_failures():
    """内部重试中模型恢复正常输出合法 JSON -> 判定成功且连续失败计数清零。"""
    fake = ScriptedLLM([
        "不是 JSON 的废话输出",
        json.dumps({"sufficient": True, "reason": "第二次输出成功", "missing_topics": []},
                   ensure_ascii=False),
    ])
    out = make_reflection_node(fake)(_reflect_state(["素材内容"], iteration=2, failures=3))
    assert fake.attempts == 2                                  # 第二次内部重试即成功
    assert out.get("reflection_sufficient") is True
    assert out.get("reflection_failures") == 0                 # 成功即清零


def test_reflection_transport_failure_degrades_and_counts(monkeypatch):
    """LLM 传输层全部重试失败(内部已重试反思请求) -> 降级信息不足 + 失败计数 +1。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    class _AlwaysDown:
        model_name = "mock-model"
        attempts = 0

        def invoke(self, messages):
            self.attempts += 1
            raise ConnectionError("network down")

    fake = _AlwaysDown()
    out = make_reflection_node(fake)(_reflect_state(["素材内容"], iteration=1))
    assert fake.attempts >= 3      # json_ask 每次尝试内部都会发生传输重试
    assert out.get("reflection_sufficient") is False
    assert out.get("reflection_failures") == 1
    assert "信息不足" in str(out.get("reflection", ""))


def test_route_after_reflection_breaks_failure_loop_and_caps():
    """条件路由: 连续判定失败达到上限后强制进报告节点(不空耗轮次); 其余分支语义不变。"""
    # 连续失败 >= MAX_REFLECT_FAILURES -> 强制报告(即使 sufficient=False)
    assert route_after_reflection({"iteration_count": 2,
                                   "reflection_failures": MAX_REFLECT_FAILURES,
                                   "reflection_sufficient": False}) == "report_node"
    # 失败未到上限且判定不足 -> 回工具节点
    assert route_after_reflection({"iteration_count": 2,
                                   "reflection_failures": MAX_REFLECT_FAILURES - 1,
                                   "reflection_sufficient": False}) == "tool_node"
    # 判定充足 -> 报告
    assert route_after_reflection({"iteration_count": 2,
                                   "reflection_failures": 0,
                                   "reflection_sufficient": True}) == "report_node"
    # 轮次上限 -> 强制报告
    assert route_after_reflection({"iteration_count": gb.MAX_ITERATIONS,
                                   "reflection_sufficient": False}) == "report_node"


def test_reflection_sufficient_false_keeps_collecting():
    fake = ScriptedLLM([json.dumps({"sufficient": False, "reason": "缺 2024 年数据",
                                    "missing_topics": ["2024 销量统计"]},
                                   ensure_ascii=False)])
    out = make_reflection_node(fake)(_reflect_state(["只有 2023 年数据"], iteration=5))
    assert out.get("reflection_sufficient") is False
    assert out.get("reflection_failures") == 0          # 正常判定"不足"不算失败
    assert "2024 销量统计" in str(out.get("reflection", "")) or "销量统计" in str(out.get("reflection", ""))


# =====================================================================
# 反思 JSON 字段规整 / 信封预算(单元)
# =====================================================================
def test_coerce_bool_accepts_various_true_false_forms():
    assert _coerce_bool(True) is True and _coerce_bool(False) is False
    assert _coerce_bool(1) is True and _coerce_bool(0) is False
    assert _coerce_bool("true") is True and _coerce_bool("是") is True
    assert _coerce_bool("false") is False and _coerce_bool("no") is False
    assert _coerce_bool("随便") is False and _coerce_bool(None) is False


def test_judgment_envelope_expands_with_corpus_until_hard_cap(monkeypatch):
    monkeypatch.setattr(gb, "_MATERIAL_REFLECT_TOTAL_TOKENS", 1000)  # 软预算基线
    monkeypatch.setattr(gb, "_context_cap", lambda: 5000)            # 上下文硬预算
    assert _reflection_judgment_envelope(300) == 1000     # 素材小 -> 保持基线
    assert _reflection_judgment_envelope(3000) == 3400    # 素材超基线 -> 扩到全量+余量
    assert _reflection_judgment_envelope(6000) == 5000    # 素材超硬预算 -> 封顶硬预算


def test_corpus_token_total_sums_without_truncation():
    total = _corpus_token_total(["abc", "你好世界", "   ", ""])
    assert total > 0                       # 非空素材都被计入
    # 空素材/纯空白不计入; 与直接 token 计数一致
    assert _corpus_token_total(["", "   "]) == 0
    one = gb._token_len("一段用于测试的素材内容")
    assert _corpus_token_total(["一段用于测试的素材内容"]) == one


# =====================================================================
# P1-1: token 裁剪逻辑
# =====================================================================
def test_truncate_within_limit_keeps_text_unchanged():
    text = "短素材内容"
    assert _truncate_to_tokens(text, 5000) == text


def test_truncate_over_limit_cuts_with_note():
    long_text = "调研素材正文。" * 2000
    cut = _truncate_to_tokens(long_text, 50)
    assert len(cut) < len(long_text)
    assert "已截断" in cut and "token 预算" in cut


def test_truncate_falls_back_to_character_estimate_without_tiktoken(monkeypatch):
    monkeypatch.setattr(gb, "_count_tokens", lambda *a, **k: None)  # 模拟 tiktoken 不可用
    long_text = "A" * 5000
    cut = _truncate_to_tokens(long_text, 50)   # 估算路径: 50 tokens * 4 字符
    assert len(cut) < len(long_text) and "已截断" in cut
    assert _truncate_to_tokens("", 100) == ""           # 空串安全


def test_join_material_marks_and_drops_over_budget():
    joined = _join_material(["内容%d" % i * 300 for i in range(20)],
                            per_item_tokens=40, total_tokens=120)
    assert "【素材1】" in joined
    assert "token 预算省略" in joined     # 超限素材明确注明, 不静默丢失
    assert "（暂无任何素材）" not in joined
    empty = _join_material([], per_item_tokens=10, total_tokens=100)
    assert "暂无任何素材" in empty


# =====================================================================
# P1-1: token budget 预算告警
# =====================================================================
def test_log_prompt_budget_warns_when_context_nearly_exhausted(caplog, monkeypatch):
    import logging

    monkeypatch.setattr(gb, "LLM_CONTEXT_TOKENS", 20000)
    monkeypatch.setattr(gb, "LLM_MAX_OUTPUT_TOKENS", 2000)
    # safety = max(4000, 20000-2000-2000) = 16000; 让提示词占用超过 90%
    big_prompt = "预算告警测试内容。" * 6000   # 远超 90% 安全预算
    with caplog.at_level(logging.INFO, logger="graph_builder"):
        _log_prompt_budget("reflection", big_prompt)
    assert any("上下文余量不足" in rec.message for rec in caplog.records), \
        [r.message for r in caplog.records]


def test_log_prompt_budget_no_warning_when_usage_low(caplog, monkeypatch):
    import logging

    monkeypatch.setattr(gb, "LLM_CONTEXT_TOKENS", 20000)
    monkeypatch.setattr(gb, "LLM_MAX_OUTPUT_TOKENS", 2000)
    small_prompt = "少量素材"
    with caplog.at_level(logging.INFO, logger="graph_builder"):
        _log_prompt_budget("reflection", small_prompt)
    assert any("节点素材/提示词约" in rec.message for rec in caplog.records)   # 常规用量日志
    assert not any("上下文余量不足" in rec.message for rec in caplog.records)  # 不触发告警


# =====================================================================
# P1-1: 素材去重逻辑
# =====================================================================
def test_dedup_helper_ignores_whitespace_differences():
    mats = ["【素材】\n第一段 内容", "另一条素材"]
    assert _is_duplicate_material("【素材】第一段 内容", mats) is True   # 换行/空格差异算重复
    assert _is_duplicate_material("另一条素材", mats) is True
    assert _is_duplicate_material("完全不同的素材", mats) is False
    assert _is_duplicate_material("", mats) is False
    assert _is_duplicate_material("  ", mats) is False


def _scripted_tool_llm(tool_payloads):
    return ScriptedLLM([json.dumps(p, ensure_ascii=False) for p in tool_payloads])


def test_tool_node_dedup_skips_identical_result_but_counts_round():
    def fake_search(query):
        return "固定搜索结果: 2026 年开源大模型综述"

    llm = _scripted_tool_llm([
        {"tool": "bocha_web_search", "query": "开源大模型 综述"},
        {"tool": "bocha_web_search", "query": "开源大模型 综述"},
    ])
    node = make_tool_node(llm, web_search_tool=fake_search)
    materials: list = []
    r1 = node({"user_query": "调研", "iteration_count": 0, "collected_info": materials,
               "uploaded_files": []})
    assert r1["iteration_count"] == 1 and len(r1["collected_info"]) == 1
    materials += r1["collected_info"]

    r2 = node({"user_query": "调研", "iteration_count": r1["iteration_count"],
               "collected_info": materials, "uploaded_files": []})
    assert r2["iteration_count"] == 2                     # 本轮仍计入轮次
    assert r2["collected_info"] == []                     # 内容重复 -> 不追加素材
    assert any("已自动去重" in line for line in (r2.get("steps_log") or []))


def test_tool_node_accumulates_distinct_materials():
    def fake_search(query):
        return f"搜索结果: {query}"

    llm = _scripted_tool_llm([
        {"tool": "bocha_web_search", "query": "关键词A"},
        {"tool": "bocha_web_search", "query": "关键词B"},
    ])
    node = make_tool_node(llm, web_search_tool=fake_search)
    materials: list = []
    for i in range(2):
        r = node({"user_query": "调研", "iteration_count": i,
                  "collected_info": materials, "uploaded_files": []})
        materials += r["collected_info"]
    assert len(materials) == 2 and "关键词A" in materials[0] and "关键词B" in materials[1]


def test_tool_node_unknown_tool_becomes_material_not_crash():
    llm = _scripted_tool_llm([{"tool": "hack_tool", "query": "x"}])
    node = make_tool_node(llm, web_search_tool=lambda q: "内容")
    out = node({"user_query": "q", "iteration_count": 0, "collected_info": [],
                "uploaded_files": []})
    assert len(out["collected_info"]) == 1
    assert "工具调用失败" in out["collected_info"][0] or "未知工具名" in out["collected_info"][0]


def test_tool_node_exec_disabled_returns_clear_material(monkeypatch):
    """ALLOW_CODE_EXEC=false 时, exec_python_code 必须被整体关闭且给出明确素材提示。"""
    monkeypatch.setattr(gb, "ALLOW_CODE_EXEC", False)
    llm = _scripted_tool_llm([{"tool": "exec_python_code", "code": "print(1)"}])
    node = make_tool_node(llm, web_search_tool=lambda q: "内容")
    out = node({"user_query": "q", "iteration_count": 0, "collected_info": [],
                "uploaded_files": []})
    entry = out["collected_info"][0]
    assert "ALLOW_CODE_EXEC" in entry and "工具调用失败" in entry, entry[:200]


def test_tool_node_exec_runs_when_enabled():
    """ALLOW_CODE_EXEC 默认开启: exec_python_code 正常执行(沙盒离线可用)。"""
    llm = _scripted_tool_llm([{"tool": "exec_python_code", "code": "print('exec-ok', 6*7)"}])
    node = make_tool_node(llm, web_search_tool=lambda q: "内容")
    out = node({"user_query": "q", "iteration_count": 0, "collected_info": [],
                "uploaded_files": []})
    assert len(out["collected_info"]) == 1 and "exec-ok 42" in out["collected_info"][0], \
        out["collected_info"][0][:200]


# =====================================================================
# 规划 / 报告 / JSON 提取与重试(离线)
# =====================================================================
def test_planner_parses_sub_tasks():
    llm = ScriptedLLM([json.dumps({"sub_tasks": ["查政策", "查市场", "查案例"]},
                                  ensure_ascii=False)])
    out = make_planner_node(llm)({"user_query": "调研低空经济"})
    assert out["sub_tasks"] == ["查政策", "查市场", "查案例"]


def test_planner_falls_back_when_tasks_missing_or_string():
    node = make_planner_node(ScriptedLLM([
        json.dumps({"sub_tasks": "直接当搜索词"}, ensure_ascii=False),
    ]))
    assert node({"user_query": "Q"})["sub_tasks"] == ["直接当搜索词"]
    node2 = make_planner_node(ScriptedLLM(['{"other": 1}']))
    assert node2({"user_query": "原始需求"})["sub_tasks"] == ["原始需求"]   # 拆不出 -> 原问题


def test_extract_json_tolerates_surrounding_text():
    assert _extract_json('解释文字 {"a": 1} 结尾') == {"a": 1}
    assert _extract_json('```json\n{"b": [1, 2]}\n```') == {"b": [1, 2]}
    with pytest.raises(ValueError):
        _extract_json("没有任何大括号的文本")


def test_llm_json_ask_retries_then_raises(monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)
    fake = ScriptedLLM(["纯文本垃圾"] * 10)
    with pytest.raises(RuntimeError, match="JSON 解析失败"):
        _llm_json_ask(fake, "sys", "user")
    assert fake.attempts == 1 + gb.MAX_JSON_RETRY    # 首次 + 2 次内部重试


def test_report_node_returns_report_or_raises_on_empty():
    fake_ok = ScriptedLLM(["  # 调研报告\n结论……  "])
    out = make_report_node(fake_ok)({"user_query": "q", "collected_info": ["素材"]})
    assert out["final_report"].startswith("# 调研报告")    # strip 后返回

    fake_empty = ScriptedLLM(["   "])
    with pytest.raises(RuntimeError, match="没有生成任何内容"):
        make_report_node(fake_empty)({"user_query": "q", "collected_info": ["素材"]})


def test_clip_marks_long_text():
    assert _clip("短文本", 10) == "短文本"
    cut = _clip("很长" * 100, 20)
    assert len(cut) <= 20 + 30 and "已截断" in cut   # 20 上限 + 尾注


def test_tool_node_round_logs_contain_material_count():
    llm = _scripted_tool_llm([{"tool": "bocha_web_search", "query": "q1"}])
    node = make_tool_node(llm, web_search_tool=lambda q: "内容X")
    out = node({"user_query": "q", "iteration_count": 0, "collected_info": [],
                "uploaded_files": []})
    assert any("素材累计 1 条" in line for line in (out.get("steps_log") or []))

# =====================================================================
# v1.6.0: 长期记忆/RAG 记忆检索节点(make_memory_retrieve_node / build_graph 接线)
# =====================================================================
class _FakeMemoryStore:
    """内存版假记忆库: 按 kind 返回固定检索结果, 用于验证节点注入与图接线。"""

    def __init__(self, run_hits=None, doc_hits=None):
        self.run_hits = run_hits or []
        self.doc_hits = doc_hits or []
        self.seen_kinds = []

    def search(self, query, top_k=3, kind=None):
        self.seen_kinds.append(kind)
        if kind == "run":
            return list(self.run_hits)
        if kind == "doc_chunk":
            return list(self.doc_hits)
        return list(self.run_hits) + list(self.doc_hits)


def test_memory_retrieve_node_injects_history_and_doc_chunks():
    """记忆检索节点: 历史记忆与文档片段分别注入 collected_info, 且日志说明命中。"""
    store = _FakeMemoryStore(
        run_hits=[{"content": "历史素材A", "query": "相似主题1"}],
        doc_hits=[{"content": "文档块B", "source_name": "白皮书.pdf"}],
    )
    node = gb.make_memory_retrieve_node(store, run_top_k=3, doc_top_k=3)
    out = node({"user_query": "调研主题", "collected_info": []})

    entries = out.get("collected_info") or []
    assert len(entries) == 2
    assert "【历史记忆】" in entries[0] and "相似主题1" in entries[0]
    assert "【文档片段】" in entries[1] and "白皮书.pdf" in entries[1]
    logs = "\n".join(out.get("steps_log") or [])
    assert "命中历史记忆 1 条" in logs and "文档片段 1 条" in logs
    assert store.seen_kinds == ["run", "doc_chunk"]


def test_memory_retrieve_node_no_hits_returns_empty_materials():
    """无命中时: 不注入素材, 日志如实说明, 不抛异常。"""
    store = _FakeMemoryStore()
    node = gb.make_memory_retrieve_node(store)
    out = node({"user_query": "全新主题", "collected_info": []})
    assert (out.get("collected_info") or []) == []
    assert any("未命中" in line for line in (out.get("steps_log") or []))


def test_memory_retrieve_node_search_failure_does_not_block():
    """检索失败: 记入日志但不阻断任务(记忆是增强而非依赖)。"""

    class _BoomStore:
        def search(self, *a, **k):
            raise RuntimeError("向量库不可用(测试)")

    node = gb.make_memory_retrieve_node(_BoomStore())
    out = node({"user_query": "主题", "collected_info": []})
    assert (out.get("collected_info") or []) == []
    assert any("记忆检索失败" in line for line in (out.get("steps_log") or []))


def test_build_graph_with_memory_store_has_retrieve_node():
    """build_graph 传入 memory_store 时: 图含 memory_retrieve_node 且位于规划之前。"""
    graph = gb.build_graph(llm=ScriptedLLM(["{}"]), web_search_tool=lambda q: "x",
                           checkpointer=None, memory_store=_FakeMemoryStore())
    nodes = graph.get_graph().nodes
    assert "memory_retrieve_node" in nodes
    # START 的下一跳必须是记忆检索节点(再进入 planner)
    edges = graph.get_graph().edges
    assert any(e.source == "__start__" and e.target == "memory_retrieve_node" for e in edges)


def test_build_graph_without_memory_store_no_retrieve_node():
    """不传 memory_store(默认): 图结构与旧版完全一致, 无记忆节点。"""
    graph = gb.build_graph(llm=ScriptedLLM(["{}"]), web_search_tool=lambda q: "x")
    assert "memory_retrieve_node" not in graph.get_graph().nodes
    edges = graph.get_graph().edges
    assert any(e.source == "__start__" and e.target == "planner_node" for e in edges)

# =====================================================================
# v1.6.0: HITL 人工确认节点(confirmation_node / route_after_confirmation / build_graph 接线)
# =====================================================================
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from graph_builder import (  # noqa: E402
    route_after_confirmation,
)


def test_route_after_confirmation_pure_function():
    """人工确认后路由: 停止→report_node, 继续/默认→tool_node(纯函数, 单测友好)。"""
    assert route_after_confirmation({"human_continue": False}) == "report_node"
    assert route_after_confirmation({"human_continue": True}) == "tool_node"
    assert route_after_confirmation({}) == "tool_node"   # 字段缺失默认继续


def test_build_graph_hitl_adds_confirmation_node():
    """human_in_the_loop=True: 图含 confirmation_node; False(默认)则无(旧行为不变)。"""
    g_on = gb.build_graph(llm=ScriptedLLM(["{}"]), web_search_tool=lambda q: "x",
                          human_in_the_loop=True)
    assert "confirmation_node" in g_on.get_graph().nodes
    g_off = gb.build_graph(llm=ScriptedLLM(["{}"]), web_search_tool=lambda q: "x")
    assert "confirmation_node" not in g_off.get_graph().nodes


def _run_hitl_graph(llm_outputs, initial_query="调研主题", resume=None):
    """完整跑一次带 HITL 的图: 返回 (最终状态 values, 中断时 next 列表)。

    - resume=None: 第一次执行, 应在 confirmation_node 处暂停;
    - resume="continue"/"stop": 以 Command(resume=...) 恢复并跑到结束。
    """
    llm = ScriptedLLM(llm_outputs)
    graph = gb.build_graph(llm=llm, web_search_tool=lambda q: "【素材】搜索结果内容。",
                           checkpointer=InMemorySaver(), human_in_the_loop=True)
    config = {"configurable": {"thread_id": "hitl-test-1"}}
    initial = {"user_query": initial_query, "collected_info": [], "iteration_count": 0}
    for _event in graph.stream(initial, config=config, stream_mode="updates"):
        pass
    state = graph.get_state(config)
    if resume is None:
        return state.values, state.next
    for _event in graph.stream(Command(resume=resume), config=config, stream_mode="updates"):
        pass
    return graph.get_state(config).values, ()


def test_hitl_interrupt_pauses_before_continue():
    """反思判定不足时: 任务在 confirmation_node 暂停(next 指向它), 不直接继续。"""
    outputs = [
        '{"sub_tasks": ["子任务1"]}',
        '{"tool": "bocha_web_search", "query": "关键词"}',
        '{"sufficient": false, "reason": "信息不足", "missing_topics": ["更多"]}',
    ]
    values, next_nodes = _run_hitl_graph(outputs)
    assert "confirmation_node" in next_nodes
    assert len(values.get("collected_info") or []) >= 1   # 已搜集素材保留


def test_hitl_resume_continue_keeps_collecting():
    """恢复 continue: 回到工具节点继续搜集, 反思充足后出报告。"""
    outputs = [
        '{"sub_tasks": ["子任务1"]}',
        '{"tool": "bocha_web_search", "query": "第一轮"}',
        '{"sufficient": false, "reason": "信息不足", "missing_topics": ["补充"]}',
        '{"tool": "bocha_web_search", "query": "第二轮"}',
        '{"sufficient": true, "reason": "信息已充足", "missing_topics": []}',
        "# 最终报告\n结论(素材1)。",
    ]
    values, _next = _run_hitl_graph(outputs, resume="continue")
    assert "最终报告" in values.get("final_report") or ""
    assert int(values.get("iteration_count") or 0) >= 2      # 恢复后继续跑了第二轮
    assert len(values.get("collected_info") or []) >= 2      # 素材只增不减


def test_hitl_resume_stop_generates_report_without_more_rounds():
    """恢复 stop: 不再搜集, 基于现有素材直接出报告(轮次不再增加)。"""
    outputs = [
        '{"sub_tasks": ["子任务1"]}',
        '{"tool": "bocha_web_search", "query": "第一轮"}',
        '{"sufficient": false, "reason": "信息不足", "missing_topics": ["补充"]}',
        "# 提前结束报告\n结论(素材1)。",
    ]
    values, _next = _run_hitl_graph(outputs, resume="stop")
    assert "提前结束报告" in values.get("final_report") or ""
    assert int(values.get("iteration_count") or 0) == 1       # 停在第一轮
    assert len(values.get("collected_info") or []) == 1
