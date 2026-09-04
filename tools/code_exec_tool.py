"""
code_exec_tool.py —— 受限 Python 代码执行工具(仅数据分析/绘图)

用途: 让 Agent 用 pandas / matplotlib 对上传的 CSV 做统计、计算、绘图。

安全与健壮性设计(2026 加固):
    1. 只注入 pd / plt / DATA_DIR / SAVE_DIR 四个名字, 模型代码无需也不能 import;
    2. 文本黑名单: 禁止 import 任何模块、禁止 eval/exec/compile/open/input 等,
       禁止文件删除/改名等操作 API, 禁止 os/sys/subprocess/shutil/socket 等;
    3. 【真实路径校验】(运行时, 在子进程内强制): pandas 读写、matplotlib 保存的
       路径参数必须解析后位于 temp_upload/ 目录内, 禁止读取 .env 等项目文件、
       禁止访问项目外任何路径 —— 修复"目录白名单只停留在提示词层面"的缺陷;
    4. 静态预检: 代码字符串中若出现 ".." 路径穿越、绝对路径、".env" 等字样,
       整段拒绝执行(第一道防线, 与第 3 条运行时校验互为补充);
    5. 【子进程隔离 + 超时强杀】: 代码在独立子进程(site-packages 隔离)中执行,
       超时直接杀死进程 —— 修复旧实现 daemon 线程超时后无法终止、
       死循环代码在后台残留占用 CPU 的问题; 子进程崩溃不影响 Agent 主流程;
    6. 代码长度上限 4000 字符。

注意: 这是学习用原型沙盒, 不是强安全隔离环境, 请勿部署到公网。

【P2 待优化点(本版本只记录、不实现, 见 README/CHANGELOG)】
    - 缺少 CPU / 内存配额与进程数限制: 无法防御 fork 炸弹与资源耗尽攻击
      (子进程超时仅拦截死循环, 不限制内存暴涨/子进程树扩散);
    - 静态黑名单只是简单文本字面拦截: 可被字符串拼接(运行时真实路径白名单是兜底
      防线, 但无法覆盖 exec/eval/动态 import/变量别名等全部混淆手法)。
"""
import os
import re
import subprocess
import sys
import time
import uuid

from logging_setup import get_logger

_logger = get_logger("code_exec_tool")

# temp_upload 目录固定在本项目根目录下(code_exec_tool 的上一级)
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BASE_DIR, "temp_upload")  # 上传数据所在目录(注入给代码)
SAVE_DIR = DATA_DIR                                 # 图表保存目录(注入给代码)
SANDBOX_RUN_DIR = os.path.join(DATA_DIR, ".sandbox_runs")  # 子进程代码/输出临时目录
_RUNNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sandbox_runner.py")

EXEC_TIMEOUT = 30      # 秒(子进程执行超时, 超时直接杀死)
CODE_MAX_CHARS = 4000  # 字符
_MAX_OUTPUT_CHARS = 200_000  # 输出最多保留的字符数(防刷屏/防撑爆上下文)

# Windows 下不弹黑窗
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------- 文本黑名单: 命中即整段拒绝执行 ----------------
_BANNED_PATTERNS = [
    r"\b__import__\b",                                   # 动态导入
    r"\b(?:import|from)\s+(?:os|sys|shutil|subprocess|socket|pathlib|requests|"
    r"urllib|ftplib|http|pickle|ctypes|multiprocessing|threading|importlib|builtins|"
    r"pandas|numpy|scipy|matplotlib|seaborn|sklearn|json|csv|io|base64|datetime|"
    r"random|math|re|time)\b",                           # 一切系统/IO/重库模块 import
    r"\b(eval|exec|compile|open|input|exit|quit|breakpoint)\s*\(",
    r"\b(?:os|sys|shutil|subprocess)\s*\.",              # 任何系统模块调用
    r"\.(?:remove|unlink|rmdir|mkdir|makedirs|rename|replace|write|truncate|"
    r"chmod|chown|symlink|link|rmtree)\s*\(",            # 文件删除/修改类 API
    r"\.(?:system|popen|run|call|check_output|Popen)\s*\(",
    r"while\s+True",                                     # 疑似死循环
    # ---- 路径越权静态预检(第一道防线, 运行时还有真实路径校验兜底) ----
    r"['\"][^'\"]*\.\.[\\/][^'\"]*['\"]",                # 字符串字面量中的 ../ 或 ..\ 穿越
    r"\.env",                                            # 任何对 .env 密钥文件的引用
]


def _check_blocked(code: str):
    """沙盒校验-第一道防线: 对整段代码做静态黑名单正则扫描。

    :param code: 模型提交的待执行代码原文
    :return: (是否违规, 命中的违规片段); 违规片段用于给模型可读的拦截原因。
    设计说明: 黑名单只是文本字面拦截, 可被拼接/混淆绕过 —— 因此它只负责"提前拒绝明显
    恶意代码、省一次子进程", 真正的安全兜底是子进程内的运行时真实路径白名单
    (_sandbox_runner._guard_path), 二者构成双层校验。
    """
    for pattern in _BANNED_PATTERNS:
        match = re.search(pattern, code, re.IGNORECASE)
        if match:
            return True, match.group(0)[:80]
    return False, ""


def _write_code_file(code: str) -> str:
    """把待执行代码写入沙盒临时目录, 返回文件路径。"""
    try:
        os.makedirs(SANDBOX_RUN_DIR, exist_ok=True)
    except OSError as exc:
        _logger.warning("创建沙盒临时目录失败: %s", exc)
        return ""
    name = f"code_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.py"
    path = os.path.join(SANDBOX_RUN_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
    except OSError as exc:
        _logger.warning("写入沙盒代码文件失败: %s", exc)
        return ""
    return path


def exec_python_code(code: str) -> str:
    """
    在受限子进程沙盒中执行数据分析代码(超时直接杀死, 不残留后台线程)。

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
                f"数据分析, 禁止 import 模块、禁止访问 temp_upload/ 之外的路径(含 .env "
                f"密钥文件)、禁止读写删文件、禁止执行系统命令等操作。")

    # ---- 写临时代码文件, 交给子进程执行(超时强杀, 无残留线程) ----
    code_path = _write_code_file(code)
    if not code_path:
        return ("【工具异常】exec_python_code: 无法在 temp_upload/.sandbox_runs/ 下创建临时文件, "
                "请检查磁盘空间与目录权限。")
    out_path = code_path[:-3] + ".out"
    start_time = time.time()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # 子进程输出统一 UTF-8, 避免 Windows 编码错乱
    env["PYTHONUNBUFFERED"] = "1"
    timed_out = False
    try:
        with open(out_path, "wb") as out_f:
            try:
                subprocess.run(
                    [sys.executable, _RUNNER_PATH, code_path],
                    stdout=out_f,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    timeout=EXEC_TIMEOUT,
                    cwd=_BASE_DIR,
                    env=env,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
    except OSError as exc:
        _logger.exception("启动沙盒子进程失败")
        _cleanup_files(code_path, out_path)
        return f"【工具异常】exec_python_code: 沙盒子进程启动失败: {type(exc).__name__}: {exc}"

    if timed_out:
        _logger.warning("代码执行超过 %s 秒被杀死(疑似死循环或计算量过大)", EXEC_TIMEOUT)
        stdout_text = _read_output_tail(out_path)
        _cleanup_files(code_path, out_path)
        return (f"【工具异常】代码执行超过 {EXEC_TIMEOUT} 秒已被强制终止(子进程已杀死, "
                f"疑似死循环或计算量过大), 请让模型检查并重写代码。已产生的输出:\n"
                f"{stdout_text[-500:] if stdout_text else '(无输出)'}")

    stdout_text = _read_output_tail(out_path).strip()  # 先读输出, 再删临时文件
    _cleanup_files(code_path, out_path)

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


def _read_output_tail(out_path: str) -> str:
    """读取子进程输出文件(截断到 _MAX_OUTPUT_CHARS, 防刷屏)。"""
    try:
        with open(out_path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[-_MAX_OUTPUT_CHARS:] + "\n……(输出过长, 仅展示尾部内容)"
    return text


def _cleanup_files(code_path: str, out_path: str) -> None:
    """删除子进程临时文件(任务结束自动清理, 不留残留)。"""
    for path in (code_path, out_path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            _logger.warning("清理沙盒临时文件失败 %s: %s", path, exc)
