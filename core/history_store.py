"""
core/history_store.py —— 调研报告历史持久化(纯逻辑, 不依赖 Streamlit)

2026 工程重构 P1: 由 main.py「📜 历史报告存储」一节迁出, 职责与行为完全不变:
    1. 运行期历史保存在调用方(Streamlit session_state), 本模块只负责
       report_history.json 文件的读写与容错;
    2. 文件不存在 → 空列表(首次使用); 内容损坏/字段不完整/IO 异常 → 记警告并
       返回空列表(降级纯内存模式), 绝不抛异常打断启动;
    3. 只取前 MAX_HISTORY 条(文件按"新→旧"存储), 与内存丢弃规则一致;
    4. 每条记录字段: {id, topic, finished_at, file_stamp, report}。

对外函数:
    load_report_history_from_disk(file_path=None) -> list
    save_report_history_to_disk(history, file_path=None) -> None
    clear_report_history_on_disk(file_path=None) -> None
    new_report_record(topic, report) -> dict        记录构造(时间戳/文件名戳/id)
    clip_topic(topic, max_len=25) -> str            历史下拉框展示截断
    safe_filename_part(text, max_len=40) -> str     下载文件名安全化
    report_filename(record) -> str                  下载文件名(如 2026-09-02_192017_主题.md)

参数说明: file_path 缺省时使用项目根目录 report_history.json(单用户本地原型约定);
显式传入 file_path 便于单元测试(临时目录)。
"""
import json
import logging
import os
import time
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPORT_HISTORY_FILE = os.path.join(PROJECT_ROOT, "report_history.json")

MAX_HISTORY = 20  # 历史报告最大保留条数: 超出自动丢弃最老记录, 防止内存/文件无限膨胀

_logger = logging.getLogger("core.history_store")


def _resolve_path(file_path: str | None) -> str:
    return file_path or DEFAULT_REPORT_HISTORY_FILE


def load_report_history_from_disk(file_path: str | None = None) -> list:
    """
    启动时读取 report_history.json 恢复历史(仅首次加载页面/会话时调用一次)。
    - 文件不存在 → 返回空列表(视为首次使用);
    - 内容损坏 / 字段不完整 / IO 异常 → 打印警告并返回空列表, 降级纯内存模式, 不抛异常;
    - 每条记录字段(id/topic/finished_at/file_stamp/report)原样复用, 不做任何改写;
    - 只取前 MAX_HISTORY 条(文件按"新→旧"顺序存储), 与内存中的丢弃规则保持一致。
    """
    path = _resolve_path(file_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):   # 根节点必须是数组
            raise ValueError("文件内容不是 JSON 数组")
        valid = []
        for rec in data:                 # 逐条校验: 缺字段 / 字段类型异常视为损坏, 丢弃
            if isinstance(rec, dict) and all(
                isinstance(rec.get(key), str) for key in
                ("id", "topic", "finished_at", "file_stamp", "report")
            ):
                valid.append(rec)
        return valid[:MAX_HISTORY]
    except Exception as exc:  # noqa: BLE001 —— JSON 损坏 / 编码错误 / IO 异常等一律容错
        _logger.warning("读取 %s 失败(%s: %s), 已降级为内存模式, 本次启动历史为空。",
                        path, type(exc).__name__, exc)
        return []


def save_report_history_to_disk(history: list, file_path: str | None = None) -> None:
    """
    把当前完整 report_history 列表【覆盖】写入 report_history.json(每次变更后同步落盘)。
    写入失败(磁盘只读 / 权限 / 编码等异常)只打印警告, 历史仍保留在内存, 程序继续运行。
    """
    path = _resolve_path(file_path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("写入 %s 失败(%s: %s), 历史仅保留在内存中, 重启后可能丢失。",
                        path, type(exc).__name__, exc)


def clear_report_history_on_disk(file_path: str | None = None) -> None:
    """【清空全部历史】时同步清空 report_history.json(文件保留但内容为空数组)。"""
    save_report_history_to_disk([], file_path=file_path)


def new_report_record(topic: str, report: str) -> dict:
    """
    构造一条历史记录(与旧版 main.py 逐字段一致, 供调用方插入内存历史并落盘):
        {"id": 唯一id, "topic": 调研主题, "finished_at": 完成时间,
         "file_stamp": 下载文件名时间戳(如 2026-09-02_192017), "report": 完整markdown报告文本}
    """
    finished_at = time.strftime("%Y-%m-%d %H:%M:%S")   # 展示用完成时间, 与结果面板同格式
    return {
        "id": uuid.uuid4().hex[:10],                   # 唯一 id, 供下拉框稳定定位记录
        "topic": topic,
        "finished_at": finished_at,
        "file_stamp": finished_at[:10] + "_" + finished_at[11:].replace(":", ""),  # 2026-09-02_192017
        "report": report,                              # 完整 markdown 报告文本
    }


def clip_topic(topic: str, max_len: int = 25) -> str:
    """截断主题用于下拉框展示: 主题最多保留 max_len 个字符, 超出加省略号"""
    topic = (topic or "").strip()
    if not topic:
        return "(空主题)"
    return topic if len(topic) <= max_len else topic[:max_len] + "…"


def safe_filename_part(text: str, max_len: int = 40) -> str:
    """把主题加工成安全的下载文件名主干: 仅剔除 Windows/Unix 文件名非法字符, 保留中文"""
    import re  # 仅本函数使用, 延迟导入保持模块顶部轻量

    part = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text).strip(" ._")
    part = re.sub(r"_+", "_", part)   # 合并连续下划线
    return (part[:max_len].rstrip(" ._")) or "report"


def report_filename(record: dict) -> str:
    """生成下载文件名, 格式示例: 2026-09-02_192017_2026年国内开源大模型最新进展对比.md"""
    return f"{record['file_stamp']}_{safe_filename_part(record['topic'])}.md"
