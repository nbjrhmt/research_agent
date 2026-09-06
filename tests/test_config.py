"""
tests/test_config.py —— core/config.py 统一环境变量解析离线单元测试(2026 工程重构 P2)

覆盖: env_str / env_int / env_float / env_flag 的取值、缺省回退、非法值回退语义,
与旧版 graph_builder._env_flag、tools.search_tool._env_int/_env_str 行为逐一对齐。
全部通过 monkeypatch 操作环境变量完成, 不产生任何真实 API 调用。
"""
import core.config as cfg


# ---------------- env_str: 字符串 ----------------
def test_env_str_reads_trimmed_value(monkeypatch):
    monkeypatch.setenv("CFG_T_S", "  hello world  ")
    assert cfg.env_str("CFG_T_S") == "hello world"


def test_env_str_missing_uses_default(monkeypatch):
    monkeypatch.delenv("CFG_T_S", raising=False)
    assert cfg.env_str("CFG_T_S", "fallback") == "fallback"


def test_env_str_empty_value_falls_back_to_default(monkeypatch):
    # 显式设为空串等价于未配置(旧版 (os.getenv(...) or default) 语义)
    monkeypatch.setenv("CFG_T_S", "")
    assert cfg.env_str("CFG_T_S", "fallback") == "fallback"


def test_env_str_default_when_unset_is_empty(monkeypatch):
    monkeypatch.delenv("CFG_T_S", raising=False)
    assert cfg.env_str("CFG_T_S") == ""


# ---------------- env_int: 整数 ----------------
def test_env_int_reads_value(monkeypatch):
    monkeypatch.setenv("CFG_T_I", "42")
    assert cfg.env_int("CFG_T_I", 7) == 42


def test_env_int_invalid_value_falls_back(monkeypatch):
    monkeypatch.setenv("CFG_T_I", "not-a-number")
    assert cfg.env_int("CFG_T_I", 7) == 7


def test_env_int_missing_falls_back(monkeypatch):
    monkeypatch.delenv("CFG_T_I", raising=False)
    assert cfg.env_int("CFG_T_I", 7) == 7


def test_env_int_empty_value_falls_back(monkeypatch):
    monkeypatch.setenv("CFG_T_I", "")
    assert cfg.env_int("CFG_T_I", 7) == 7


# ---------------- env_float: 浮点 ----------------
def test_env_float_reads_value(monkeypatch):
    monkeypatch.setenv("CFG_T_F", "3.5")
    assert cfg.env_float("CFG_T_F", 1.0) == 3.5


def test_env_float_invalid_value_falls_back(monkeypatch):
    monkeypatch.setenv("CFG_T_F", "abc")
    assert cfg.env_float("CFG_T_F", 1.0) == 1.0


def test_env_float_missing_falls_back(monkeypatch):
    monkeypatch.delenv("CFG_T_F", raising=False)
    assert cfg.env_float("CFG_T_F", 1.0) == 1.0


# ---------------- env_flag: 开关 ----------------
def test_env_flag_true_forms(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on", "y", "t"):
        monkeypatch.setenv("CFG_T_B", value)
        assert cfg.env_flag("CFG_T_B") is True, f"value={value}"


def test_env_flag_false_forms(monkeypatch):
    for value in ("0", "false", "FALSE", "no", "off", "n", "f"):
        monkeypatch.setenv("CFG_T_B", value)
        assert cfg.env_flag("CFG_T_B") is False, f"value={value}"


def test_env_flag_unknown_value_uses_default(monkeypatch):
    monkeypatch.setenv("CFG_T_B", "maybe")
    assert cfg.env_flag("CFG_T_B") is True            # 默认开启
    assert cfg.env_flag("CFG_T_B", default=False) is False  # 默认关闭


def test_env_flag_missing_uses_default(monkeypatch):
    monkeypatch.delenv("CFG_T_B", raising=False)
    assert cfg.env_flag("CFG_T_B") is True
    assert cfg.env_flag("CFG_T_B", default=False) is False
