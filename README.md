
# 🔎 本地个人调研 Agent(LangGraph + Streamlit)

> 一个把 **LLM 大模型 + 任务规划 + 多轮反思迭代 + 联网搜索 + 工具调用** 串起来的本地调研 Agent 个人实践项目。

> 🚨 **安全与部署边界声明 —— 先读这里, 不要跳过**
>
> 本项目自带的 **代码沙盒不是生产级安全沙箱**:
> - 静态黑名单**只是简单的文本字面拦截**, 可被字符串拼接、`exec`/`eval`、动态 `import`、变量别名等手段绕过;
> - 缺少 **CPU / 内存 / fork 炸弹等资源限制**, 恶意或写坏的代码仍可能拖垮运行机器;
> - 所有用户代码运行在**同一个 UID/账户**下, 仅靠目录隔离(temp_upload/)受限执行, 不是进程级强隔离;
> - 因此:**禁止公网 / 多租户对外部署**, 本项目仅可用于 **内部演示、本地个人使用**;
> - 页面上传、历史 JSON、日志等也均未做多用户隔离与鉴权, 请勿暴露在不受信任的网络中。
>
> 全部限制细节见 [§8 已知项目局限](#8-已知项目局限如实说明)。

## 1. 项目简介

本项目是一个基于 **LangGraph + Streamlit** 的本地 LLM 调研 Agent —— 输入一个调研主题(可选上传 PDF / CSV 素材), Agent 会自动完成"规划子任务 → 联网搜集资料 → 反思是否充足 → 再搜集 / 生成报告"的完整调研闭环, 最终输出一份结构化 Markdown 调研报告, 并支持本地持久化的历史记录管理。

**核心能力:**

| 能力 | 说明 |
| --- | --- |
| 📋 任务规划 | LLM 规划节点把用户需求自动拆解为多个可执行的子调研任务 |
| 🧠 多轮反思迭代 | 反思节点以**结构化 JSON 判定**(sufficient / missing_topics)判断"信息是否充足", 不足则回到工具节点继续搜集(上限 `MAX_ITERATIONS` = 10 轮) |
| 🌐 联网搜索 | 工具节点调用博查(Bocha)Web Search API 实时搜索网页信息 |
| 📄 PDF / CSV 上传解析 | PDF 自动提取全文; CSV 自动给出列名 + 行数 + 前 20 行结构预览 |
| 📊 Python 数据分析绘图 | 受限子进程沙盒内允许对上传 CSV 做 pandas 统计分析与 matplotlib 绘图(真实路径白名单 + 超时强杀) |
| ⏱ 实时执行日志 | Streamlit 逐节点实时展示子任务、工具调用、素材片段与反思结论 |
| 📝 Markdown 报告导出 | 报告按浏览器侧边栏一键下载为 `.md` 文件; 每条结论强制标注素材编号与来源 URL |
| 💾 本地 JSON 持久化历史 | 每次完成的报告自动写入根目录 `report_history.json`, 重启后可恢复(最多保留 20 条) |
| ⛑ 异常兜底 | LLM 调用统一超时 + 重试退避; LangGraph checkpointer 在任务异常时自动保留已搜集素材 |

> **⚠️ 定位声明: 本项目为单用户本地原型项目, 非线上生产系统。** 适合个人学习、动手实践与求职简历展示, 不建议直接部署到公网对外提供服务(详见"已知项目局限"与顶部「安全与部署边界声明」)。

## 2. 项目架构设计

> 设计目标: 用一张 **LangGraph 状态机** 把"AI Agent 自主迭代(规划 → 搜集 → 反思 → 收敛)"做成
> 确定、可控、可观测、可容错的工程系统。架构围绕五个层次组织, 层间通过 **AgentState** 状态通道
> 与"素材文本流"解耦——每个工具/节点只产出或消费文本素材, 不互相直接调用。

### 2.1 分层架构总览

```
┌──────────────────────────────────────────────────────────────────────────┐
│ L0 展示层        main.py(Streamlit)                                        │
│                  输入主题/上传 PDF·CSV → 逐节点实时日志 → 报告/图表/历史/下载     │
├──────────────────────────────────────────────────────────────────────────┤
│ L1 调度编排层    LangGraph StateGraph(graph_builder.py · state_schema.py)   │
│                  planner → tool ⇄ reflection → report                      │
│                  route_after_reflection 确定性路由: 轮次上限→失败上限→充足判定   │
│                  (不依赖 LLM 字符串, 只消费结构化 reflection_sufficient)       │
├──────────────────────────────────────────────────────────────────────────┤
│ L2 素材加工层    素材全生命周期: 上传预读 → token 预算(判断信封/裁剪) → 去重 →      │
│                  prompt 装配(编号素材流)→ 报告强制【素材N】+URL 溯源             │
├──────────────────────────────────────────────────────────────────────────┤
│ L3 工具能力层    tools/: bocha_web_search(联网) · read_pdf ·                 │
│                  exec_python_code(受限代码执行)——全部只返回"素材文本"           │
├──────────────────────────────────────────────────────────────────────────┤
│ L4 安全隔离层    双层沙盒(code_exec_tool.py + _sandbox_runner.py)             │
│                  静态黑名单→静态预检→子进程受限命名空间→运行时真实路径白名单→超时强杀 │
├──────────────────────────────────────────────────────────────────────────┤
│ L5 LLM 通信层    build_llm / _invoke_llm / _llm_json_ask                   │
│                  超时→指数退避重试→JSON 解析内部重试→结构化结果(全节点统一入口)     │
├──────────────────────────────────────────────────────────────────────────┤
│ L6 工程底座      logging_setup(日志) · report_history.json(历史)              │
│                  temp_upload/partial_*.json(异常快照) · prompts/*(提示词)      │
│                  tests + CI + ruff + requirements-lock(质量门禁)             │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 五个核心层次的设计说明(简历可直接引用)

| 层次 | 职责与关键实现 | 体现的设计思想 |
| --- | --- | --- |
| 🛡 **沙盒安全层** | `code_exec_tool` 静态黑名单 + 静态预检 → 子进程执行(`_sandbox_runner` 受限内置函数白名单、pandas/matplotlib **运行时真实路径白名单**、30s 超时强杀、输出截断) | 纵深防御: 文本层→运行时层→进程层三道防线; 越权读 `.env`、拼接字符串绕过、死循环残留均有对应拦截(局限如实标注, 见 §8) |
| 🔌 **LLM 调用封装层** | `build_llm`(超时/max_tokens/客户端重试)→ `_invoke_llm`(指数退避 1s/2s/4s…8s)→ `_llm_json_ask`(JSON 解析失败附错误提示内部重试)→ 反思节点专用重试 + 连续失败计数 | 全节点统一容错入口: 传输错误与输出不规范的失败分层处理; 先内部重试、后降级、防空转(P0-3) |
| 🔁 **反思迭代调度层** | LangGraph 状态图: 反思节点【先判断(全量素材、零截断判断信封)、后截断展示文本】(P0-1); `reflection_failures`/`MAX_ITERATIONS` 双上限经 `route_after_reflection` 强制收敛; 工具轮次单轮单工具 | 自主迭代可终止、可解释: 判断依据结构化 JSON, 路由不依赖自由文本; 素材只增不减(Annotated add 通道) |
| 🧪 **素材校验去重层** | token 预算: `_truncate_to_tokens`(tiktoken 真实计数 + 二分逼近)→ `_join_material`(总量预算 + 省略标注)→ `_log_prompt_budget`(≥90% 余量告警); 素材归一化去重; PDF 只读白名单 | 上下文防溢出 + 防重复占窗 + 判断防误截断; "素材 → 结论"全程可溯源(报告强制素材编号+URL) |
| 🗄 **日志/持久化工程层** | `logging_setup`(单进程 RotatingFileHandler)→ `report_history.json`(报告历史)→ `_save_partial_run`(异常素材快照)→ `_cleanup_temp_files`(临时文件生命周期) | 长任务可恢复、可审计: 全异常路径素材落盘兜底 + UI 提示下载(P0-4); 生命周期边界与竞争条件显式标注 |

### 2.3 核心数据流

**正常任务数据流(一次完整调研)**

```
用户输入(user_query) ─→ L1 规划节点: LLM 拆解 N 个子任务(≤10)
                        ─→ L3 工具节点(每轮仅 1 次工具调用):
                            搜索/读 PDF/代码执行 → 素材条目
                        ─→ L2 素材加工: token 预算裁剪(判断信封) + 去重
                        ─→ L1 反思节点: 结构化判定 {sufficient, reason, missing_topics}
                             ├─ 充足 → 报告节点: 基于素材生成带编号溯源报告 → END
                             └─ 不足 → 回工具节点(携带 missing_topics 作为下一轮搜索指引)
                        任何时刻 iteration_count ≥ 10 / reflection_failures ≥ 2 → 强制进入报告节点
```

- **状态通道**: `AgentState`(TypedDict)承载全部上下文; `collected_info`(素材)与 `steps_log`(日志)
  使用 `operator.add` 累加通道——节点返回增量, LangGraph 自动追加, 素材在迭代中"只增不减",
  异常时可通过 checkpointer 快照取回完整素材;
- **素材文本流**: 工具层产出 → 加工层"预算化、编号化" → 模型层消费 → 报告层引用(【素材N】+URL),
  同一份素材贯穿判断、生成、溯源全链路, 杜绝节点间私有状态;
- **异常数据流**: 任一步抛错 → ① 取 checkpointer 快照(已跑过节点时)→ ② 或本地增量追踪素材
  (含上传预读)→ 合并写入 `temp_upload/partial_*.json` → 页面提示 + 一键下载, 不静默失败。

### 2.4 模块职责拆分(按层索引, 函数级)

| 层 | 模块/关键函数 | 职责(面试讲解口径) |
| --- | --- | --- |
| L5 | `graph_builder.build_llm` | 依据 `.env` 构建统一 ChatOpenAI: request_timeout / max_tokens / max_retries, 一处配置全图生效 |
| L5 | `_invoke_llm` | 统一调用入口: 瞬时错误(超时/429/5xx)指数退避重试, 全失败抛错由上层兜底 |
| L5 | `_llm_json_ask` / `_extract_json` | 强制结构化输出: 容忍代码围栏/解释文字提取 JSON; 解析失败附错误提示重试 |
| L1 | `make_planner_node / tool_node / reflection_node / report_node` | 四个节点工厂, 各节点只做一件事, 通过返回 dict 增量更新 State |
| L1 | `route_after_reflection` | 纯函数条件路由(轮次上限 → 连续失败上限 → 充足判定), 无副作用, 单测友好 |
| L2 | `_truncate_to_tokens / _join_material / _corpus_token_total / _reflection_judgment_envelope` | token 真实计数、二分裁剪、总量预算、判断信封扩容、余量告警——"先判断后截断"的工程载体 |
| L2 | `_is_duplicate_material` / `_normalize` | 素材归一化去重(空白折叠后全等判定, P2 计划语义级去重) |
| L4 | `exec_python_code` / `_check_blocked` / `_sandbox_runner.main` | 沙盒入口: 预检 → 写临时代码 → 子进程受限执行 → 读输出/收集图表 → 清理 |
| L3 | `bocha_web_search` / `read_pdf` / `_ingest_upload` | 三种素材源, 失败均返回【工具异常】文本素材而非抛断流程 |
| L6 | `_save_partial_run` / `_salvage_run_materials` | 异常快照落盘 + 双源素材取回(快照优先, 本地追踪兜底) |
| L6 | `logging_setup` / `_load_report_history_*` / `_cleanup_temp_files` | 日志/历史/临时文件三套生命周期管理 |

## 3. 项目核心难点与技术突破

> 以下每一条都是**真实实现且经过测试验证**(对应 [CHANGELOG.md](CHANGELOG.md) 与 §9 测试用例),
> 面试可任选展开: 按「遇到了什么问题 → 我怎么做 → 效果如何」的结构讲。安全/部署边界均如实
> 标注, 不夸大防护等级(见顶部安全声明与 §8)。

1. **自研"双层沙盒"防护体系, 解决代码执行绕过与资源失控风险**
   Agent 会让模型编写并执行数据分析代码, 若直接 `exec` 在服务进程内, 一段 `read_csv(DATA_DIR+'/../.env')`
   就能读走密钥。方案: 第一层**静态黑名单 + 静态预检**(拦截 import/open/eval/路径穿越字样),
   第二层**子进程真实隔离**(`_sandbox_runner` 只注入 pd/plt/受限内置函数, 对 pandas 读写与
   matplotlib 保存做**运行时真实路径白名单**, 只允许 `temp_upload/` 内路径), 叠加 **30s 超时强杀**
   防止死循环残留。收益: 字符串拼接、`chr()` 拼路径等绕过手法在运行时层被二次拦截(有专门测试
   `test_runtime_path_guard_blocks_env_read`); 同时把沙盒定为"原型级"并写明局限(无资源配额等),
   不把演示边界包装成生产安全。

2. **LLM 结构化输出容错重试机制, 解决模型输出不规范与"空跑耗损"问题**
   LLM 输出 JSON 常带解释文字/代码围栏甚至纯文本, 且接口有超时/限流抖动。方案: 统一三层容错
   链路——`_invoke_llm`(超时 + 指数退避重试)→ `_extract_json`(容忍围栏截取首个 JSON 对象)→
   `_llm_json_ask`(解析失败时**附上具体解析错误再次请求**, 纠正模型输出); 反思节点再叠加
   `REFLECT_JSON_RETRY` 内部重试与 `reflection_failures` 连续失败计数, 达到上限**强制进入报告节点**。
   收益: 单次解析失败不再直接判"信息不足"导致后续 10 轮空转烧 API 额度; 全部失败也有明确日志与
   用户提示(离线用例验证重试次数与降级路径)。

3. **Token 预算精准管控 + 反思执行时序重构, 解决素材截断误判**
   上下文有限, 素材必须先裁剪; 但"先截断再判断"会让反思只看到素材前段, 误判"信息不足"并空转搜集。
   方案: ① 用 **tiktoken 真实计数 + 二分逼近**做逐条裁剪, `_join_material` 按总量预算装配并**注明省略
   条数**(不静默丢内容); ② 反思节点改为"**先判断、后截断**": 判断前先做素材全量无损盘点,
   按"判断信封"动态扩容(素材全量放得进模型上下文硬预算就一字不截), 判定完成才截断展示文本;
   ③ `_log_prompt_budget` 在用量 ≥90% 安全预算时输出余量告警。收益: 素材尾部信息不再被提前丢弃,
   反思判断失真率下降、轮次空转减少(时序由 `test_reflection_judges_on_full_materials_beyond_soft_budget`
   等测试锁定)。

4. **全链路异常快照落盘机制, 解决 AI 长任务中断的数据丢失**
   多轮调研可能已耗数十次 API 调用, 中途任何一步抛错(LLM 全失败/搜索报错/沙盒致命错误/运行异常)
   都不该"白跑"。方案: LangGraph checkpointer(InMemorySaver)持续记录图状态, 叠加 main.py 本地
   增量追踪素材做**双源兜底**(快照优先、本地追踪保底, 覆盖"建 LLM 阶段就失败"的极端情况), 统一
   落盘 `temp_upload/partial_*.json`(含错误原因), UI 明示素材去向并提供下载; 无素材可保留、兜底
   自身失败也分别给出明确提示。收益: **全异常路径无静默失败**, 用户始终知道已搜集素材去哪了。

5. **确定性迭代调度与防幻觉约束, 收敛 Agent 的"自由"**
   多轮反思若没有硬边界, LLM 可能无限自我怀疑或编造。方案: 状态机内建四类收敛闸门——
   ① 工具轮次上限 `MAX_ITERATIONS=10`; ② 反思连续失败上限(防空转); ③ 条件路由只读结构化字段
   `reflection_sufficient`(不解析自由文本); ④ 素材去重但**去重轮次照计**(防止同一内容反复刷轮)。
   防幻觉侧: 反思判定只准依据已有素材、报告强制每条结论标注【素材N】+来源 URL、工具失败以
   【工具异常】素材如实进入报告"资料获取情况"章节。收益: 迭代一定终止、结论可逐条溯源。

6. **工程自动化: 双 CI + 全量离线单测体系, 质量门禁前置**
   e2e 会真实消耗 LLM/搜索 API, 不适合 CI 频繁执行。方案: 建立 **46 条全离线 pytest 用例**
   (注入假 LLM/假搜索, 零 API 消耗), 用 `pytest.ini` 只收集 `test_*.py` 把耗额度测试隔离在 CI 外;
   GitHub Actions 与 GitLab CI 双平台等价流水线(离线单测 → ruff lint → 锁文件一致性校验,
   由 `scripts/check_lock_consistency.py` 无网络校验 requirements.txt 与 lock 文件约束);
   `.env`/常量/提示词模板分层配置, `_e2e_test.py` 保留为发布前人工冒烟。收益: 本地 `pytest tests`
   12 秒全绿即可放心提交, 回归成本趋近于零。

7. **以"状态通道 + 文本流"为核心的模块化设计, 工程可维护、可讲解**
   全部上下文收敛进 `AgentState`(TypedDict, 素材/日志用 `operator.add` 累加通道), 节点工厂只做
   "输入 State → 输出增量 dict"; 提示词抽离 `prompts/*.txt` 与代码解耦; LLM 行为参数全部可经
   `.env` 调优; 异常边界、单进程假设、P2 演进路线(沙盒资源配额/持久化 saver/语义去重)全部以
   `★ 工程边界备注` 注释显式落档。收益: 每个函数可独立单测、可在面试中按"层"讲清职责,
   代码质量达到可开源、可复用的标准。

## 4. 环境依赖

- **Python**: 本项目要求 **Python >= 3.10**(代码使用 `int | None` 等 3.10+ 语法), 推荐 3.11 ~ 3.13(项目开发验证环境为 Python 3.13.9)
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
tiktoken           # 素材按真实 token 预算裁剪(替代纯字符截断)
```

- **网络前置条件**: 运行时需能联网访问你所选 **LLM 接口**(如 `https://api.deepseek.com` / OpenAI)与**博查搜索接口**(`https://api.bocha.cn`); 两者均为**付费 API 服务**, 需自行在对应平台注册并充值/开通。
- **版本锁定(可选)**: 仓库提供 `requirements-lock.txt`(由 pip-tools 生成的精确版本锁定), 需要可复现环境时可执行 `pip install -r requirements-lock.txt` 安装。

## 5. 部署 & 运行步骤

> ⚠️ 下面的 `pip install` / `python` / `streamlit` 命令默认在**项目根目录**下执行(脚本已兼容从 tests/、scripts/ 子目录直接运行的情况)。

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
# 若需要可复现的精确版本: pip install -r requirements-lock.txt

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

# ── 可选: 沙盒代码执行开关(默认开启; 设为 false 整体关闭代码执行, 见 FAQ Q10) ──
# ALLOW_CODE_EXEC=false
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | ✅ | LLM 服务商 API Key(OpenAI 兼容格式) |
| `OPENAI_BASE_URL` | 可选 | 接口地址; **OpenAI 官方可省略本行**, 其他兼容服务商(如 DeepSeek)需填写对应地址 |
| `LLM_MODEL` | ✅ | 使用的模型名, 按服务商提供名称填写(默认 `gpt-4o-mini`) |
| `BOCHA_API_KEY` | 可选* | 博查 Web Search API Key。*不配置时应用可正常运行, 但**联网搜索不可用**, 只能基于上传素材调研(启动时会给出提示) |

**可选调优变量(全部有默认值, 不填即可; 详见 `.env.example` 注释):**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BOCHA_BASE_URL` | `https://api.bocha.cn/v1/web-search` | 博查接口地址 |
| `BOCHA_COUNT` | `5` | 单次搜索返回条数(1-10) |
| `BOCHA_FRESHNESS` | `noLimit` | 时效过滤: oneDay/oneWeek/oneMonth/oneYear/noLimit |
| `SEARCH_TIMEOUT` | `15` | 单次搜索超时秒数 |
| `LLM_TIMEOUT` | `60` | 单次 LLM 请求超时(秒) |
| `LLM_MAX_OUTPUT_TOKENS` | `8192` | 单次 LLM 输出 token 上限 |
| `LLM_CONTEXT_TOKENS` | `60000` | 估算的模型上下文窗口(token), 素材预算与余量告警依据 |
| `LLM_CLIENT_RETRIES` | `2` | SDK 客户端层重试次数 |
| `LLM_MAX_RETRIES` | `3` | 应用层统一重试次数(指数退避) |
| `ALLOW_CODE_EXEC` | `true` | 是否允许沙盒代码执行工具(exec_python_code); 设 `false` 整体关闭, 仅保留搜索/读 PDF(见 FAQ Q10) |

启动后浏览器访问 `http://localhost:8501`, 侧边栏会显示 Key 是否加载成功; 未配置 Key 时页面会给出明确报错, 不会崩溃。

## 6. 运行示例(用例)

**示例 1: 仅文本联网调研**

1. 在"输入调研主题"框填入: `2024 年中国新能源汽车销量 TOP10 品牌及主要技术路线对比`;
2. 点击"🚀 开始调研": 页面将依次展示 规划子任务 → 每轮搜索日志与素材 → 反思结论(充足/继续) → 最终报告;
3. 调研完成后: 结果区展示 Markdown 报告(每条结论标注〔素材N〕与来源 URL), 侧边栏自动存入历史, 可一键下载 `.md`。

**示例 2: 上传 CSV 做数据分析 + 绘图**

1. 上传一份含年份/销量列的 CSV(≤30MB);
2. 输入如 `分析上传的销量数据并按品牌汇总绘图`;
3. Agent 自动预读 CSV 结构 → 通过代码沙盒(pandas/matplotlib)做统计并生成图表 → 报告与图表同页展示。

**示例 3: 上传 PDF 素材(不联网)**

1. 不配置 `BOCHA_API_KEY`(或断网), 上传 PDF 后输入 `总结这份文档的核心观点`;
2. 页面会提示"联网搜索不可用", Agent 仅基于 PDF 提取文本完成报告。

## 7. 功能特性

- **完整的 Agent 主流程**: 规划 → 工具搜集 → 反思评估 → (信息不足则)继续搜集 → 报告生成, 由 LangGraph 状态图编排;
- **安全规则内置**: 工具最多迭代 10 轮(达到上限强制出报告)、LLM 输出 JSON 解析失败自动重试 2 次(反思节点另有内部重试 + 连续失败防空转, 见 CHANGELOG P0-3)、反思节点只依据已有素材做结构化判定且【先判断、后截断】(素材不会在判定前被提前截断, 见 CHANGELOG P0-1)、报告节点严禁编造素材之外的事实且每条结论必须标注素材编号/来源;
- **真实联网搜索**: 统一走博查 Web Search API, 失败时返回"【工具异常】"素材而不是编造内容;
- **上下文 token 预算管理**: 素材拼接按 tiktoken 真实 token 数裁剪(反思"判断信封"随素材全量动态扩容、报告节点设预算), LLM 输出设 max_tokens 上限, 接近预算时自动省略多余素材并记录告警日志;
- **上传文件即素材**: PDF / CSV 上传后自动预读, 工具节点还能在受限子进程沙盒中对 CSV 做统计分析、绘图(可用 `ALLOW_CODE_EXEC=false` 整体关闭);
- **代码沙盒限制(加固版)**: 文本黑名单 + 子进程内**真实路径白名单**(pandas/matplotlib 只允许读写 `temp_upload/`, 越权路径如 `.env`/项目外文件一律拦截) + 超时 30 秒强杀进程(无后台残留线程); ⚠️ 但**不是生产级沙箱**(限制手法可被绕过, 见顶部安全声明);
- **实时执行日志**: 页面按节点流式展示每一步(子任务 / 工具调用 / 素材 / 反思结论 / 进度条);
- **调研报告展示**: 最终报告 + 生成的图表同页呈现, 附带"资料获取情况"如实说明;
- **历史报告本地持久化**: 完成的报告自动存入侧边栏历史并写入 `report_history.json`; 关闭浏览器 / 重启 streamlit 后历史自动恢复; 最多保留 20 条, 超出自动丢弃最老记录;
- **临时文件自动清理**: 上传文件/图表按任务周期自动清理(`temp_upload/` 不无限膨胀);
- **全异常路径素材兜底(不静默失败)**: LLM 重试全部失败 / 沙盒致命错误 / 搜索 API 报错 / 任务运行异常等任一异常, 已搜集素材(含上传预读素材)都会落盘 `temp_upload/partial_*.json` 并在页面给出提示 + 下载按钮; 无素材时也明确提示;
- **离线单测 + CI**: 46 条 pytest 离线用例(不调用 LLM/搜索 API)覆盖沙盒、token 裁剪/预算、反思解析、去重等核心逻辑; GitHub Actions 与 GitLab CI 自动跑测试 / ruff lint / 锁文件一致性(见 §9);
- **统一日志**: 标准 logging 模块输出到 `logs/app.log`(带时间戳/级别/异常堆栈, 自动轮转; 仅限单进程模式, 见 §8)。

## 8. 已知项目局限(如实说明)

- 🧑‍💻 **单用户本地原型**: 只考虑个人在本地使用, 未做多用户 / 多会话隔离;
- 🔑 **无用户认证**: 任何能访问到页面端口的人都能直接操作, 切勿直接暴露到公网;
- 🔐 **密钥依赖本地 .env**: API Key 以明文存在项目根目录 `.env`, 需自行保证不提交到仓库、不泄露;
- 🐢 **未做高并发**: 未针对多用户同时跑调研做并发 / 排队设计, 同时多人使用可能出现资源竞争;
- 🛡 **代码沙盒不是生产级沙箱(重要)**: 详见顶部「安全与部署边界声明」——静态黑名单仅是文本字面拦截(可被字符串拼接 / exec / eval / 动态 import / 变量别名绕过), 缺少 CPU / 内存 / fork 炸弹资源限制, 所有用户代码运行在同一 UID、仅目录隔离; 只应在可信环境运行、**禁止公网多租户部署**, 仅用于内部演示与本地使用;
- 💾 **report_history.json 存放全部历史报告文本**: 历史报告的完整文本会明文保存在该文件中(含可能涉及的个人/敏感信息), 请妥善保管, 必要时手动删除该文件清空历史。

**工程边界备注(单进程假设, 多进程/多实例部署前必读):**

- 📋 **RotatingFileHandler 多进程日志风险**: 日志统一写入 `logs/app.log`(自动轮转)。当前为**单进程 Streamlit 模式**可用; 若切换多进程/多实例部署, 多个进程同时轮转同一文件会出现日志互相覆盖/文件损坏(RotatingFileHandler 轮转非跨进程原子操作), 需改用 concurrent-log-handler 或集中日志采集(见 `logging_setup.py` 注释);
- ♻️ **checkpointer(InMemorySaver)限制**: LangGraph 会话状态仅保存在**单进程内存**, Streamlit 服务重启 / 进程退出后图运行状态即丢失, 仅适合单机演示; 任务中途异常素材通过落盘 `temp_upload/partial_*.json` 兜底(重启后仍可打开)。P2 计划接入文件/Redis 持久化 saver(见 `graph_builder.build_graph` 注释);
- 🧹 **temp_upload/ 清理竞争条件**: 上传文件 / 图表 / partial 成果共用 `temp_upload/` 全局目录, 清理逻辑(listdir 后逐个删除)无跨进程锁 —— 多进程/多实例并发时, 一个实例可能删除另一个实例正在使用的文件; 仅支持单进程模式(见 `main.py._cleanup_temp_files` 注释)。

**P2 后续迭代计划(本轮仅记录、未实现, 详见 [CHANGELOG.md](CHANGELOG.md)「P2」)**: Dockerfile 容器化部署、ruff+black+isort 完整格式化 lint 配置、checkpointer 文件/Redis 持久化 saver、沙盒 CPU/内存/进程数资源限制(fork 炸弹防御)、素材语义去重(embedding 相似度)。

## 9. 项目文件目录说明

```
research_agent/
├── main.py                  # Streamlit 入口: 页面布局、上传文件预读、实时日志渲染、
│                            #   历史面板、临时文件清理、全异常路径素材落盘兜底、统一日志
├── graph_builder.py         # LangGraph 图: State 图构建、四个节点(规划/工具/反思/报告)、
│                            #   结构化反思判定(先判断后截断 + 失败内部重试防空转)、token 预算
├── state_schema.py          # Agent State 数据结构(TypedDict, 含 reflection_sufficient /
│                            #   reflection_failures 等判定与内部字段)
├── logging_setup.py         # 统一日志配置(logs/app.log, 时间戳 + 异常堆栈落盘)
├── requirements.txt         # Python 依赖清单(下限约束)
├── requirements-lock.txt    # pip-tools 生成的精确版本锁定(可复现安装; CI 校验其一致性)
├── CHANGELOG.md             # 版本变更记录(P0/P1/P2 计划与工程备注, 迭代追溯用)
├── pytest.ini               # pytest 配置: 只收集 test_*.py 离线用例(e2e 不进 CI)
├── .github/workflows/ci.yml # GitHub Actions CI: pytest + ruff + 锁文件一致性(与 GitLab CI 等价)
├── .gitlab-ci.yml           # GitLab CI(等价于 GitHub Actions CI, 按平台任选其一)
├── .env.example             # 环境变量配置示例(复制为 .env 后填写; 含全部可选调优变量)
├── .env                     # 本地密钥配置(不入库, 含 API Key)
├── .gitignore               # Git 忽略规则(.venv/.env/.idea/chroma_db/日志/缓存等)
├── prompts/                 # 全部提示词模板(节点 system prompt + 用户模板, 与代码分离):
│   ├── planner_system.txt   #   规划节点 system prompt
│   ├── planner_prompt.txt   #   规划节点用户模板(含 {user_query})
│   ├── tool_system.txt      #   工具调度节点 system prompt(工具说明 + JSON 格式)
│   ├── reflection_system.txt#   反思节点 system prompt(结构化 JSON: sufficient/missing_topics)
│   ├── reflection_prompt.txt#   反思节点用户模板(含 {user_query}/{collected_info})
│   ├── report_system.txt    #   报告节点 system prompt(结论必须标注素材编号+来源 URL)
│   └── report_prompt.txt    #   报告节点用户模板(含 {user_query}/{collected_info})
├── tools/                   # 工具模块(每个工具只返回文本素材, 不直接写报告):
│   ├── search_tool.py       #   博查 Web Search 联网搜索封装
│   ├── pdf_reader.py        #   PDF 全文提取
│   ├── code_exec_tool.py    #   代码沙盒入口(文本黑名单 + 静态预检 + 子进程调度/超时强杀)
│   └── _sandbox_runner.py   #   代码沙盒子进程运行器(受限内置函数 + 真实路径白名单)
├── memory/                  # ChromaDB 长期记忆模块(预留, 当前未接入主流程)
├── tests/                   # 离线单元测试(pytest, 不调用 LLM/搜索 API, CI 自动执行):
│   ├── test_graph_builder.py#   graph_builder 核心单测: token 裁剪/预算告警、反思 JSON 解析
│   │                        #   与时序、素材去重、规划/报告/路由(31 条)
│   ├── test_sandbox.py      #   代码沙盒回归: 黑名单/运行时路径白名单/超时强杀/图表/模板(15 条)
│   │                        #   运行: python -m pytest tests  或  python tests/test_sandbox.py
│   ├── test_search.py       #   ⚠️ 联网冒烟(真实调用博查 API, 手动运行, 不进 CI)
│   └── _e2e_test.py         #   ⚠️ 全链路测试(真实消耗 LLM + 博查 API 额度, 手动运行, 不进 CI!)
│                            #   (python tests/_e2e_test.py; 异常时自动保留已搜集素材)
├── scripts/                 # 辅助脚本:
│   ├── export_md.py         #   把 report_history.json 最新报告导出为 .md
│   └── check_lock_consistency.py  # 锁文件一致性校验(CI 用, 无网络依赖)
├── report_history.json      # 历史报告持久化文件: 调研完成后自动生成/更新(不入库)
├── temp_upload/             # 上传文件/图表/部分成果(partial_*.json)临时目录(运行时自动创建/清理)
├── logs/                    # 运行日志目录(自动创建: logs/app.log, 不入库)
└── chroma_db/               # ChromaDB 向量库目录(启用记忆模块后生成, 不入库)
```

**核心文件职责速览**

| 文件 | 职责 |
| --- | --- |
| `main.py` | 网页入口与"胶水层": 收集输入 → 调 LangGraph 主流程 → 实时渲染节点日志 → 结果 / 历史 / 下载 / 临时文件清理 / **全异常路径素材落盘兜底(P0-4)**; 历史 JSON 持久化也在此文件 |
| `graph_builder.py` | Agent 业务核心: 规划 / 工具 / 反思 / 报告四个节点 + 反思后条件路由; **反思"先判断后截断"时序(P0-1)与 JSON 失败内部重试、连续失败防空转(P0-3)**; LLM 统一超时/重试与 token 预算 |
| `state_schema.py` | State 类型定义: `user_query / sub_tasks / collected_info / reflection / reflection_sufficient / reflection_failures / final_report / iteration_count` 等 |
| `logging_setup.py` | 统一日志(logs/app.log: 时间戳 + 级别 + 异常堆栈, RotatingFileHandler 轮转; 仅单进程模式) |
| `tools/*` | 三类工具: 联网搜索、PDF 读取、代码沙盒 —— 全部"只产出素材文本"; 沙盒为子进程 + 路径白名单 + 超时强杀(非生产级, 见顶部安全声明) |
| `prompts/*` | 全部节点提示词模板(系统 + 用户), 与代码分离, 方便单独调整(升级时保持目录完整, 见 §11) |
| `tests/test_graph_builder.py` `tests/test_sandbox.py` | **离线单元测试(pytest, 不调用 API)**: token 裁剪/预算告警、反思解析与时序、素材去重、沙盒回归等; `python -m pytest tests` |
| `tests/_e2e_test.py` `tests/test_search.py` | ⚠️ **真实消耗 LLM / 博查 API 额度的联网测试, 手动运行、不进 CI**(见 FAQ Q11) |
| `scripts/*` | export_md(历史导出 .md)、check_lock_consistency(CI 锁文件一致性校验) |
| `pytest.ini` `.github/workflows/ci.yml` `.gitlab-ci.yml` `CHANGELOG.md` | 测试配置 / 双平台 CI(测试 + lint + 锁校验)/ 版本变更记录 |
| `report_history.json` | 历史报告持久化载体(自动生成, 结构: `[{id, topic, finished_at, file_stamp, report}]`, 新→旧排列) |

### 代码规范与可读性约定(全库统一, 面试讲解可引用)

- **模块文档**: 每个模块文件头部有 `"""` docstring: 一句话职责 + 设计决策要点; 安全/边界/
  待办以 `★ 工程边界备注`、`P0-x/P1-x/P2` 前缀显式标注(如沙盒局限、单进程假设、P2 路线);
- **函数注释**: 首行"一句话用途" + 必要展开; 参数多/关键路径的函数使用 `:param:`/`:return:`
  标签(见 `build_llm`、`_llm_json_ask`、`exec_python_code`、`_guard_path` 等核心函数);
- **命名与语言**: 标识符/字符串统一英文, 注释中文; 常量集中模块顶部并可被 `.env` 覆盖,
  不在函数内散落魔法数字(轮次/预算/超时等均有具名常量);
- **分层规范**: 节点/工具只通过 State 与"素材文本"交互, 不互相 import 调用; 每个工具
  "只产出素材文本、不直接写报告"; 提示词抽离 `prompts/*.txt`, 与代码解耦;
- **质量门禁**: 全库通过 `ruff check`(E4/E7/E9/F, `noqa` 必须带原因注释); 离线单测不依赖
  网络与密钥; CI 覆盖测试 / lint / 锁一致性三项。

## 10. 常见问题 FAQ / 故障排查

**Q1: 页面提示"未配置 API Key" / "任务终止: 未配置 OPENAI_API_KEY"?**
→ 检查项目根目录是否存在 `.env`(由 `.env.example` 复制而来), 且 `OPENAI_API_KEY` / `LLM_MODEL` 是否填写真实值; 修改后**刷新页面**(env 在页面启动时读取)。

**Q2: 页面提示"❌ 联网搜索: 未配置 BOCHA_API_KEY"?**
→ 联网搜索需要博查 API Key(console.bochaai.com 申请)。不配置也能跑, 但 Agent 只能基于上传素材工作; 需要联网检索时在 `.env` 填入后刷新页面。

**Q3: 任务中途报错终止(网络/限流/服务端错误)?**
→ 已内置统一超时(LLM_TIMEOUT)+ 指数退避重试(LLM_MAX_RETRIES), 偶发抖动会自动重试; 持续失败请检查: 网络能否访问所选接口(含 api.bocha.cn)、Key 额度是否用尽、模型名是否正确(换更常见模型名如 deepseek-chat / gpt-4o-mini)。异常时页面会提供已搜集素材 partial JSON 的**下载按钮**(全异常路径素材兜底, 见 CHANGELOG P0-4); 反思判定 JSON 解析失败也会先在节点内部自动重试再降级, 不会空耗轮次。

**Q4: 报告结论没有来源 / 想追溯某条结论的依据?**
→ 报告提示词强制每条结论标注〔素材N〕编号与来源 URL; 若某条内容无标注, 属于模型未遵守约束, 可重新运行或把该问题反馈到反思轮次。

**Q5: 运行日志在哪看?**
→ 所有模块的警告/错误(含异常堆栈)统一写入 `logs/app.log`(自动轮转, 单文件 ≤1MB 保留 5 份); 页面内的"实时执行日志"展示的是任务步骤而非程序日志。

**Q6: temp_upload / report_history.json 占空间?**
→ 上传文件与图表在每次任务开始/结束时自动清理; 历史报告明文存在 `report_history.json`, 可在侧边栏"清空全部历史"删除(同步清空该文件)。

**Q7: 换一台机器克隆后 `pip install -r requirements.txt` 装不上?**
→ 确认 Python ≥ 3.10; 若最新版本依赖冲突, 改用 `requirements-lock.txt` 精确版本安装(见 Q9)。

**Q8: tiktoken 安装失败 / 提示 tiktoken 不可用?**
→ ① Windows 用户请确认 Python ≥ 3.10 并升级 pip: `python -m pip install --upgrade pip`, 再 `python -m pip install tiktoken`; ② macOS/Linux 若编译失败, 安装 rust 工具链后重试(`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`); ③ 实在装不上也能运行——代码内置降级: 日志会提示 "tiktoken 不可用, token 计数降级为字符估算", 裁剪/预算按字符近似估算, 功能不受阻断(见 `graph_builder._get_encoding`)。

**Q9: requirements-lock.txt(lock 锁文件)怎么用 / 什么时候需要重新生成?**
→ 需要**可复现安装**时使用: `pip install -r requirements-lock.txt`(所有依赖精确锁定版本, 与 CI 一致)。日常开发仍可 `pip install -r requirements.txt`(下限约束)。**每次改动 requirements.txt 后**, 需要重新生成锁文件并一起提交:
```bash
pip install pip-tools
pip-compile --output-file=requirements-lock.txt requirements.txt
```
CI 会自动校验两者一致性(`scripts/check_lock_consistency.py`), 不一致会直接失败。

**Q10: 如何关闭沙盒代码执行功能?**
→ 在项目根目录 `.env` 中加入(或改为): `ALLOW_CODE_EXEC=false`, 然后刷新页面。关闭后:
① 工具提示词会明确告知模型禁止选择 `exec_python_code`; ② 即使模型仍选择该工具, 也会被拦截并返回"已被管理员关闭"的素材说明, **不会执行任何代码**; ③ 侧边栏安全边界显示"代码沙盒: 已整体关闭"。适合只想做"联网检索 + PDF 阅读"纯只读调研的场景。

**Q11: `tests/_e2e_test.py` / `tests/test_search.py` 会消耗真实 API 额度吗?**
→ ⚠️ **会, 而且是高额风险点**——它们会真实调用 LLM(推理费用)与博查搜索 API(次数费用): 每次跑通 e2e 约 2~10 次接口调用。**执行前务必确认 `.env` 里的密钥有效且有充足额度**, 建议先查账单/额度; CI 不运行这两个文件, 日常验证请用 `python -m pytest tests`(46 条离线用例, 零 API 消耗)。

**Q12: 任务异常后 partial_*.json 素材文件什么时候会被删除?**
→ 页面提示下载后请尽快保存; 下一次开始新任务时会触发 `temp_upload/` 自动清理, 删除旧的 partial 文件(见 §8 工程边界备注)。历史报告则在 `report_history.json`, 不受影响。

## 11. 老用户升级迁移指南(从旧版本升级)

> 覆盖范围: 从"1.3.x 及更早的纯字符截断/旧提示词版本"升级到本版本(1.4.0)。改动清单与
> 原因见 [CHANGELOG.md](CHANGELOG.md)。

### 11.1 `.env` 需要新增的环境变量(旧 .env 不必推倒重来, 追加即可)

| 变量 | 说明 | 缺失时的行为 |
| --- | --- | --- |
| `ALLOW_CODE_EXEC` | 沙盒代码执行开关(默认 `true`; `false` 整体关闭, 见 FAQ Q10) | 不填 = 开启, 行为同旧版 |
| `LLM_TIMEOUT` / `LLM_MAX_OUTPUT_TOKENS` / `LLM_CONTEXT_TOKENS` / `LLM_CLIENT_RETRIES` / `LLM_MAX_RETRIES` | LLM 调用调优(1.3 起引入, 均带默认值) | 不填 = 默认(60s / 8192 / 60000 / 2 / 3) |
| `SEARCH_TIMEOUT` / `BOCHA_COUNT` / `BOCHA_FRESHNESS` | 搜索调优(均带默认值) | 不填 = 默认 |

最简单的迁移: 把旧 `.env` 保留, 对照 `.env.example` 注释逐项确认即可(密钥两行
`OPENAI_API_KEY` / `BOCHA_API_KEY` / `LLM_MODEL` / `OPENAI_BASE_URL` 不变)。

### 11.2 prompts/ 目录改动(重要: 目录必须完整)

- 本版本全部节点提示词从"代码内嵌字符串"迁移到 **`prompts/*.txt` 模板文件**(1.3 起),
  共 7 个文件(见 §9 目录树)。**升级时请连同 prompts/ 整个目录一起更新**, 不要只覆盖
  `.py` 文件——缺失模板会直接报错: `缺少提示词模板文件 prompts/xxx, 请检查目录完整性`;
- 反思/工具/报告模板中【输出 JSON 格式、素材编号、来源 URL】等约定与代码强相关,
  请勿用旧版同名文件覆盖新版(结构已变: 反思输出要求 `sufficient/reason/missing_topics`)。
  自定义模板前请先通读 `tests/test_graph_builder.py` 与 CHANGELOG, 保持字段契约一致。

### 11.3 行为变化与迁移注意事项(旧版本运行数据)

- **反思判定更"抗截断"**: 素材总量较大时, 反思判断输入不再按旧版固定预算提前截断,
  而是随素材全量扩容(受模型上下文硬预算约束)——单次反思请求消耗的上下文 token 可能
  高于旧版, 若你的模型上下文较小(如 32K), 请调低 `LLM_CONTEXT_TOKENS`(如 28000);
- **反思失败防空转**: JSON 解析失败先内部重试 2 次再降级; 连续失败 2 轮后会强制进入
  报告节点(不再把 10 轮全部空耗), 行为比旧版"每轮都试错"更省额度;
- **异常素材兜底更完整**: 旧版仅"跑过节点后"的异常能保留素材; 新版连上传预读阶段的
  素材也会在任意异常时落盘 `temp_upload/partial_*.json` 并给出下载按钮;
- **会话状态不跨重启(沿用旧版说明)**: checkpointer 使用 InMemorySaver, 进程重启后
  图运行状态丢失, 仅适合单机演示(见 §8); 历史报告不受影响(report_history.json);
- **`report_history.json` / `temp_upload/`**: 新旧版本文件结构兼容, 无需手工迁移;
  应用启动时会自动清理 `temp_upload/` 旧残留(partial 素材请提前下载);
- **安全边界收紧提醒**: 升级后请阅读顶部「安全与部署边界声明」——沙盒为本地原型级,
  切勿因为版本号升高而把它当成可公网部署的隔离沙箱。

### 11.4 离线测试 & CI(开发者)

- 本地: `python -m pytest tests`(46 条, 不消耗 API); lint: `ruff check .`;
- 提交前保证 `python scripts/check_lock_consistency.py` 通过(锁文件一致);
- 若在 GitHub / GitLab 上启用 CI, 无需额外配置即可自动执行(见 §9)。

## 12. 开源 License

> 📄 **License 说明(占位)**: 本仓库目前**尚未选择开源许可证**, 默认保留所有权利 —— 仅可查看与本地学习, 未经作者许可不得分发、修改后商用或对外提供。
> 若你 Fork 后希望开源发布, 请自行补充许可证文件(如 MIT / Apache-2.0)并删除本占位说明。

## 13. 简历提示

本项目为**个人学习原型项目**, 技术栈与实现深度非常适合 **LLM Agent 方向(大模型应用 / AI Agent)校招简历**, 建议在简历 / 项目介绍中突出以下要点:

- 用 **LangGraph 状态图** 实现"规划 → 工具调用 → 反思 → 循环 → 报告"的完整 Agent 编排, 理解 **ReAct / 反思迭代** 这类 Agent 核心范式;
- 解决 LLM 输出的关键工程问题: **结构化输出(JSON)约束与失败重试**、**真实 token 预算管理(tiktoken)与上下文防溢出**、**结构化反思判定(替代关键词匹配)**、**防幻觉(报告只依据素材、结论强制标注来源)**;
- 通过 **Tool Use 模式** 接入联网搜索(博查 API)、PDF 解析、**受限 Python 代码沙盒(子进程隔离 + 真实路径白名单 + 超时强杀)**, 体现 Agent 工具安全工程能力;
- 用 **Streamlit 构建可交互演示**, 含逐节点实时日志、图表展示、报告下载与 **本地持久化历史**、checkpointer 异常兜底、统一日志 —— 完整的前后端闭环;
- 代码注释规范、模块划分清晰(入口 / 图构建 / 状态 / 工具 / 提示词 / 日志分离), 适合在面试中快速讲清系统架构与每一处设计取舍。

> 如实描述为"个人实践原型"即可: 亮点在于亲手把 LLM + Agent 编排 + 工具调用 + Web 界面完整打通, 并做了不少真实工程化细节(重试、限轮、超时、token 预算、沙盒加固、防编造、日志、兜底)。

## 14. 简历项目简介(直接复制版)

> 本模块提供两版可直接粘贴到简历"项目经历"区的项目简介(简体中文, 数据与代码现状一致);
> 如需英文简历, 按同一结构翻译即可。投递前建议按目标岗位微调关键词顺序, 保留量化的数字。

### 14.1 精简版(约 150 字)

> **本地 AI 调研 Agent(LangGraph + Streamlit)[个人项目]**
>
> 多轮自主迭代的 AI Agent: 输入主题自动完成"规划子任务 → 搜索/PDF/受限代码执行 → 反思评估 → 报告生成"闭环, 结论标注素材来源。工程亮点: 双层沙盒(静态拦截 + 运行时路径白名单 + 超时强杀); LLM 超时/解析失败重试容错; tiktoken 素材预算 + 反思"先判断后截断"; 全异常路径素材快照落盘; 46 条离线单测 + 双 CI。

### 14.2 详细版(约 300 字)

> **本地 AI 调研 Agent(LangGraph + Streamlit)[个人项目]**
>
> 基于 LangGraph 状态机实现多轮自主迭代的 AI 调研 Agent: 编排"规划 → 工具调用 → 反思评估 → 再搜集/出报告"闭环, 反思节点以结构化 JSON 判定信息充足性并给出下轮检索词, 报告强制标注【素材N】与来源 URL 防幻觉。工程亮点: ① 双层沙盒: 静态黑名单叠加子进程受限命名空间、运行时真实路径白名单与 30s 超时强杀, 阻断越权读密钥与死循环残留; ② LLM 统一容错: 指数退避重试、JSON 解析失败附错重试、反思连续失败防空转, 降低无效 API 消耗; ③ tiktoken 素材预算与判断信封动态扩容, 以"先判断后截断"消除素材截断误判; ④ 全异常路径素材快照落盘并支持 UI 下载。配套 46 条离线单测、GitHub/GitLab 双 CI 与依赖锁一致性校验。

### 14.3 使用提示

- 两条简介的数字(轮次上限 10、单测 46 条、双 CI)与仓库现状一致, 面试被追问时可到对应
  代码/测试中现场展开(推荐按 §2 架构 → §3 技术难点 → 代码注释的顺序讲解);
- 若简历字数受限, 优先保留"多轮自主迭代 / LLM 容错 / 双层沙盒 / 素材快照兜底 / 46 条单测 + 双 CI";
- 面试高频追问已在本仓库可查证: 沙盒为何不是生产级(§8/代码注释)、InMemorySaver 为何重启丢状态
  (§8)、为什么反思先判断后截断(§3 第 3 条 + `test_graph_builder.py`)、CI 为什么不含 e2e(FAQ Q11)。
