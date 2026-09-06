"""
tests/test_file_store.py —— core/file_store.py 临时文件生命周期离线单元测试
(2026 工程重构 P2, 由 main.py 迁出的纯逻辑, 全部使用 workdir 临时目录)

覆盖: 上传落盘(文件名净化/内容)、任务清理(keep_files 保留 / .sandbox_runs 清空 /
缺失目录容错)、上传副本删除、partial 素材快照落盘与失败抛出、运行期新图表发现。
"""
import json
import os
import time

import pytest

import core.file_store as fs


# ---------------- 上传落盘 ----------------
def test_save_upload_sanitizes_name_and_writes_content(workdir):
    fname = fs.save_upload(b"csv,data\n1,2", 'a/b\\c?.csv', temp_dir=str(workdir))
    # 不安全字符全部替换为下划线, 随机前缀 + 时间戳 + 安全名
    assert fname.endswith("_a_b_c_.csv")
    saved = list(workdir.iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"csv,data\n1,2"


def test_save_upload_accepts_memoryview_and_filelike(workdir):
    # Streamlit UploadedFile.getbuffer() 返回 memoryview
    f1 = fs.save_upload(memoryview(b"mv-data"), "mv.bin", temp_dir=str(workdir))
    assert (workdir / f1).read_bytes() == b"mv-data"
    # 也兼容带 read() 的文件对象
    import io

    f2 = fs.save_upload(io.BytesIO(b"file-data"), "fl.bin", temp_dir=str(workdir))
    assert (workdir / f2).read_bytes() == b"file-data"


# ---------------- 清理 ----------------
def test_cleanup_removes_stale_keeps_keep_files(workdir):
    a = workdir / "a.csv"
    b = workdir / "b.png"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    sandbox = workdir / ".sandbox_runs"
    sandbox.mkdir()
    inner = sandbox / "tmp_code.py"
    inner.write_text("x = 1", encoding="utf-8")

    fs.cleanup_temp_files(temp_dir=str(workdir), keep_files=[str(b)])

    assert not a.exists()          # 过期文件被清理
    assert b.exists()              # keep_files 保留
    assert sandbox.exists() and not inner.exists()  # 沙盒目录保留、内容清空


def test_cleanup_missing_dir_is_silent(workdir):
    # 目录不存在时只记日志不抛异常(与旧版 main.py 行为一致)
    fs.cleanup_temp_files(temp_dir=str(workdir / "nope"))


def test_delete_uploaded_files_only_removes_listed(workdir):
    a = workdir / "a.pdf"
    b = workdir / "b.pdf"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    fs.delete_uploaded_files(["a.pdf", "missing.pdf"], temp_dir=str(workdir))
    assert not a.exists()
    assert b.exists()  # 未列出文件不受影响


# ---------------- partial 素材快照 ----------------
def test_save_partial_run_writes_payload(workdir):
    state = {
        "user_query": "调研主题",
        "sub_tasks": ["任务A"],
        "iteration_count": 3,
        "collected_info": ["素材1", "素材2"],
    }
    path = fs.save_partial_run(state, error_text="RuntimeError: boom", temp_dir=str(workdir))
    assert path.startswith(str(workdir))
    assert path.endswith(".json")
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["user_query"] == "调研主题"
    assert data["sub_tasks"] == ["任务A"]
    assert data["iteration_count"] == 3
    assert data["collected_info"] == ["素材1", "素材2"]
    assert data["error"] == "RuntimeError: boom"
    assert "note" in data and data["saved_at"]


def test_save_partial_run_error_text_capped_at_500_chars(workdir):
    path = fs.save_partial_run({"collected_info": ["m"]}, error_text="E" * 900,
                               temp_dir=str(workdir))
    data = json.loads(open(path, encoding="utf-8").read())
    assert len(data["error"]) == 500


def test_save_partial_run_raises_on_io_failure(workdir):
    # temp_dir 是一个普通文件 → makedirs/exist_ok 抛 FileExistsError, 必须上抛不静默
    blocker = workdir / "not_a_dir"
    blocker.write_bytes(b"x")
    with pytest.raises(Exception):
        fs.save_partial_run({"collected_info": ["m"]}, temp_dir=str(blocker))


# ---------------- 运行期新图表发现 ----------------
def test_find_new_images_filters_by_time_ext_and_sorts(workdir):
    old = workdir / "old.png"
    old.write_bytes(b"1")
    os_ts = time.time() - 100
    os.utime(old, (os_ts, os_ts))  # 修改时间戳: 100 秒前(旧图)
    b = workdir / "b.png"
    b.write_bytes(b"2")
    a = workdir / "a.jpg"
    a.write_bytes(b"3")
    (workdir / "note.txt").write_text("x", encoding="utf-8")  # 非图片应被忽略

    since = time.time() - 10
    found = fs.find_new_images(temp_dir=str(workdir), since_ts=since)
    assert [str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in found] == ["a.jpg", "b.png"]  # 排序稳定


def test_find_new_images_missing_dir_is_empty(workdir):
    assert fs.find_new_images(temp_dir=str(workdir / "nope")) == []
