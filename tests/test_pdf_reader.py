"""
tests/test_pdf_reader.py —— tools/pdf_reader.py 离线单元测试(2026 工程重构 P2)

覆盖: 空文件名 / 非 PDF 扩展名 / 白名单目录缺文件 / 损坏 PDF / 真实空白 PDF
(解析成功但无可提取文字提示) / 逐页提取与损坏页容错 / max_chars 截断。
文件写入项目 temp_upload/(与 read_pdf 白名单目录一致), 用例结束即清理, 不触网。
"""
import io
import os
import uuid

import tools.pdf_reader as pr


def _unique_name(ext: str = ".pdf") -> str:
    return f"_unit_{uuid.uuid4().hex[:8]}{ext}"


def _write_upload(name: str, content: bytes) -> None:
    os.makedirs(pr.UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(pr.UPLOAD_DIR, name), "wb") as f:
        f.write(content)


def _remove_upload(name: str) -> None:
    try:
        os.remove(os.path.join(pr.UPLOAD_DIR, name))
    except OSError:
        pass


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        if self._text is None:
            raise RuntimeError("损坏页")
        return self._text


class _FakePdf:
    """替换 pypdf.PdfReader: 含损坏页, 验证逐页遍历与单页异常容错。"""

    def __init__(self, path):
        self.pages = [
            _FakePage("第一页正文"),
            _FakePage("第二页正文"),
            _FakePage(None),  # 损坏页: extract_text 抛异常应被吞掉, 不影响其余页
        ]


# ---------------- 参数与文件前置校验(不真正解析 PDF) ----------------
def test_read_pdf_empty_filename():
    result = pr.read_pdf("")
    assert result.startswith("【工具异常】") and "未提供文件名" in result


def test_read_pdf_rejects_non_pdf_extension():
    result = pr.read_pdf("notes.txt")
    assert result.startswith("【工具异常】") and "不是 PDF" in result


def test_read_pdf_missing_file():
    result = pr.read_pdf(_unique_name())  # temp_upload/ 下不存在
    assert result.startswith("【工具异常】") and "找不到文件" in result


# ---------------- 损坏文件 / 真实解析 ----------------
def test_read_pdf_damaged_content():
    name = _unique_name()
    _write_upload(name, b"%PDF-1.4 this is not a valid pdf body")
    try:
        result = pr.read_pdf(name)
        assert result.startswith("【工具异常】") and "读取失败" in result
    finally:
        _remove_upload(name)


def test_read_pdf_real_blank_pdf_gives_no_text_hint():
    # 用 pypdf 生成一张真实(无文字)PDF: 走"解析成功但没有可提取文字"提示分支
    from pypdf import PdfWriter

    name = _unique_name()
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    _write_upload(name, buf.getvalue())
    try:
        result = pr.read_pdf(name)
        assert "未能提取到文本" in result and "图片型 PDF" in result
    finally:
        _remove_upload(name)


# ---------------- 逐页提取 / 容错 / 截断(替换 pypdf.PdfReader 离线验证) ----------------
def test_read_pdf_pages_joined_and_truncated(monkeypatch):
    name = _unique_name()
    _write_upload(name, b"%PDF-1.4 placeholder (existence check; parse handled by Fake)")
    try:
        monkeypatch.setattr(pr, "PdfReader", _FakePdf)
        result = pr.read_pdf(name, max_chars=10)
        assert result.startswith("第一页正文\n\n第二")  # 截断到前 10 字符(含两页换行)
        assert "……(PDF 内容过长" in result              # 超长截断尾注
    finally:
        _remove_upload(name)


def test_read_pdf_short_content_kept_unchanged(monkeypatch):
    name = _unique_name()
    _write_upload(name, b"%PDF-1.4 placeholder")
    try:
        monkeypatch.setattr(pr, "PdfReader", _FakePdf)
        result = pr.read_pdf(name)  # 默认 max_chars=12000, 不截断
        assert "第一页正文\n\n第二页正文" in result
    finally:
        _remove_upload(name)
