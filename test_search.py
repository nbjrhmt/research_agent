"""
test_search.py —— 博查(Bocha)联网搜索冒烟测试

用法(在项目根目录执行, 会自动读取 .env 里的 BOCHA_API_KEY):
    python test_search.py [可选: 搜索词, 默认示例词]

原理: 与主流程复用同一个 tools/search_tool.bocha_web_search, 不在此处重复实现/硬编码 Key。
"""
import sys

from tools.search_tool import bocha_web_search

if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "2026国内开源大模型有哪些"
    print(f">>> 搜索词: {query}")
    print(bocha_web_search(query, max_results=5))
