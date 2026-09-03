"""
state_schema.py —— Agent State 数据结构(全部上下文保存在 State 里)

字段说明(与规格文档一一对应):
    user_query:      用户原始调研问题
    sub_tasks:       规划节点拆解出的子调研任务列表
    collected_info:  全部已搜集素材: 网页摘要 / PDF文本 / 上传文件预览 / 代码运行结果
    reflection:      反思输出: 信息是否充足、还缺哪些信息、下一步搜索关键词
    final_report:    最终输出调研报告(Markdown)
    iteration_count: 当前已执行的工具轮次(达到上限 8 即强制进入报告节点)

内部附加字段(供前端实时日志使用):
    uploaded_files:  本次上传到 temp_upload/ 的文件名清单(工具只允许读取这些文件)
    steps_log:       每一步操作日志, 供 Streamlit "实时日志面板" 展示
"""
from operator import add
from typing import Annotated, List, TypedDict


class AgentState(TypedDict, total=False):
    # ---------------- 规格文档定义的核心字段 ----------------
    user_query: str
    sub_tasks: List[str]

    # 素材只增不减: 用 operator.add 做"累加合并", 每次节点返回的新条目自动追加到末尾
    collected_info: Annotated[List[str], add]

    reflection: str
    final_report: str
    iteration_count: int

    # ---------------- 内部附加字段 ----------------
    uploaded_files: List[str]
    steps_log: Annotated[List[str], add]  # 日志同样只追加
