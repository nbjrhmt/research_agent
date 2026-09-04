"""
logging_setup.py —— 项目统一日志配置(标准 logging 模块)

用法:
    from logging_setup import get_logger
    logger = get_logger(__name__)     # 自动完成一次初始化(logs/ 目录 + 文件输出)

特性:
    1. 日志同时输出到: 控制台(StreamHandler) + 文件 logs/app.log(RotatingFileHandler,
       单文件最大 1MB, 保留 5 个备份, UTF-8);
    2. 每条日志带时间戳、级别、模块名; 用 logger.exception() 调用时自动把完整
       异常堆栈写入日志文件;
    3. 幂等初始化: streamlit 每次交互重跑脚本 / 重复 import 不会重复添加 handler;
    4. logs/ 目录已列入 .gitignore, 不会入库。

★ 工程边界备注(RotatingFileHandler 多进程风险, 见 README「已知项目局限」):
    本配置以【单进程 Streamlit 模式】为前提。若切换为多进程/多实例部署, 多个进程会
    同时打开并轮转 logs/app.log —— RotatingFileHandler 的 rollover(rename + 新建)
    不是跨进程原子操作, 会出现日志互相覆盖/截断/文件损坏。多进程部署前应改用
    concurrent-log-handler(多进程安全按大小轮转)或集中式日志采集, 当前版本不实现。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_initialized = False


def setup_logging(console: bool = True) -> None:
    """配置根日志器(幂等)。console=False 时只写文件, 不输出到控制台。"""
    global _initialized
    if _initialized:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:  # 目录创建失败时至少不崩溃, 文件日志降级
        pass

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    _formatter = logging.Formatter(_LOG_FORMAT)

    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(_formatter)
        root.addHandler(file_handler)
    except OSError:
        pass  # 日志文件不可写时降级为仅控制台, 不影响业务

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(_formatter)
        root.addHandler(console_handler)

    _initialized = True


def get_logger(name: str = "app") -> logging.Logger:
    """获取子日志器(首次调用自动完成一次全局初始化)。"""
    setup_logging()
    return logging.getLogger(name)
