"""
core/config.py —— 项目统一环境变量配置解析(全库唯一出处)

设计说明(2026 工程重构 P1):
    - 只提供"解析函数", 不主动加载 .env——加载职责保留在各入口模块
      (main.py / graph_builder.py / tools.search_tool.py, 与旧行为完全一致);
    - 所有环境变量读取统一走本模块函数, 禁止业务代码散落
      int(os.getenv(...)) / float(os.getenv(...)) 等重复写法;
    - 非法值一律回退默认值、绝不抛异常(配置写错不阻塞启动), 语义与旧版
      graph_builder._env_int/_env_flag、tools.search_tool._env_int/_env_str
      逐一对齐, 纯重构不改行为。

函数速览:
    env_str(name, default="")   字符串配置: 读取后去首尾空白; 空值回退默认
    env_int(name, default)      整数配置: 无法解析为 int 时回退默认
    env_float(name, default)    浮点配置: 无法解析为 float 时回退默认
    env_flag(name, default=True)开关配置: 1/true/yes/on/y/t → True,
                                0/false/no/off/n/f → False, 其余回退默认
"""
import os
from typing import Union

_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "f"}


def env_str(name: str, default: str = "") -> str:
    """读取字符串型环境变量并去首尾空白; 未设置/为空串时返回 default。

    与原 tools/search_tool.py 的 _env_str 语义完全一致。
    """
    return (os.getenv(name, "") or default).strip()


def env_int(name: str, default: int) -> int:
    """读取整数型环境变量; 值缺失或无法解析为 int 时返回 default(不抛异常)。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    """读取浮点型环境变量; 值缺失或无法解析为 float 时返回 default(不抛异常)。"""
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def env_flag(name: str, default: bool = True) -> bool:
    """解析开关型环境变量(大小写不敏感)。

    与旧版 graph_builder._env_flag 语义完全一致:
        1/true/yes/on/y/t → True; 0/false/no/off/n/f → False;
        未设置或值无法识别 → 返回 default。
    """
    raw = (os.getenv(name, "") or "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


# 类型别名: 供其它模块做显式类型标注用(如 "值: Union[str, int]")
ConfigValue = Union[str, int, float, bool]
