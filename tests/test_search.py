"""
tests/test_search.py —— 博查(Bocha)联网搜索冒烟测试

⚠️ 注意: 本脚本会【真实调用博查搜索 API, 消耗搜索额度】, 手动运行、不进 CI;
执行前请确认 .env 中 BOCHA_API_KEY 有效且有充足额度(见 README FAQ Q11)。

用法(任意目录均可, 脚本会自动把项目根目录加入 sys.path, 并读取 .env 里的
BOCHA_API_KEY):
    python tests/test_search.py [可选: 搜索词, 默认示例词]

原理: 与主流程复用同一个 tools/search_tool.bocha_web_search,
不在此处重复实现/硬编码 Key。
"""
import os
import sys

# 允许从任意工作目录运行: 把项目根目录(本文件的上两级)加入 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.search_tool import bocha_web_search  # noqa: E402

if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "2026国内开源大模型有哪些"
    print(f">>> 搜索词: {query}")
    print(bocha_web_search(query, max_results=5))
