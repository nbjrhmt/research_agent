"""
tests/test_ingest.py —— core/ingest.py 上传文件预读离线单元测试(2026 工程重构 P2)

覆盖: CSV 多编码(utf-8 / gbk)预览与"列名+行数+前N行"结构、空 CSV 提示、
超长预览截断、非法类型跳过、缺失文件工具异常、PDF 分支(替换 read_pdf 后断言
调用与素材格式)。全部离线, 使用 workdir 临时目录。
"""
import tools.pdf_reader as pdf_reader_mod  # 供 monkeypatch 替换 read_pdf
from core.ingest import ingest_upload, preview_csv


def _write_csv(workdir, name: str, content: str, encoding: str = "utf-8") -> str:
    path = workdir / name
    path.write_bytes(content.encode(encoding))
    return str(path)


# ---------------- ingest_upload: CSV ----------------
def test_ingest_csv_utf8(workdir):
    _write_csv(workdir, "sales.csv", "year,sales\n2023,100\n2024,200\n", encoding="utf-8")
    material, log = ingest_upload("sales.csv", temp_dir=str(workdir))
    assert material.startswith("【素材-上传CSV预览】文件: sales.csv")
    assert "行数: 2" in material and "列名: year, sales" in material
    assert "前 20 行预览" in material
    assert "已自动预读上传的 CSV" in log


def test_ingest_csv_gbk_encoding(workdir):
    # GBK 编码中文表头: 多编码回退链必须能读出
    _write_csv(workdir, "gbk.csv", "品牌,销量\n比亚迪,100\n", encoding="gbk")
    material, _log = ingest_upload("gbk.csv", temp_dir=str(workdir))
    assert "品牌" in material and "比亚迪" in material


def test_ingest_unsupported_type_skipped(workdir):
    (workdir / "note.txt").write_text("hello", encoding="utf-8")
    material, log = ingest_upload("note.txt", temp_dir=str(workdir))
    assert material == ""
    assert "跳过不支持的文件类型" in log


def test_ingest_missing_csv_returns_tool_error(workdir):
    # 与旧版 main.py 行为一致: 读取失败时素材 = 预览前缀 + 【工具异常】正文(不抛异常)
    material, log = ingest_upload("nope.csv", temp_dir=str(workdir))
    assert material.startswith("【素材-上传CSV预览】文件: nope.csv")
    assert "【工具异常】CSV 读取失败" in material
    assert log  # 日志行仍然给出


# ---------------- ingest_upload: PDF(替换 read_pdf 离线验证) ----------------
def test_ingest_pdf_calls_reader_and_formats_material(workdir, monkeypatch):
    (workdir / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    calls = []

    def fake_read_pdf(filename: str, max_chars: int = 12000) -> str:
        calls.append(filename)
        return "这是PDF正文"

    monkeypatch.setattr(pdf_reader_mod, "read_pdf", fake_read_pdf)
    material, log = ingest_upload("doc.pdf", temp_dir=str(workdir))
    assert calls == ["doc.pdf"]
    assert material == "【素材-上传PDF】文件: doc.pdf\n这是PDF正文"
    assert "已自动预读上传的 PDF" in log


# ---------------- preview_csv ----------------
def test_preview_csv_header_only_reports_empty(workdir):
    path = _write_csv(workdir, "empty.csv", "a,b\n")  # 解析成功但 0 行
    text = preview_csv(path)
    assert text.startswith("【提示】CSV 内容是空的")


def test_preview_csv_truncates_when_too_long(workdir):
    rows = "\n".join(f"{i},value{i}" for i in range(500))
    path = _write_csv(workdir, "big.csv", "id,val\n" + rows)
    text = preview_csv(path, max_chars=100)
    assert "……(预览过长" in text
    assert len(text) < 300


def test_preview_csv_reads_gbk_directly(workdir):
    path = _write_csv(workdir, "gbk2.csv", "指标,数值\n毛利率,0.3\n", encoding="gbk")
    text = preview_csv(path)
    assert "毛利率" in text and "行数: 1" in text
