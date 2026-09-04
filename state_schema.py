"""
state_schema.py —— Agent State 数据结构(全部上下文保存在 State 里)

字段说明(与规格文档一一对应):
    user_query:            用户原始调研问题
    sub_tasks:             规划节点拆解出的子调研任务列表
    collected_info:        全部已搜集素材: 网页摘要 / PDF文本 / 上传文件预览 / 代码运行结果
    reflection:            反思输出(展示用文本): 信息是否充足、还缺哪些信息、下一步搜索关键词
    reflection_sufficient: 反思的结构化判定结果(True=信息充足→进入报告节点;
                           False/缺失=信息不足→继续工具轮次)。由反思节点先于文本
                           截断前解析 LLM 结构化 JSON 得出, 供条件路由直接使用
    final_report:          最终输出调研报告(Markdown)
    iteration_count:       当前已执行的工具轮次(达到上限 MAX_ITERATIONS 即强制进入报告节点)

内部附加字段(供前端实时日志使用):
    uploaded_files:  本次上传到 temp_upload/ 的文件名清单(工具只允许读取这些文件)
    steps_log:       每一步操作日志, 供 Streamlit "实时日志面板" 展示
    reflection_failures: 反思连续结构化判定失败计数(内部字段, P0-3 防空转轮次, 见字段注释)
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
    reflection_sufficient: bool  # 反思结构化判定结果(2026 新增, 可选字段, 兼容旧状态)
    final_report: str
    iteration_count: int

    # ---------------- 内部附加字段 ----------------
    # 反思节点连续结构化判定失败计数(P0-3 加固): 判定成功自动清零; 达到
    # graph_builder.MAX_REFLECT_FAILURES 后, route_after_reflection 强制进入报告节点,
    # 避免 JSON 解析持续失败时空耗工具迭代轮次/LLM API 额度(仅内部使用)
    reflection_failures: int
    uploaded_files: List[str]
    steps_log: Annotated[List[str], add]  # 日志同样只追加
