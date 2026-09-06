"""
tests/conftest.py —— pytest 共享配置与项目路径引导(2026 工程重构 P2/P4)

职责:
    1. 把项目根目录加入 sys.path: 供"未执行 pip install -e ."的环境(如 CI 仅安装
       requirements-lock.txt)也能直接导入根目录平铺模块(graph_builder / state_schema /
       logging_setup)与源码包(core / tools / memory);
    2. 收敛各测试文件重复的 sys.path 硬编码——旧版每个 test_*.py 文件头部各写一份
       "项目根目录插入 sys.path", 现统一在本文件维护一份, 各测试文件不再重复;
    3. 提供 workdir fixture: 在项目根目录 .pytest_unit_tmp/ 内为每个用例创建独立的
       临时工作目录并自动清理——不依赖系统 TEMP(受限/沙盒环境 TEMP 不可写时用例
       仍可运行), 也不依赖 pytest 自身的 basetemp 目录扫描机制。

说明: conftest.py 由 pytest 在任何用例执行前自动加载(先于测试模块 import),
因此测试模块顶部直接 import 项目模块即可, 无需再自行插路径。
"""
import os
import pathlib
import shutil
import uuid

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_sys_path() -> None:
    import sys

    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)


_ensure_sys_path()


@pytest.fixture
def workdir():
    """独立临时工作目录(pathlib.Path), 用例结束后自动删除。

    目录创建于项目根 .pytest_unit_tmp/(已 gitignore), 全部离线文件操作都落在
    项目工作区内, 不触碰系统 TEMP。注: 刻意不用 tempfile.mkdtemp——其 0700
    目录权限在受限/沙盒环境(Windows 部分 CI 容器/安全软件)下可能导致新建
    目录不可写, 这里用默认权限 makedirs + uuid 命名保证可写性。
    """
    parent = os.path.join(_PROJECT_ROOT, ".pytest_unit_tmp")
    os.makedirs(parent, exist_ok=True)
    d = os.path.join(parent, "test_" + uuid.uuid4().hex[:8])
    os.makedirs(d, exist_ok=False)
    yield pathlib.Path(d)
    shutil.rmtree(d, ignore_errors=True)
