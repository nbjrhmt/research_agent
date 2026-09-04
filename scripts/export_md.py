"""
scripts/export_md.py —— 把 report_history.json 中最新的报告导出为 .md 文件

用法(任意目录均可运行, 脚本自动定位项目根目录):
    python scripts/export_md.py

说明:
    - 从项目根目录 report_history.json 读取记录, 默认导出最新一条(records[0]);
    - 输出文件写到项目根目录; 文件名默认 AI_Agent开源框架调研报告.md,
      如需其他文件名可修改下方 out_file_name。
"""
import json
import os

# 项目根目录 = 本文件(scripts/ 下)的上一级; 不依赖当前工作目录
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_FILE = os.path.join(_BASE_DIR, "report_history.json")

try:
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)
except FileNotFoundError:
    print(f"❌ 未找到 {HISTORY_FILE}: 请先在网页端完成至少一次调研, 再运行本脚本。")
    raise SystemExit(1)
except json.JSONDecodeError as exc:
    print(f"❌ {HISTORY_FILE} 内容损坏, 无法解析: {exc}")
    raise SystemExit(1)

if not isinstance(records, list) or not records:
    print("❌ report_history.json 中没有历史报告记录。")
    raise SystemExit(1)

# 如果有多条记录，records[0]是第1条(最新)，records[1]第2条，以此类推
markdown_text = records[0]["report"]

# 导出的md文件名
out_file_name = "AI_Agent开源框架调研报告.md"
out_path = os.path.join(_BASE_DIR, out_file_name)

with open(out_path, "w", encoding="utf-8") as out_f:
    out_f.write(markdown_text)

print(f"✅导出成功！生成文件：{out_path}")
