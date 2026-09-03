import json

# 和 report_history.json 在同一个文件夹
with open("report_history.json", "r", encoding="utf-8") as f:
    records = json.load(f)

# 如果有多条记录，records[0]是第1条，records[1]第2条，以此类推
markdown_text = records[0]["report"]

# 导出的md文件名
out_file_name = "AI_Agent开源框架调研报告.md"

with open(out_file_name, "w", encoding="utf-8") as out_f:
    out_f.write(markdown_text)

print(f"✅导出成功！生成文件：{out_file_name}")
