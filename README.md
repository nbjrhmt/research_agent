# 🔎 本地个人调研 Agent(LangGraph + Streamlit)

> 一个把 **LLM 大模型 + 任务规划 + 多轮反思迭代 + 联网搜索 + 工具调用** 串起来的本地调研 Agent 个人实践项目。

## 1. 项目简介

本项目是一个基于 **LangGraph + Streamlit** 的本地 LLM 调研 Agent —— 输入一个调研主题(可选上传 PDF / CSV 素材), Agent 会自动完成"规划子任务 → 联网搜集资料 → 反思是否充足 → 再搜集 / 生成报告"的完整调研闭环, 最终输出一份结构化 Markdown 调研报告, 并支持本地持久化的历史记录管理。

**核心能力:**

| 能力 | 说明 |
| --- | --- |
| 📋 任务规划 | LLM 规划节点把用户需求自动拆解为多个可执行的子调研任务 |
| 🧠 多轮反思迭代 | 反思节点依据已有素材判断"信息是否充足", 不足则回到工具节点继续搜集(上限 10 轮) |
| 🌐 联网搜索 | 工具节点调用博查(Bocha)Web Search API 实时搜索网页信息 |
| 📄 PDF / CSV 上传解析 | PDF 自动提取全文; CSV 自动给出列名 + 行数 + 前 20 行结构预览 |
| 📊 Python 数据分析绘图 | 代码沙盒内允许对上传 CSV 做 pandas 统计分析与 matplotlib 绘图 |
| ⏱ 实时执行日志 | Streamlit 逐节点实时展示子任务、工具调用、素材片段与反思结论 |
| 📝 Markdown 报告导出 | 报告按浏览器侧边栏一键下载为 `.md` 文件 |
| 💾 本地 JSON 持久化历史 | 每次完成的报告自动写入根目录 `report_history.json`, 重启后可恢复(最多保留 20 条) |

> **⚠️ 定位声明: 本项目为单用户本地原型项目, 非线上生产系统。** 适合个人学习、动手实践与求职简历展示, 不建议直接部署到公网对外提供服务(详见"已知项目局限")。

## 2. 环境依赖

- **Python**: 3.10+ 均可, 推荐 3.11 ~ 3.13(项目开发验证环境为 Python 3.13.9)
- **依赖包**: 按项目根目录 `requirements.txt` 安装即可, 核心依赖如下(langchain 全家桶 + Web UI + 数据处理):

```
langgraph          # 图编排: 节点 / 边 / 条件路由
langchain          # LLM 应用框架
langchain-openai   # OpenAI 兼容接口接入(ChatOpenAI)
streamlit          # Web 界面(实时日志 / 历史面板)
requests           # 博查 Web Search API 请求
pypdf              # PDF 文本提取
pandas             # CSV 分析与预览
matplotlib         # 数据分析绘图
chromadb           # 长期记忆预留模块依赖(当前未启用)
python-dotenv      # 读取根目录 .env
```

## 3. 部署 & 运行步骤

```bash
# ① 克隆 / 拉取项目(若已通过其他方式获得项目, 跳过此步)
git clone <你的仓库地址> research_agent
cd research_agent
git pull          # 后续更新时拉取最新代码

# ② 创建虚拟环境并安装依赖
python -m venv .venv
# Windows 激活:
.venv\Scripts\activate
# macOS / Linux 激活:
# source .venv/bin/activate

pip install -r requirements.txt

# ③ 复制 .env 示例并填写真实密钥
# Windows:
copy .env.example .env
# macOS / Linux:
# cp .env.example .env

# ④ 启动应用(默认地址 http://localhost:8501)
streamlit run main.py
```

### `.env` 配置示例(复制 `.env.example` 后填写)

```dotenv
# ── LLM 配置(OpenAI 及一切 OpenAI 兼容服务均可) ──
OPENAI_API_KEY=sk-你的OpenAI兼容API_Key
OPENAI_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# ── 联网搜索配置(博查 Bocha, console.bochaai.com 申请) ──
BOCHA_API_KEY=sk-你的博查API_Key
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | ✅ | LLM 服务商 API Key(OpenAI 兼容格式) |
| `OPENAI_BASE_URL` | ✅ | 接口地址, 官方留空即可; 示例为 DeepSeek 地址 |
| `LLM_MODEL` | ✅ | 使用的模型名, 按服务商提供名称填写 |
| `BOCHA_API_KEY` | ✅ | 博查 Web Search API Key, 用于联网搜索 |

启动后浏览器访问 `http://localhost:8501`, 侧边栏会显示 Key 是否加载成功; 未配置 Key 时页面会给出明确报错, 不会崩溃。

## 4. 功能特性

- **完整的 Agent 主流程**: 规划 → 工具搜集 → 反思评估 → (信息不足则)继续搜集 → 报告生成, 由 LangGraph 状态图编排;
- **安全规则内置**: 工具最多迭代 10 轮(达到上限强制出报告)、LLM 输出 JSON 解析失败自动重试 2 次、反思节点必须依据已有素材判断、报告节点严禁编造素材之外的事实;
- **真实联网搜索**: 统一走博查 Web Search API, 失败时返回"【工具异常】"素材而不是编造内容;
- **上传文件即素材**: PDF / CSV 上传后自动预读, 工具节点还能在代码沙盒中对 CSV 做统计分析、绘图;
- **代码沙盒限制**: 仅允许 `pandas` / `matplotlib` 等只读操作, 禁止删除 / 修改文件, 危险操作会被拦截并如实记录;
- **实时执行日志**: 页面按节点流式展示每一步(子任务 / 工具调用 / 素材 / 反思结论 / 进度条);
- **调研报告展示**: 最终报告 + 生成的图表同页呈现, 附带"资料获取情况"如实说明;
- **历史报告本地持久化**: 完成的报告自动存入侧边栏历史并写入 `report_history.json`; 关闭浏览器 / 重启 streamlit 后历史自动恢复; 支持历史选择、页内预览、一键下载 MD、清空全部历史(内存与 json 同步清空); 最多保留 20 条, 超出自动丢弃最老记录;
- **异常容错**: 依赖缺失 / Key 未配置 / 网络错误 / 历史 json 损坏等都有友好提示或降级处理, 程序不崩溃。

## 5. 已知项目局限(如实说明)

- 🧑‍💻 **单用户本地原型**: 只考虑个人在本地使用, 未做多用户 / 多会话隔离;
- 🔑 **无用户认证**: 任何能访问到页面端口的人都能直接操作, 切勿直接暴露到公网;
- 🔐 **密钥依赖本地 .env**: API Key 以明文存在项目根目录 `.env`, 需自行保证不提交到仓库、不泄露;
- 🐢 **未做高并发**: 未针对多用户同时跑调研做并发 / 排队设计, 同时多人使用可能出现资源竞争;
- 🛡 **代码沙盒仅本地安全**: CSV 分析代码在本机进程内执行, 限制手段以提示词约束 + 目录白名单为主, 不是强隔离沙箱, 只应在可信环境运行;
- 💾 **report_history.json 存放全部历史报告文本**: 历史报告的完整文本会明文保存在该文件中(含可能涉及的个人/敏感信息), 请妥善保管, 必要时手动删除该文件清空历史。

## 6. 项目文件目录说明

```
research_agent/
├── main.py                # Streamlit 入口: 页面布局、上传文件预读、实时日志渲染、
│                          #   历史面板(含 report_history.json 读写持久化)
├── graph_builder.py       # LangGraph 图: State 图构建、四个节点(规划/工具/反思/报告)、
│                          #   条件路由、LLM 构建与 JSON 重试解析(业务核心, 不要改动)
├── state_schema.py        # Agent State 数据结构(TypedDict, 含素材累加 / 日志追加规则)
├── requirements.txt       # Python 依赖清单
├── .env.example           # 环境变量配置示例(复制为 .env 后填写)
├── .env                   # 本地密钥配置(不入库, 含 API Key)
├── report_history.json    # 历史报告持久化文件: 调研完成后自动生成/更新, 重启自动恢复;
│                          #   【🗑️ 清空全部历史】会同步清空该文件; 首次运行前不存在
├── prompts/               # 提示词模板: planner / reflection / report 三个节点的 system prompt
├── tools/                 # 工具模块(每个工具只返回文本素材, 不直接写报告):
│   ├── search_tool.py     #   博查 Web Search 联网搜索封装
│   ├── pdf_reader.py      #   PDF 全文提取
│   └── code_exec_tool.py  #   Python 代码沙盒(pandas/matplotlib, 目录受限)
├── memory/                # ChromaDB 长期记忆模块(预留, 当前未接入主流程)
└── temp_upload/           # 上传文件落盘与图表生成目录(运行时自动创建)
```

**核心文件职责速览**

| 文件 | 职责 |
| --- | --- |
| `main.py` | 网页入口与"胶水层": 收集输入 → 调 LangGraph 主流程 → 实时渲染节点日志 → 结果 / 历史 / 下载; **历史 JSON 持久化逻辑也在此文件** |
| `graph_builder.py` | Agent 业务核心: 规划 / 工具 / 反思 / 报告四个节点 + 反思后条件路由(信息充足 → 报告, 否则循环工具节点, 满 10 轮强制结束) |
| `state_schema.py` | State 类型定义: `user_query / sub_tasks / collected_info / reflection / final_report / iteration_count` 等 |
| `tools/*` | 三类工具: 联网搜索、PDF 读取、代码沙盒 —— 全部"只产出素材文本" |
| `prompts/*` | 各节点提示词模板, 与代码分离, 方便单独调整 |
| `temp_upload/` | 上传的 PDF / CSV 与图表图片的存放目录 |
| `report_history.json` | 历史报告持久化载体(自动生成, 结构: `[{id, topic, finished_at, file_stamp, report}]`, 新→旧排列) |

## 7. 简历提示

本项目为**个人学习原型项目**, 技术栈与实现深度非常适合 **LLM Agent 方向(大模型应用 / AI Agent)校招简历**, 建议在简历 / 项目介绍中突出以下要点:

- 用 **LangGraph 状态图** 实现"规划 → 工具调用 → 反思 → 循环 → 报告"的完整 Agent 编排, 理解 **ReAct / 反思迭代** 这类 Agent 核心范式;
- 解决 LLM 输出的关键工程问题: **结构化输出(JSON)约束与失败重试**、**上下文长度控制(素材裁剪拼接)**、**防幻觉(报告只依据素材、失败如实上报)**;
- 通过 **Tool Use 模式** 接入联网搜索(博查 API)、PDF 解析、**受限 Python 代码沙盒(数据分析 / 绘图)**, 体现"让模型学会用工具"的工程能力;
- 用 **Streamlit 构建可交互演示**, 含逐节点实时日志、图表展示、报告下载与 **本地持久化历史** —— 完整的前后端闭环;
- 代码注释规范、模块划分清晰(入口 / 图构建 / 状态 / 工具 / 提示词分离), 适合在面试中快速讲清系统架构与每一处设计取舍。

> 如实描述为"个人实践原型"即可: 亮点在于亲手把 LLM + Agent 编排 + 工具调用 + Web 界面完整打通, 并做了不少真实工程化细节(重试、限轮、防编造、容错)。
