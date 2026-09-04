"""
tests/test_sandbox.py —— 代码沙盒 & 核心工具离线回归测试(pytest 风格, 不调用任何网络/API 额度)

覆盖点:
    1. exec_python_code 正常执行输出;
    2. 静态黑名单拦截(open / import / .env 字面量 / ../ 穿越写法);
    3. 【运行时真实路径校验】: 拼接字符串绕过静态检查后读取项目 .env,
       必须被子进程内的路径白名单拦截(【安全拦截】);
    4. 超时强杀: 死循环代码在 EXEC_TIMEOUT 后被杀死(无残留线程), 返回超时说明;
    5. matplotlib 图表自动保存(图片出现在 temp_upload/ 并可从输出定位);
    6. token 计数/裁剪工具与全部提示词模板可加载;
    7. LLM 统一重试退避 / 素材 token 预算 / 模板占位符(全部离线);
    8. 反思节点"无素材"分支、结构化 JSON 判定成功分支与带 checkpointer 的图编译。

运行方式(两种等价, 均不调用 LLM API / 不联网):
    python -m pytest tests/test_sandbox.py -v      # pytest 方式(CI 同款)
    python tests/test_sandbox.py                    # 兼容旧式"直接运行"

graph_builder 的其余离线单测见 tests/test_graph_builder.py。
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _sandbox_module():
    """延迟导入, 保证任意工作目录先完成 sys.path 注入。"""
    from tools import code_exec_tool  # noqa: E402

    return code_exec_tool


# 1) 正常执行
def test_sandbox_normal_execution():
    out = _sandbox_module().exec_python_code("print('hello-沙盒', 1 + 1)")
    assert "hello-沙盒 2" in out and "工具异常" not in out, out[:200]


# 2) 静态黑名单: open / import / .env 字面量 / ../ 穿越
def test_static_blacklist_intercepts_dangerous_code():
    code_exec_tool = _sandbox_module()
    cases = [
        "print(open('a.txt'))",                                     # open(
        "import pandas as pd",                                      # import
        "df = pd.read_csv(DATA_DIR + '/../.env')",                  # .env 字面量
        "df = pd.read_csv(DATA_DIR + '/../../etc/passwd')",         # ../ 穿越
    ]
    for code in cases:
        assert "【安全拦截】" in code_exec_tool.exec_python_code(code), code


# 3) 运行时真实路径校验: 拼字符串绕过静态扫描, 读取项目根 .env -> 必须被拦截
def test_runtime_path_guard_blocks_env_read():
    out = _sandbox_module().exec_python_code(
        "df = pd.read_csv(DATA_DIR + '/..' + chr(47) + '.en' + 'v', nrows=1)"
    )
    assert "安全拦截" in out and ".env" in out, out[:300]


# 4) 超时强杀: 缩短超时便于测试, 死循环代码必须被终止并返回超时提示
def test_infinite_loop_timeout_kill(monkeypatch):
    import time

    code_exec_tool = _sandbox_module()
    monkeypatch.setattr(code_exec_tool, "EXEC_TIMEOUT", 3)
    t0 = time.time()
    out = code_exec_tool.exec_python_code("x = 0\nwhile x < 10**12:\n    x = x + 1")
    elapsed = time.time() - t0
    assert "已被强制终止" in out and elapsed < 20, f"耗时 {elapsed:.1f}s: {out[:200]}"


# 5) 图表自动保存(temp_upload/ 应出现新 png)
def test_matplotlib_chart_auto_saved():
    code_exec_tool = _sandbox_module()
    os.makedirs(code_exec_tool.SAVE_DIR, exist_ok=True)
    img_before = {n for n in os.listdir(code_exec_tool.SAVE_DIR) if n.lower().endswith(".png")}
    out = code_exec_tool.exec_python_code(
        "plt.plot([1, 2, 3], [3, 1, 2])\nplt.title('sandbox-test')"
    )
    img_after = {n for n in os.listdir(code_exec_tool.SAVE_DIR) if n.lower().endswith(".png")}
    new_imgs = img_after - img_before
    assert len(new_imgs) >= 1 and "本轮生成的图表文件" in out, \
        f"新图: {sorted(new_imgs)} | {out[:200]}"
    for n in new_imgs:  # 测试自清理
        try:
            os.remove(os.path.join(code_exec_tool.SAVE_DIR, n))
        except OSError:
            pass


# 6) token 计数/裁剪 与 提示词模板完整性
def test_token_count_and_truncation():
    from graph_builder import _count_tokens, _truncate_to_tokens

    assert _count_tokens("你好世界 hello world") is not None
    long_text = "调研素材内容" * 500
    cut = _truncate_to_tokens(long_text, 30)
    assert len(cut) < len(long_text) and "已截断" in cut, f"{len(long_text)} -> {len(cut)} 字符"


def test_all_prompt_templates_loadable():
    from graph_builder import _load_prompt

    prompt_names = [
        "planner_prompt.txt", "planner_system.txt", "tool_system.txt",
        "reflection_prompt.txt", "reflection_system.txt",
        "report_prompt.txt", "report_system.txt",
    ]
    assert all(len(_load_prompt(n).strip()) > 0 for n in prompt_names), "共 7 个模板必须全部存在且非空"


def test_prompt_placeholders_renderable():
    import re

    from graph_builder import _load_prompt

    for name in ["planner_prompt.txt", "reflection_prompt.txt", "report_prompt.txt"]:
        template = _load_prompt(name)
        placeholders = re.findall(r"\{(\w+)\}", template)
        rendered = template.format(**{p: "X" for p in placeholders})  # 缺占位符会抛 KeyError
        assert len(rendered) > 0


# 7) 反思节点空素材分支 + checkpointer 图编译(不发起 LLM 请求)
def test_reflection_empty_materials_judges_insufficient():
    from graph_builder import make_reflection_node

    out = make_reflection_node(None)({
        "user_query": "测试主题", "iteration_count": 1, "collected_info": [],
    })
    assert out.get("reflection_sufficient") is False and "信息不足" in out.get("reflection", ""), \
        str(out.get("reflection", ""))[:120]


def test_graph_compiles_with_checkpointer():
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver

    from graph_builder import build_graph

    dummy_llm = ChatOpenAI(model="dummy-model", api_key="sk-dummy-not-real",
                           base_url="http://127.0.0.1:1/v1", request_timeout=2)
    graph = build_graph(dummy_llm, checkpointer=InMemorySaver())
    assert graph is not None


# 8) LLM 统一重试退避 / 素材 token 预算 / 反思结构化成功分支(全部离线)
class _RetryLLM:
    """前 2 次调用抛超时, 第 3 次成功。"""
    model_name = "deepseek-chat"
    attempts = 0

    def invoke(self, messages):
        self.attempts += 1
        if self.attempts <= 2:
            raise TimeoutError("simulated timeout")
        return type("R", (), {"content": "ok-result"})()


def test_llm_retry_succeeds_after_transient_errors(monkeypatch):
    import time as _time

    from graph_builder import _invoke_llm

    monkeypatch.setattr(_time, "sleep", lambda _s: None)  # 离线测试跳过退避等待
    llm = _RetryLLM()
    out = _invoke_llm(llm, [("system", "s")])
    assert out == "ok-result" and llm.attempts == 3, f"attempts={llm.attempts}"


class _BoomLLM:
    model_name = "x"
    attempts = 0

    def invoke(self, messages):
        self.attempts += 1
        raise ConnectionError("boom")


def test_llm_retry_raises_after_all_failures(monkeypatch):
    import time as _time

    import pytest

    from graph_builder import _invoke_llm

    monkeypatch.setattr(_time, "sleep", lambda _s: None)
    llm = _BoomLLM()
    with pytest.raises(ConnectionError):
        _invoke_llm(llm, [])
    assert llm.attempts >= 2, f"attempts={llm.attempts}"


def test_join_material_keeps_all_when_within_budget():
    from graph_builder import _join_material

    materials = ["甲" * 1000] * 5
    joined = _join_material(materials, per_item_tokens=2000, total_tokens=30000)
    assert joined.count("【素材") == 5


def test_join_material_drops_and_notes_when_over_budget():
    from graph_builder import _join_material

    materials = ["乙" * 1000] * 25
    joined = _join_material(materials, per_item_tokens=50, total_tokens=1000)
    assert "token 预算省略" in joined and "【素材1】" in joined, joined[-80:]


def test_reflection_json_sufficient_branch():
    from graph_builder import make_reflection_node

    class _JsonReflectLLM:
        model_name = "x"

        def invoke(self, messages):
            return type("R", (), {"content": '{"sufficient": true, "reason": "素材已覆盖", '
                                            '"missing_topics": []}'})()

    out = make_reflection_node(_JsonReflectLLM())({
        "user_query": "q", "iteration_count": 3,
        "collected_info": ["素材内容覆盖主题全部要点"],
    })
    assert out.get("reflection_sufficient") is True and "信息充足" in out.get("reflection", ""), \
        str(out.get("reflection", ""))[:80]


# 兼容旧用法: python tests/test_sandbox.py 直接运行(内部转调 pytest)
def _standalone_main() -> int:
    try:
        import pytest
    except ImportError:
        print("直接运行本脚本需要先安装 pytest: python -m pip install pytest", file=sys.stderr)
        return 2
    return pytest.main([os.path.abspath(__file__), "-v"])


if __name__ == "__main__":
    sys.exit(_standalone_main())
