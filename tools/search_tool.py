"""
search_tool.py —— 联网搜索封装(博查 Bocha Web Search API)

说明: 本项目联网搜索统一走博查(Bocha)Web Search API:
    POST {BOCHA_BASE_URL}  (请求头 Authorization: Bearer <BOCHA_API_KEY>)
博查是国内可直连的实时网页搜索 API(OpenAI 兼容格式的搜索服务), 取代早期
ddgs(DuckDuckGo)+ 必应 RSS/HTML 抓取方案——抓取方案经常被反爬、限流或网络
拦截, 是"之前的联网搜索容易出问题"的主要来源, 现已全部移除。

配置(写在项目根目录 .env):
    BOCHA_API_KEY    必填: 博查 API Key(在 console.bochaai.com 申请)
    BOCHA_BASE_URL   选填: 接口地址, 默认 https://api.bocha.cn/v1/web-search
    BOCHA_COUNT      选填: 单次返回条数 1-10, 默认 5
    BOCHA_FRESHNESS  选填: 时效性过滤, 取值 oneDay/oneWeek/oneMonth/oneYear/noLimit,
                           默认 noLimit
    SEARCH_TIMEOUT   选填: 单次请求超时秒数, 默认 15

对外函数:
    bocha_web_search(query, max_results=..., timeout=..., summary=True,
                     freshness=None) -> str
    无论成功失败都只返回字符串: 成功返回格式化网页摘要文本; 失败返回以
    【工具异常】开头的说明文字(由上层作为一条素材记录, 不中断调研主流程)。
"""
import os
import time

_BOCHA_DEFAULT_URL = "https://api.bocha.cn/v1/web-search"

# 尽量自动加载项目根目录 .env(不覆盖已存在的环境变量)。
# 主程序 main.py / graph_builder.py 也会加载, 这里保证本模块被独立引用时(如 test_search.py)配置依然生效。
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:  # 没有 python-dotenv 时静默跳过, 交给调用方负责配置
    pass
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_FRESHNESS_OPTIONS = ("oneday", "oneweek", "onemonth", "oneyear", "nolimit")


def _env_int(name: str, default: int) -> int:
    """读取环境变量中的整数, 填错时退回默认值"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    """读取环境变量中的字符串, 空值退回默认值"""
    return (os.getenv(name, "") or default).strip()


# ============================ 通用: 格式化结果 ============================
def _format_results(items: list) -> str:
    """把 [{title,href,body}...] 格式化成文本素材(与历史素材格式保持一致)"""
    lines = [f"共获取 {len(items)} 条结果:"]
    for i, item in enumerate(items, start=1):
        title = str(item.get("title") or "(无标题)")
        href = str(item.get("href") or item.get("url") or "(无链接)")
        body = str(item.get("body") or "").strip() or "(无摘要)"
        if len(body) > 800:
            body = body[:800] + "…"
        lines.append(f"[{i}] 标题: {title}")
        lines.append(f"    链接: {href}")
        lines.append(f"    摘要: {body}")
    return "\n".join(lines)


# ============================ 博查(Bocha)搜索 ============================
def bocha_web_search(query: str, max_results: int | None = None,
                     timeout: int | None = None, summary: bool = True,
                     freshness: str | None = None) -> str:
    """
    执行联网网页搜索(博查 Web Search API)。

    :param query:       搜索关键词(中英文均可)
    :param max_results: 最多返回几条结果(1-10, 博查单次上限 10);
                        默认取 .env 的 BOCHA_COUNT, 未配置时为 5
    :param timeout:     单次请求超时秒数, 默认取 .env 的 SEARCH_TIMEOUT 或 15
    :param summary:     是否请求"摘要模式"(每条结果附 AI 生成的简练摘要)
    :param freshness:   时效性过滤: oneDay/oneWeek/oneMonth/oneYear/noLimit;
                        默认取 .env 的 BOCHA_FRESHNESS, 未配置时不过滤
    :return:            格式化后的网页摘要文本; 失败返回以【工具异常】开头的说明文字
    """
    query = (query or "").strip()
    if not query:
        return "【工具异常】bocha_web_search: 搜索关键词为空。"

    api_key = _env_str("BOCHA_API_KEY", "")
    if not api_key or api_key.startswith("你的"):
        return ("【工具异常】bocha_web_search: 未配置 BOCHA_API_KEY。请在项目根目录 .env 中填入"
                "博查 API Key(申请地址 https://console.bochaai.com), 配置后刷新页面重试。")
    base_url = _env_str("BOCHA_BASE_URL", _BOCHA_DEFAULT_URL)
    timeout = timeout if timeout is not None else _env_int("SEARCH_TIMEOUT", 15)

    # 博查单次最多 10 条, count 超出上限会被服务端拒绝, 这里先钳制
    count = max_results if max_results is not None else _env_int("BOCHA_COUNT", 5)
    try:
        count = max(1, min(int(count), 10))
    except (TypeError, ValueError):
        count = 5

    payload = {"query": query, "count": count, "summary": bool(summary)}
    freshness = (freshness or _env_str("BOCHA_FRESHNESS", "noLimit")).strip().lower()
    if freshness in _FRESHNESS_OPTIONS:
        payload["freshness"] = freshness

    try:
        import requests  # 延迟导入: 依赖缺失时给出明确提示而非直接抛异常
    except ImportError:
        return ("【工具异常】bocha_web_search: 缺少 requests 依赖, "
                "请先执行 pip install -r requirements.txt 安装依赖。")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }

    t0 = time.time()
    try:
        resp = requests.post(base_url, json=payload, headers=headers, timeout=timeout)
    except Exception as exc:  # 网络不通 / 超时 / 连接被重置等, noqa: BLE001
        return (f"【工具异常】bocha_web_search: 请求博查接口失败({type(exc).__name__}: {str(exc)[:200]}, "
                f"已耗时约 {time.time() - t0:.0f} 秒)。排查建议: ① 检查本机网络能否访问 "
                f"{_BOCHA_DEFAULT_URL}; ② .env 中 BOCHA_API_KEY 是否正确、额度是否用尽; "
                "③ 稍后重试。")

    # ---- 解析响应体(HTTP 状态码或业务码非 200 都视为失败) ----
    try:
        body = resp.json()
    except ValueError:
        body = {}
    msg = str(body.get("msg") or body.get("message") or body.get("error") or "").strip()
    if resp.status_code != 200:
        return (f"【工具异常】bocha_web_search: HTTP {resp.status_code}{(' - ' + msg) if msg else ''}。"
                "请确认 BOCHA_API_KEY 有效且已开通 Web Search API 权限。")
    code = body.get("code")
    if code is not None and int(code) != 200:
        return (f"【工具异常】bocha_web_search: 接口返回业务错误码 {code}{(' - ' + msg) if msg else ''}。"
                "常见原因: API Key 无效/额度用尽/查询参数不合法。")

    # ---- 读取网页结果列表(兼容字段缺失/结构变化) ----
    data = body.get("data") or {}
    web_pages = data.get("webPages") or {}
    raw_items = web_pages.get("value") if isinstance(web_pages, dict) else None
    if not raw_items:
        raw_items = data.get("value") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        return (f"【工具异常】bocha_web_search: 接口返回结构异常(未找到 data.webPages.value), "
                f"原始响应: {str(body)[:300]}")

    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        url = str(item.get("url") or item.get("displayUrl") or "").strip()
        # 摘要模式取 summary(无 summary 时回退到 snippet / 正文片段)
        body_text = str(item.get("summary") or item.get("snippet") or item.get("body") or "").strip()
        if title or url:
            items.append({"title": title, "href": url, "body": body_text})
        if len(items) >= count:
            break

    if not items:
        return ("搜索来源: 博查(Bocha)\n"
                f"没有搜索到与「{query}」相关的网页结果, 建议更换关键词或换一种表述后再试。")
    return "搜索来源: 博查(Bocha Web Search API)\n" + _format_results(items)
