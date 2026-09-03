"""
pdf_reader.py —— PDF 文本读取工具

安全规则:
    - 只允许读取 temp_upload/ 目录下、由本次任务上传的 PDF 文件
    - 只读操作, 绝不修改 / 删除任何文件
"""
import os

from pypdf import PdfReader

# temp_upload 目录固定在本项目根目录下(tools 包的上一级)
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_upload")

# 单次最多提取的字符数, 防止超大 PDF 撑爆模型上下文
MAX_CHARS = 12000


def read_pdf(filename: str, max_chars: int = MAX_CHARS) -> str:
    """
    读取上传的 PDF 并返回其文本内容。

    :param filename:  文件名(只取 basename, 防止路径穿越; 必须真实存在于 temp_upload/)
    :param max_chars:  文本截断上限
    :return:           纯文本素材; 失败时返回以【工具异常】开头的说明文字
    """
    filename = os.path.basename(str(filename or "").strip())  # 防路径穿越
    if not filename:
        return "【工具异常】read_pdf: 未提供文件名。"

    if not filename.lower().endswith(".pdf"):
        return f"【工具异常】read_pdf: {filename} 不是 PDF 文件, 只支持读取 .pdf。"

    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.isfile(file_path):
        return f"【工具异常】read_pdf: temp_upload/ 下找不到文件「{filename}」, 只能读取本次任务上传的文件。"

    try:
        reader = PdfReader(file_path)
        parts = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:  # 个别损坏页面不影响整体
                text = ""
            if text.strip():
                parts.append(text.strip())
        content = "\n\n".join(parts).strip()
    except Exception as exc:
        return f"【工具异常】read_pdf 读取失败(文件可能已损坏或加密): {type(exc).__name__}: {exc}"

    if not content:
        return f"【提示】「{filename}」未能提取到文本, 可能是扫描件/图片型 PDF, 没有可直接使用的文字内容。"

    if len(content) > max_chars:
        content = content[:max_chars] + f"\n……(PDF 内容过长, 仅展示前 {max_chars} 字符, 全文共 {len(content)} 字符)"

    return content
