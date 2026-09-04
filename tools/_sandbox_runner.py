"""
_sandbox_runner.py —— 代码沙盒的【子进程运行器】(由 code_exec_tool 通过 subprocess 调用)

为什么单独放一个子进程脚本:
    1. Python 线程无法被强制终止, 旧实现里死循环代码在 30 秒"超时"后仍作为
       daemon 线程在后台持续占用 CPU; 改为子进程后, 超时可以直接杀死进程,
       不存在后台残留线程问题;
    2. 子进程与主进程隔离, 沙盒代码崩溃/占用资源不会影响 Agent 主流程。

本文件内部职责(不要直接运行):
    1. 建立受限命名空间(pd / plt / DATA_DIR / SAVE_DIR + 安全内置函数白名单);
    2. 对 pandas 读写函数 / matplotlib 保存函数做【真实路径运行时校验】:
       只允许访问 temp_upload/ 目录内的路径, 其余(项目根、.env、任意磁盘路径)
       一律拦截 —— 这是对"目录白名单只停留在提示词层面"的修复;
    3. 执行模型代码, 把 stdout/stderr 合并输出到标准输出, 由父进程收集。

注意: 这仍是"本地原型级沙盒"(限制手段以受限内置函数 + 路径白名单为主),
不是生产级强安全隔离环境, 请勿部署到公网(README「已知项目局限」已声明)。

【P2 待优化点(本版本只记录、不实现)】缺少 CPU/内存(rlimit/容器配额)/进程树
限制, 无法防御 fork 炸弹与资源耗尽攻击; 未做系统调用过滤(seccomp)等强隔离。
"""
import contextlib
import io
import os
import sys
import time
import traceback

# ---- 无界面后端必须在 pyplot 使用前设置 ----
import matplotlib

matplotlib.use("Agg")

import matplotlib.figure  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# temp_upload 目录固定在本项目根目录下(tools 包的上一级)
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BASE_DIR, "temp_upload")  # 上传数据所在目录(注入给代码)
SAVE_DIR = DATA_DIR                                 # 图表保存目录(注入给代码)
_ALLOWED_ROOT = os.path.realpath(SAVE_DIR)          # 沙盒唯一允许访问的根目录


class SandboxPathError(RuntimeError):
    """沙盒路径越界拦截异常: 单独类型, 运行时不打印堆栈, 只输出拦截说明。"""


# ============================ 路径白名单校验 ============================
def _is_allowed(path: str) -> bool:
    """把路径解析为绝对真实路径后, 判断是否位于 temp_upload/ 内。"""
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
    except Exception:
        return False
    root = os.path.normcase(_ALLOWED_ROOT)
    target = os.path.normcase(resolved)
    return target == root or target.startswith(root + os.sep)


def _guard_path(path, op: str) -> str:
    """对文件路径参数做真实路径校验; 越界直接抛 SandboxPathError。

    支持 str / os.PathLike; 非本地文件类参数(如 io 对象、http 地址)原样放行。
    """
    if path is None:
        return path
    if not isinstance(path, (str, os.PathLike)):
        return path
    text = str(path)
    if "://" in text or text.startswith("data:"):  # 远程/内嵌数据地址, 无本地文件访问
        return path
    if not _is_allowed(text):
        raise SandboxPathError(
            "【安全拦截】文件路径越界: 沙盒只允许访问 temp_upload/ 目录内的文件"
            f"(被拒路径: {text[:120]})。禁止读取 .env 等项目文件或项目外路径。"
        )
    return path


# ---- pandas 读取类函数: 第一个路径参数强制走白名单 ----
_READERS = (
    "read_csv", "read_table", "read_fwf", "read_excel", "read_json",
    "read_parquet", "read_pickle", "read_html", "read_orc", "read_feather",
    "read_stata", "read_sas", "read_spss",
)


def _wrap_reader(func):
    """沙盒校验-运行时路径白名单(读取侧): 包装 pandas 读取函数, 越界路径直接拒绝。"""
    def _guarded(*args, **kwargs):
        if "filepath_or_buffer" in kwargs:
            kwargs["filepath_or_buffer"] = _guard_path(kwargs["filepath_or_buffer"], "read")
        elif "path" in kwargs:
            kwargs["path"] = _guard_path(kwargs["path"], "read")
        elif args and isinstance(args[0], (str, os.PathLike)):
            args = (_guard_path(args[0], "read"),) + args[1:]
        return func(*args, **kwargs)
    return _guarded


for _name in _READERS:
    if hasattr(pd, _name):
        setattr(pd, _name, _wrap_reader(getattr(pd, _name)))

# ---- DataFrame / Series 写出类方法: 目标路径强制走白名单 ----
_WRITER_ARG_KEYS = {
    "to_csv": "path_or_buf", "to_json": "path_or_buf", "to_parquet": "path",
    "to_pickle": "path", "to_feather": "path", "to_excel": "excel_writer",
}


def _wrap_writer(method_name, arg_key):
    """沙盒校验-运行时路径白名单(写出侧): 包装 DataFrame/Series 写出方法, 越界路径直接拒绝。"""
    def _decorator(func):
        def _guarded(self, *args, **kwargs):
            if arg_key in kwargs:
                kwargs[arg_key] = _guard_path(kwargs[arg_key], "write")
            elif args and isinstance(args[0], (str, os.PathLike)):
                args = (_guard_path(args[0], "write"),) + args[1:]
            return func(self, *args, **kwargs)
        return _guarded
    return _decorator


for _cls in (pd.DataFrame, getattr(pd, "Series", None)):
    if _cls is None:
        continue
    for _method, _arg_key in _WRITER_ARG_KEYS.items():
        if hasattr(_cls, _method):
            setattr(_cls, _method, _wrap_writer(_method, _arg_key)(getattr(_cls, _method)))

# ---- matplotlib 保存类函数: 输出路径强制走白名单 ----
_orig_plt_savefig = plt.savefig
_orig_figure_savefig = matplotlib.figure.Figure.savefig
_orig_plt_imread = None
if hasattr(plt, "imread"):
    _orig_plt_imread = plt.imread


def _guarded_plt_savefig(fname, *args, **kwargs):
    """沙盒校验-运行时路径白名单(绘图写出侧): plt.savefig 的目标路径必须位于 temp_upload/ 内。"""
    return _orig_plt_savefig(_guard_path(fname, "write"), *args, **kwargs)


def _guarded_figure_savefig(self, fname, *args, **kwargs):
    """沙盒校验-运行时路径白名单(绘图写出侧): Figure.savefig 与 plt.savefig 同规则。"""
    return _orig_figure_savefig(self, _guard_path(fname, "write"), *args, **kwargs)


def _guarded_plt_imread(fname, *args, **kwargs):
    """沙盒校验-运行时路径白名单(图片读取侧): plt.imread 只允许读取 temp_upload/ 内图片。"""
    return _orig_plt_imread(_guard_path(fname, "read"), *args, **kwargs)


plt.savefig = _guarded_plt_savefig
matplotlib.figure.Figure.savefig = _guarded_figure_savefig
if _orig_plt_imread is not None:
    plt.imread = _guarded_plt_imread


# ============================ 受限内置函数白名单 ============================
_SAFE_BUILTIN_NAMES = (
    "print len range enumerate zip map filter str int float bool list dict tuple set "
    "frozenset min max sum abs round sorted reversed any all isinstance type repr format "
    "divmod pow complex slice iter next chr ord hex oct bin hash id object range "
    "Exception ValueError TypeError KeyError IndexError RuntimeError AttributeError "
    "ZeroDivisionError ArithmeticError OverflowError NameError MemoryError "
    "NotImplementedError StopIteration FileNotFoundError"
).split()
_RESTRICTED_BUILTINS = {name: getattr(__builtins__, name) for name in _SAFE_BUILTIN_NAMES}


def main() -> int:
    """执行父进程传入的代码文件(唯一命令行参数), 输出合并到 stdout。"""
    if len(sys.argv) < 2:
        print("【工具异常】_sandbox_runner: 缺少代码文件参数")
        return 1
    code_path = sys.argv[1]
    try:
        with open(code_path, encoding="utf-8") as f:
            code = f.read()
    except OSError as exc:
        print(f"【工具异常】_sandbox_runner: 读取代码文件失败: {exc}")
        return 1

    output_buf = io.StringIO()
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
    except SandboxPathError as exc:
        output_buf.write("\n[安全拦截]\n")
        output_buf.write(str(exc))
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

    # 输出合并结果给父进程(父进程负责文件收集与格式化)
    sys.stdout.write(output_buf.getvalue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
