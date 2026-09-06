"""
core/ingest.py —— 上传文件预读为素材(纯逻辑, 不依赖 Streamlit)

2026 工程重构 P1: 由 main.py「上传文件相关」一节迁出, 职责与行为完全不变:
    - PDF: 提取全文为素材文本(经 tools.pdf_reader, 只读 temp_upload/ 白名单);
    - CSV: 给出列名 + 行数 + 前 20 行结构预览(不进入模型前先了解字段结构);
    - 其他类型: 跳过并返回说明(不报错)。

对外函数:
    preview_csv(file_path, head_rows=20, max_chars=10000) -> str
        读取 CSV 并生成文本预览素材(兼容常见中文编码)
    ingest_upload(fname, temp_dir=None) -> (素材条目文本, 日志行)
        任务开始前自动预读上传文件; 空素材(跳过)时第一条返回 ""

说明: temp_dir 缺省为项目根目录 temp_upload/(与 tools.pdf_reader 白名单目录一致);
显式传入便于单元测试(临时目录)。PDF 提取始终以 tools.pdf_reader 的固定白名单目录
(temp_upload/)为准——与旧版 main.py 行为一致。
"""
import os

from core.file_store import DEFAULT_TEMP_UPLOAD_DIR


def preview_csv(file_path: str, head_rows: int = 20, max_chars: int = 10000) -> str:
    """读取 CSV 并生成文本预览素材(兼容常见中文编码)"""
    import pandas as pd  # 延迟导入: 未上传 CSV 时不必加载

    df = None
    errors = []
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin1"):
        try:
            df = pd.read_csv(file_path, encoding=encoding)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{encoding}: {exc}")
    if df is None:
        return f"【工具异常】CSV 读取失败(编码/格式无法识别): {errors[0] if errors else '未知错误'}"
    if df.shape[1] == 0 or df.shape[0] == 0:
        return "【提示】CSV 内容是空的(0 行 0 列)。"

    lines = [
        f"行数: {len(df)}  |  列数: {df.shape[1]}",
        f"列名: {', '.join(str(c) for c in df.columns)}",
        f"前 {head_rows} 行预览(用于了解字段结构):",
    ]
    lines.append(df.head(head_rows).to_string(index=False, max_colwidth=40))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n……(预览过长, 仅展示前 {max_chars} 字符)"
    return text


def ingest_upload(fname: str, temp_dir: str | None = None):
    """
    任务开始前自动预读上传文件 → 返回 (素材条目, 日志行)。

    PDF 提取全文(返回素材文本); CSV 给出结构预览(更深入的分析由工具节点用
    exec_python_code 完成); 其他类型返回 ("", 说明日志), 由调用方决定是否跳过。
    """
    upload_dir = temp_dir or DEFAULT_TEMP_UPLOAD_DIR
    path = os.path.join(upload_dir, fname)
    ext = os.path.splitext(fname)[1].lower()
    if ext == ".pdf":
        # PDF 提取固定走 tools.pdf_reader(白名单目录 temp_upload/, 与旧版一致);
        # 延迟导入: 不读 PDF 时无需加载 pypdf, 也便于测试替换该工具函数。
        from tools.pdf_reader import read_pdf

        text = read_pdf(fname)
        return f"【素材-上传PDF】文件: {fname}\n{text}", f"已自动预读上传的 PDF: {fname}"
    if ext == ".csv":
        text = preview_csv(path)
        return f"【素材-上传CSV预览】文件: {fname}\n{text}", f"已自动预读上传的 CSV: {fname}(结构预览见素材)"
    return "", f"跳过不支持的文件类型: {fname}(仅支持 PDF / CSV)"
