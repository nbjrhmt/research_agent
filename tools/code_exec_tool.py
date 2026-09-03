"""
code_exec_tool.py —— 受限 Python 代码执行工具(仅数据分析/绘图)

用途: 让 Agent 用 pandas / matplotlib 对上传的 CSV 做统计、计算、绘图。

原型级沙盒规则(安全保护规则 #2):
    1. 只注入 pd / plt / DATA_DIR / SAVE_DIR 四个名字, 模型代码无需也不能 import;
    2. 文本黑名单: 禁止 import 任何模块、禁止 eval/exec/compile/open/input 等,
       禁止文件删除/改名等操作 API, 禁止 os/sys/subprocess/shutil/socket 等;
    3. 线程超时 30 秒, 超时放弃本轮执行(防止死循环卡死整个页面);
    4. 代码长度上限 4000 字符。
注意: 这是学习用原型沙盒, 不是强安全隔离环境, 请勿部署到公网。
"""
import builtins
import contextlib
import io
import os
import re
import threading
import time
import traceback

# ---- 无界面后端必须在使用 pyplot 之前设置, 避免弹窗与多线程问题 ----
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  在设置 Agg 之后导入

import pandas as pd  # noqa: E402

# temp_upload 目录固定在本项目根目录下(code_exec_tool 的上一级)
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BASE_DIR, "temp_upload")  # 上传数据所在目录(注入给代码)
SAVE_DIR = DATA_DIR                                 # 图表保存目录(注入给代码)

EXEC_TIMEOUT = 30      # 秒
CODE_MAX_CHARS = 4000  # 字符

# ---------------- 文本黑名单: 命中即整段拒绝执行 ----------------
_BANNED_PATTERNS = [
    r"\b__import__\b",                                   # 动态导入
    r"\b(?:import|from)\s+(?:os|sys|shutil|subprocess|socket|pathlib|requests|"
    r"urllib|ftplib|http|pickle|ctypes|multiprocessing|threading|importlib|builtins)\b",
    r"\b(eval|exec|compile|open|input|exit|quit|breakpoint)\s*\(",
    r"\b(?:os|sys|shutil|subprocess)\s*\.",              # 任何系统模块调用
    r"\.(?:remove|unlink|rmdir|mkdir|makedirs|rename|replace|write|truncate|"
    r"chmod|chown|symlink|link|rmtree)\s*\(",            # 文件删除/修改类 API
    r"\.(?:system|popen|run|call|check_output|Popen)\s*\(",
    r"while\s+True",                                     # 疑似死循环
]

# ---------------- 允许暴露给模型代码的内置名字白名单 ----------------
_SAFE_BUILTIN_NAMES = (
    "print len range enumerate zip map filter str int float bool list dict tuple set "
    "frozenset min max sum abs round sorted reversed any all isinstance type repr format "
    "divmod pow complex slice iter next chr ord hex oct bin hash id object range "
    "Exception ValueError TypeError KeyError IndexError RuntimeError AttributeError "
    "ZeroDivisionError ArithmeticError OverflowError NameError MemoryError "
    "NotImplementedError StopIteration FileNotFoundError"
).split()
_RESTRICTED_BUILTINS = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES}


def _check_blocked(code: str):
    """检查黑名单, 返回 (是否违规, 命中的片段)"""
    for pattern in _BANNED_PATTERNS:
        match = re.search(pattern, code, re.IGNORECASE)
        if match:
            return True, match.group(0)[:80]
    return False, ""


def exec_python_code(code: str) -> str:
    """
    在受限环境中执行数据分析代码。

    :param code: 待执行代码(只能使用已注入的 pd / plt / DATA_DIR / SAVE_DIR)
    :return:     运行输出文本(含生成的图表文件路径); 违规/异常时返回说明文字
    """
    code = (code or "").strip()
    if not code:
        return "【工具异常】exec_python_code: code 参数为空。"

    if len(code) > CODE_MAX_CHARS:
        return f"【安全拦截】代码超过长度上限 {CODE_MAX_CHARS} 字符, 请让模型精简代码。"

    blocked, snippet = _check_blocked(code)
    if blocked:
        return (f"【安全拦截】代码包含被禁止的写法「{snippet}」: 沙盒只允许 pandas/matplotlib "
                f"数据分析, 禁止 import 模块、读写删文件、执行系统命令等操作。")

    output_buf = io.StringIO()
    start_time = time.time()

    def _run() -> None:
        """在工作线程中执行代码(便于超时控制)"""
        namespace = {
            "pd": pd,
            "plt": plt,
            "DATA_DIR": DATA_DIR,
            "SAVE_DIR": SAVE_DIR,
            "__builtins__": _RESTRICTED_BUILTINS,  # 只暴露安全内置函数, 没有 open/import
        }
        try:
            with contextlib.redirect_stdout(output_buf), contextlib.redirect_stderr(output_buf):
                exec(compile(code, "<exec_python_code>", "exec"), namespace, namespace)
        except Exception:
            output_buf.write("\n[代码运行出错]\n")
            output_buf.write(traceback.format_exc(limit=5))
        finally:
            # 自动把仍未关闭的 matplotlib 图形保存成图片(模型常忘记写 savefig)
            try:
                for num in plt.get_fignums():
                    fig = plt.figure(num)
                    chart_path = os.path.join(SAVE_DIR, f"chart_{int(time.time() * 1000)}_{num}.png")
                    fig.savefig(chart_path, bbox_inches="tight")
                plt.close("all")
            except Exception:
                pass

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=EXEC_TIMEOUT)

    if worker.is_alive():
        return (f"【工具异常】代码执行超过 {EXEC_TIMEOUT} 秒被中止(疑似死循环或计算量过大), "
                f"请让模型检查并重写代码。已产生的输出:\n{output_buf.getvalue()[-500:]}")

    stdout_text = output_buf.getvalue().strip()

    # 收集本轮新生成的图表文件(用修改时间 >= 开始时间 判定, 覆盖模型自己 savefig 的情况)
    new_images = []
    try:
        for name in os.listdir(SAVE_DIR):
            if name.lower().endswith((".png", ".jpg", ".jpeg")):
                img_path = os.path.join(SAVE_DIR, name)
                if os.path.getmtime(img_path) >= start_time - 2:
                    new_images.append(img_path)
    except OSError:
        pass
    new_images = sorted(set(new_images))

    lines = ["——— 代码运行输出 ———"]
    lines.append(stdout_text if stdout_text else "(代码没有产生 print 输出)")
    if "[代码运行出错]" in stdout_text:
        lines.append("——— 提示: 上面的代码运行出错, 反思/下一轮请让模型修正代码 ———")
    if new_images:
        lines.append("——— 本轮生成的图表文件 ———")
        lines.extend(new_images)
    else:
        lines.append("(本轮没有生成图表; 需要图表时让模型用 plt 绘图并保存)")
    return "\n".join(lines)
