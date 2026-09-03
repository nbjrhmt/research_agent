"""
tools 工具包: Agent 可用的全部工具(每个工具只做"获取素材", 不做知识生成)
    search_tool.py     bocha_web_search: 联网搜索(博查 Web Search API), 返回网页标题/链接/摘要文本
    pdf_reader.py      read_pdf: 读取上传到 temp_upload/ 的 PDF, 提取文本
    code_exec_tool.py  exec_python_code: 受限执行 pandas/matplotlib 数据分析代码, 返回输出与图表路径
"""
