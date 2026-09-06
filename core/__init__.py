"""
core 核心层 —— 与 UI 无关的项目级业务逻辑(引擎/服务代码)

设计目标(2026 工程重构 P1, 详见 README「项目架构设计」):
    - main.py(Streamlit)只保留页面渲染与事件驱动代码;
    - 历史持久化 / 临时文件生命周期 / 上传素材预读 / 环境变量配置解析等纯业务逻辑
      迁入本包, 保证可在无 UI、无浏览器环境下独立 import、独立单元测试, 也可被
      CLI / API / 其他前端复用;
    - 本包内模块禁止 import streamlit, 禁止依赖项目根目录的平铺脚本模块
      (保证 pip install 后本包可独立使用)。

模块索引:
    config.py        统一环境变量配置解析(env_str / env_int / env_float / env_flag,
                    全库唯一出处, 消除旧版 graph_builder.py 与 tools/search_tool.py
                    各自重复实现的 _env_int/_env_str/_env_flag)
    history_store.py 调研报告历史持久化(report_history.json 读写容错 + 记录构造 +
                    下载文件名辅助, 原 main.py「历史报告存储」逻辑迁出)
    file_store.py    临时文件生命周期(上传落盘 / 启动与任务间清理 / 删除 / partial
                    素材快照 / 运行期新图表发现, 原 main.py 同名函数迁出)
    ingest.py        上传文件预读为素材(PDF 全文 / CSV 结构预览, 原 main.py
                    _preview_csv/_ingest_upload 迁出)
"""
