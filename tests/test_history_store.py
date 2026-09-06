"""
tests/test_history_store.py —— core/history_store.py 历史持久化离线单元测试
(2026 工程重构 P2, 由 main.py 迁出的纯逻辑, 全部使用 workdir 临时目录)

覆盖: 文件缺失/损坏/字段不完整容错、MAX_HISTORY 裁剪、round-trip 落盘、
写入失败降级、记录构造字段格式、下载文件名辅助函数。
"""
import json
import re

import core.history_store as hs


# ---------------- 读取容错 ----------------
def test_load_missing_file_returns_empty(workdir):
    assert hs.load_report_history_from_disk(str(workdir / "nope.json")) == []


def test_load_corrupt_json_returns_empty(workdir):
    bad = workdir / "history.json"
    bad.write_text("{ 这不是合法 JSON !!!", encoding="utf-8")
    assert hs.load_report_history_from_disk(str(bad)) == []


def test_load_non_list_root_returns_empty(workdir):
    bad = workdir / "history.json"
    bad.write_text('{"not": "a list"}', encoding="utf-8")
    assert hs.load_report_history_from_disk(str(bad)) == []


def _valid_record(rid: str, topic: str = "主题") -> dict:
    return {
        "id": rid,
        "topic": topic,
        "finished_at": "2026-09-01 10:00:00",
        "file_stamp": "2026-09-01_100000",
        "report": f"# 报告 {rid}",
    }


def test_load_filters_invalid_records(workdir):
    path = workdir / "history.json"
    data = [
        _valid_record("ok1"),
        {"id": "no-topic", "finished_at": "2026-09-01 10:00:00", "file_stamp": "x", "report": "r"},  # 缺 topic
        {"id": "bad-ts", "topic": "t", "finished_at": 123, "file_stamp": "x", "report": "r"},  # 类型错
        ["not", "a", "dict"],
        _valid_record("ok2"),
    ]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = hs.load_report_history_from_disk(str(path))
    assert [rec["id"] for rec in loaded] == ["ok1", "ok2"]


def test_load_caps_at_max_history(workdir, monkeypatch):
    monkeypatch.setattr(hs, "MAX_HISTORY", 3)
    path = workdir / "history.json"
    path.write_text(json.dumps([_valid_record(f"r{i}") for i in range(8)], ensure_ascii=False),
                    encoding="utf-8")
    loaded = hs.load_report_history_from_disk(str(path))
    assert len(loaded) == 3
    assert [rec["id"] for rec in loaded] == ["r0", "r1", "r2"]  # 按文件顺序取前 N 条


# ---------------- 写入 / 清空 ----------------
def test_save_and_load_roundtrip(workdir):
    path = workdir / "history.json"
    records = [_valid_record("a1"), _valid_record("b2")]
    hs.save_report_history_to_disk(records, file_path=str(path))
    assert hs.load_report_history_from_disk(str(path)) == records


def test_save_failure_degrades_without_raise(workdir, caplog):
    # file_path 指向一个目录 → open(..., "w") 抛 IsADirectoryError, 只记警告不抛出
    bad_dir = workdir / "as_file"
    bad_dir.mkdir()
    hs.save_report_history_to_disk([], file_path=str(bad_dir))
    assert any("写入" in r.getMessage() for r in caplog.records)


def test_clear_writes_empty_array(workdir):
    path = workdir / "history.json"
    hs.save_report_history_to_disk([_valid_record("a1")], file_path=str(path))
    hs.clear_report_history_on_disk(file_path=str(path))
    assert hs.load_report_history_from_disk(str(path)) == []


# ---------------- 记录构造与文件名辅助 ----------------
def test_new_report_record_fields():
    record = hs.new_report_record("新能源汽车调研", "# 正文")
    assert set(record) == {"id", "topic", "finished_at", "file_stamp", "report"}
    assert record["topic"] == "新能源汽车调研"
    assert record["report"] == "# 正文"
    assert len(record["id"]) == 10 and re.fullmatch(r"[0-9a-f]{10}", record["id"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", record["finished_at"])
    assert record["file_stamp"] == record["finished_at"][:10] + "_" + record["finished_at"][11:].replace(":", "")


def test_clip_topic_truncates_long():
    assert hs.clip_topic("x" * 30) == "x" * 25 + "…"


def test_clip_topic_short_kept_and_empty_fallback():
    assert hs.clip_topic("短主题") == "短主题"
    assert hs.clip_topic("") == "(空主题)"
    assert hs.clip_topic("   ") == "(空主题)"


def test_safe_filename_part_removes_illegal_chars():
    assert ":" not in hs.safe_filename_part('2026:报告?<a>|b')
    assert hs.safe_filename_part('a\\/:*?"<>|b') == "a_b"


def test_safe_filename_part_empty_and_truncation():
    assert hs.safe_filename_part("") == "report"
    assert hs.safe_filename_part("   ") == "report"
    assert len(hs.safe_filename_part("字" * 100)) <= 40


def test_report_filename_format():
    record = _valid_record("x1", topic="2026 大模型")
    record["file_stamp"] = "2026-09-02_192017"
    name = hs.report_filename(record)
    assert name == "2026-09-02_192017_2026 大模型.md"
