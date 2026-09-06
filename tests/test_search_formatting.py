"""
tests/test_search_formatting.py —— tools/search_tool.py 联网搜索封装的离线分支单测
(2026 工程重构 P2 补齐: 旧版只有真联网冒烟 tests/test_search.py, 不进 CI)

通过 monkeypatch 环境变量与 requests.post 假响应, 覆盖 bocha_web_search 的:
    空关键词 / 未配置 Key / 网络异常 / HTTP 错误码 / 业务错误码 / 返回结构异常 /
    无结果 / count 钳制与非法值回退 / summary·freshness 参数透传 /
    结果格式化(标题/链接/摘要、snippet 回退、>800 字符截断、脏条目过滤)。
不产生任何真实网络请求与 API 额度消耗。
"""
import types

import requests

from tools import search_tool as st


def _patch_env_key(monkeypatch, key: str = "test-key-123"):
    monkeypatch.setenv("BOCHA_API_KEY", key)


def _fake_post(captured: list):
    def fake(url, json=None, headers=None, timeout=None):
        captured.append((url, dict(json or {}), headers, timeout))
        body = {
            "code": 200,
            "data": {
                "webPages": {"value": [
                    {"name": "标题A", "url": "https://a.test", "summary": "摘要A"},
                    {"name": "标题B", "url": "https://b.test", "snippet": "回退摘要B"},
                    {"foo": "既无 name 也无 url 的脏条目"},
                ]},
            },
        }
        return types.SimpleNamespace(status_code=200, json=lambda: body)

    return fake


# ---------------- 前置校验: 不联网即可判定 ----------------
def test_empty_query_returns_tool_error():
    result = st.bocha_web_search("   ")
    assert result.startswith("【工具异常】")
    assert "关键词为空" in result


def test_missing_key_returns_tool_error(monkeypatch):
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    result = st.bocha_web_search("新能源汽车")
    assert result.startswith("【工具异常】")
    assert "未配置 BOCHA_API_KEY" in result


def test_placeholder_key_returns_tool_error(monkeypatch):
    monkeypatch.setenv("BOCHA_API_KEY", "你的BOCHA_API_Key")  # 模板占位 = 未配置
    result = st.bocha_web_search("新能源汽车")
    assert "未配置 BOCHA_API_KEY" in result


# ---------------- 请求层异常 ----------------
def test_network_error_returns_tool_error(monkeypatch):
    _patch_env_key(monkeypatch)

    def boom(url, json=None, headers=None, timeout=None):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "post", boom)
    result = st.bocha_web_search("新能源汽车")
    assert result.startswith("【工具异常】")
    assert "请求博查接口失败" in result


def test_http_error_status_returns_tool_error(monkeypatch):
    _patch_env_key(monkeypatch)
    captured = []

    def fake(url, json=None, headers=None, timeout=None):
        captured.append(1)
        body = {"msg": "invalid api key"}
        return types.SimpleNamespace(status_code=401, json=lambda: body)

    monkeypatch.setattr(requests, "post", fake)
    result = st.bocha_web_search("q")
    assert "HTTP 401" in result and "invalid api key" in result
    assert len(captured) == 1


def test_business_error_code_returns_tool_error(monkeypatch):
    _patch_env_key(monkeypatch)

    def fake(url, json=None, headers=None, timeout=None):
        body = {"code": 40003, "msg": "额度用尽"}
        return types.SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(requests, "post", fake)
    result = st.bocha_web_search("q")
    assert "业务错误码 40003" in result and "额度用尽" in result


def test_unexpected_body_structure_returns_tool_error(monkeypatch):
    _patch_env_key(monkeypatch)

    def fake(url, json=None, headers=None, timeout=None):
        return types.SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": {}})

    monkeypatch.setattr(requests, "post", fake)
    result = st.bocha_web_search("q")
    assert "结构异常" in result


def test_no_results_returns_hint(monkeypatch):
    _patch_env_key(monkeypatch)

    def fake(url, json=None, headers=None, timeout=None):
        # 无结果时博查返回空 value 列表(webPages 为空则回退读 data.value)
        body = {"code": 200, "data": {"value": []}}
        return types.SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(requests, "post", fake)
    result = st.bocha_web_search("冷门词XYZ")
    assert "没有搜索到与「冷门词XYZ」相关" in result


# ---------------- 成功路径: 参数与格式化 ----------------
def test_success_formats_results_and_payload(monkeypatch):
    _patch_env_key(monkeypatch)
    monkeypatch.setenv("BOCHA_BASE_URL", "https://example.test/v1/web-search")
    monkeypatch.setenv("BOCHA_COUNT", "5")
    monkeypatch.setenv("BOCHA_FRESHNESS", "oneDay")
    captured = []
    monkeypatch.setattr(requests, "post", _fake_post(captured))

    result = st.bocha_web_search("新能源汽车 2026", summary=True, freshness="oneDay")

    assert result.startswith("搜索来源: 博查(Bocha Web Search API)")
    assert "共获取 2 条结果:" in result          # 脏条目(无 title/url)被过滤
    assert "[1] 标题: 标题A" in result and "https://a.test" in result and "摘要A" in result
    assert "[2] 标题: 标题B" in result and "回退摘要B" in result  # 无 summary 回退 snippet
    url, payload, headers, timeout = captured[0]
    assert url == "https://example.test/v1/web-search"
    assert payload["query"] == "新能源汽车 2026"
    assert payload["count"] == 5 and payload["summary"] is True
    assert payload["freshness"] == "oneday"      # 大小写归一化
    assert headers["Authorization"] == "Bearer test-key-123"
    assert timeout is not None


def test_success_count_clamped_to_10(monkeypatch):
    _patch_env_key(monkeypatch)
    captured = []
    monkeypatch.setattr(requests, "post", _fake_post(captured))
    st.bocha_web_search("q", max_results=999)    # 超上限被钳制
    assert captured[0][1]["count"] == 10


def test_invalid_count_env_falls_back_to_5(monkeypatch):
    _patch_env_key(monkeypatch)
    monkeypatch.setenv("BOCHA_COUNT", "abc")     # 非法值 → 回退 5
    captured = []
    monkeypatch.setattr(requests, "post", _fake_post(captured))
    st.bocha_web_search("q")
    assert captured[0][1]["count"] == 5


def test_invalid_freshness_omitted_and_summary_false(monkeypatch):
    _patch_env_key(monkeypatch)
    captured = []
    monkeypatch.setattr(requests, "post", _fake_post(captured))
    st.bocha_web_search("q", summary=False, freshness="yesterday")  # 非法时效 → 不带该字段
    payload = captured[0][1]
    assert payload["summary"] is False
    assert "freshness" not in payload


def test_long_body_truncated_at_800_chars(monkeypatch):
    _patch_env_key(monkeypatch)

    def fake(url, json=None, headers=None, timeout=None):
        body = {"code": 200, "data": {"webPages": {"value": [
            {"name": "长文", "url": "https://long.test", "summary": "S" * 1200},
        ]}}}
        return types.SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(requests, "post", fake)
    result = st.bocha_web_search("q")
    assert "…" in result
    body_line = next(ln for ln in result.splitlines() if ln.startswith("    摘要:"))
    assert len(body_line) <= 800 + len("    摘要: ") + 1
