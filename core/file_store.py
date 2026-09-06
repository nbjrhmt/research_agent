"""
core/file_store.py —— 临时文件生命周期管理(纯逻辑, 不依赖 Streamlit)

2026 工程重构 P1: 由 main.py「🧹 临时文件生命周期管理」「上传文件相关」「partial 兜底」
等节迁出, 职责与行为完全不变:

    1. save_upload           上传文件落盘(文件名净化 + 随机前缀防碰撞);
    2. cleanup_temp_files    启动/任务前清理: 删除可清理文件、清空 .sandbox_runs、
                             保留 keep_files(当前结果面板仍引用的图表);
    3. delete_uploaded_files 调研结束后删除本次上传文件磁盘副本;
    4. save_partial_run      任务异常兜底: 已搜集素材落盘 partial_*.json(失败抛异常,
                             由调用方在 UI 显式提示, 不静默失败);
    5. find_new_images       本轮运行期间新生成的图表文件发现(供结果面板展示)。

★ 工程边界备注(与旧版 main.py 注释一致): 本模块函数只适合"单进程 Streamlit"模式。
   若多进程/多实例共享同一 temp_upload/ 目录, 清理(listdir 后逐个删除, 无跨进程锁)
   会出现竞争: 一个实例可能删除另一个实例刚生成/仍在使用的文件。多实例部署前需改为
   按任务目录隔离或引入互斥/引用计数, 当前版本不实现。

参数约定: 所有函数 temp_dir 缺省时使用项目根目录 temp_upload/(单用户本地原型约定);
显式传入便于单元测试(临时目录)。
"""
import json
import logging
import os
import re
import time
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TEMP_UPLOAD_DIR = os.path.join(PROJECT_ROOT, "temp_upload")  # 上传文件/图表存放目录

_logger = logging.getLogger("core.file_store")


def _resolve_dir(temp_dir: str | None) -> str:
    return temp_dir or DEFAULT_TEMP_UPLOAD_DIR


def save_upload(data, original_name: str, temp_dir: str | None = None) -> str:
    """
    把上传文件内容保存到 temp_upload/, 返回带随机前缀的落盘文件名。

    :param data:          文件二进制内容(支持 bytes / memoryview / 带 read() 的文件对象)
    :param original_name: 原始文件名(仅用于生成安全落盘名, 非法字符会被替换)
    :param temp_dir:      落盘目录, 缺省为项目根目录 temp_upload/
    :return:              落盘文件名(如 20260902_183000_a1b2c3_数据.csv)
    """
    upload_dir = _resolve_dir(temp_dir)
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", str(original_name or "upload"))  # 去掉不安全字符
    fname = f"{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}_{safe_name}"
    path = os.path.join(upload_dir, fname)
    with open(path, "wb") as f:
        if hasattr(data, "read"):          # 兼容文件对象
            f.write(data.read())
        else:
            f.write(bytes(data))           # bytes / memoryview
    return fname


def cleanup_temp_files(temp_dir: str | None = None, keep_files: list = None) -> None:
    """
    清理 temp_upload/ 下可删除的临时文件; keep_files: 需保留的绝对路径列表。

    删除失败只记日志, 不影响主流程; .sandbox_runs 子目录内的沙盒临时文件一并清理。
    """
    upload_dir = _resolve_dir(temp_dir)
    keep = set()
    for p in keep_files or []:
        try:
            keep.add(os.path.normcase(os.path.abspath(str(p))))
        except Exception:  # noqa: BLE001
            pass
    try:
        for name in os.listdir(upload_dir):
            item = os.path.join(upload_dir, name)
            if os.path.isdir(item):
                # 沙盒运行子目录: 只清内容, 目录保留(代码执行工具按需自建)
                if name == ".sandbox_runs":
                    for inner in os.listdir(item):
                        try:
                            os.remove(os.path.join(item, inner))
                        except OSError as exc:
                            _logger.warning("清理沙盒临时文件失败 %s: %s", inner, exc)
                continue
            if os.path.normcase(os.path.abspath(item)) in keep:
                continue
            try:
                os.remove(item)
            except OSError as exc:
                _logger.warning("清理临时文件失败 %s: %s", item, exc)
    except OSError as exc:
        _logger.warning("清理 temp_upload/ 失败: %s", exc)


def delete_uploaded_files(fnames: list, temp_dir: str | None = None) -> None:
    """调研结束后删除本次上传文件的磁盘副本(素材文本已在 State/历史中)。"""
    upload_dir = _resolve_dir(temp_dir)
    for fname in fnames or []:
        path = os.path.join(upload_dir, fname)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            _logger.warning("删除本次上传文件副本失败 %s: %s", path, exc)


def save_partial_run(state_values: dict, error_text: str = "", temp_dir: str | None = None) -> str:
    """
    任务异常兜底: 把已搜集素材(含上传文件预读素材)落盘为 partial JSON。

    任何异常路径(LLM 重试全失败 / 沙盒致命错误 / 搜索 API 报错 / 任务异常)只要
    能取回素材就必须落盘并提供下载, 杜绝静默失败; 落盘失败会抛异常, 由调用方在 UI
    明确提示"部分素材保存失败"(不掩盖原始任务错误)。
    """
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "任务异常中断时自动保存的已搜集素材(partial 兜底), 可直接阅读或复制给新任务使用",
        "error": error_text[:500] if error_text else "",
        "user_query": state_values.get("user_query", ""),
        "sub_tasks": state_values.get("sub_tasks") or [],
        "iteration_count": state_values.get("iteration_count") or 0,
        "collected_info": state_values.get("collected_info") or [],
    }
    upload_dir = _resolve_dir(temp_dir)
    os.makedirs(upload_dir, exist_ok=True)
    fname = f"partial_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json"
    path = os.path.join(upload_dir, fname)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001 —— 落盘失败必须抛给调用方在 UI 显式提示
        _logger.exception("保存部分成果失败: %s", exc)
        raise
    return path


def find_new_images(temp_dir: str | None = None, since_ts: float = 0.0,
                    exts: tuple = (".png", ".jpg", ".jpeg"), grace: float = 2.0) -> list:
    """
    发现 since_ts(含 grace 秒容差)之后生成的图表图片文件, 供结果面板展示。

    :param temp_dir:  扫描目录, 缺省为项目根目录 temp_upload/
    :param since_ts:  本轮任务开始时间戳(time.time()), 只返回此后的图片
    :param exts:      视为图表的扩展名集合(小写)
    :param grace:     时间容差秒数(避免同一秒内文件时间戳与起始戳相等被漏判)
    :return:          匹配图片的绝对路径列表(按文件名排序, 稳定展示顺序)
    """
    upload_dir = _resolve_dir(temp_dir)
    new_images = []
    try:
        for name in sorted(os.listdir(upload_dir)):
            if name.lower().endswith(tuple(ext.lower() for ext in exts)):
                img_path = os.path.join(upload_dir, name)
                if os.path.getmtime(img_path) >= since_ts - grace:
                    new_images.append(img_path)
    except OSError:
        pass
    return new_images
