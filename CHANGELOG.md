# Changelog

本项目变更记录。版本号规则: 语义化版本(主.次.修订)。

## [1.6.0] - 2026-09-15 — 长期记忆/RAG + FastAPI 服务化 + 引用校验 + 人工确认(HITL)

> 本轮目标(增量开发): 在不动既有业务逻辑的前提下补齐四大增量 ——
> ① ChromaDB 长期记忆与文档分块检索(RAG, 跨任务复用历史素材);
> ② FastAPI 把 Agent 封装为 REST 服务(server.py, 可脱离 Streamlit 独立部署);
> ③ 报告引用一致性校验(素材编号可追溯); ④ Human-in-the-loop 人工确认闸门。
> 既有节点逻辑 / 断点续研 / 素材兜底 / 全部既有测试均保持行为不变。

### 🟢 新增功能

1. **长期记忆 + 文档检索(RAG)(核心)**
   - 新增 `memory/vector_memory.py`: `MemoryStore`(ChromaDB cosine)统一管理两类记忆
     —— `save_run`(历史调研任务记忆)与 `save_document`(上传文档全文分块入库);
     `chunk_text` 纯函数(默认 800 字符/块、100 字符重叠);
   - 图首新增 `memory_retrieve_node`(graph_builder.build_graph 传入 memory_store 时插入
     START 与 planner 之间): 任务开始前按主题检索相似历史记忆/文档片段注入素材,
     检索失败只记日志不阻断;
   - 任务完成后调用方回写 `save_run(topic, materials, report)`, 形成"检索→执行→回写"
     长期记忆闭环; `main.py` 新增 `MEMORY_ENABLED`(默认 true, ChromaDB 不可用自动降级)/
     `MEMORY_TOP_K` 配置; 侧边栏显示记忆库状态。

2. **FastAPI 服务化(核心)**
   - 新增 `server.py`: `POST /api/research`(JSON)、`POST /api/research/with-files`
     (multipart 上传 PDF/CSV)、`GET /api/research/{task_id}`(结果查询)、
     `GET /api/research`(任务列表, 按 seq 倒序)、`GET /health`;
   - `TaskStore`(内存 dict + 锁 + 自增 seq)管理异步任务, `ThreadPoolExecutor` 后台执行
     完整图流程(InMemorySaver + 长期记忆), 结果含报告/轮次/素材/引用校验标记;
     启动命令 `uvicorn server:app --port 8000`(见 README §快速开始)。

3. **报告引用一致性校验**
   - 新增 `core/report_verifier.py`: 从报告正文提取「素材N」引用(兼容【】/〔〕/()/（）/
     裸编号/页码后缀), 校验编号越界/重复, 在 Streamlit 报告区与 API 响应中显示
     ✅/⚠️ 校验标记。

4. **Human-in-the-loop 人工确认(可选, 默认关闭)**
   - `graph_builder` 新增 `confirmation_node`(LangGraph `interrupt` 暂停)与
     `route_after_confirmation` 纯函数; `build_graph(..., human_in_the_loop=True)` 启用后,
     反思判定"信息不足"时任务暂停, 调用方展示确认 UI, 以 `Command(resume=continue/stop)`
     恢复(继续搜集 / 停止出报告); `main.py` 新增 `HUMAN_IN_THE_LOOP` 配置与确认面板。

5. **单元测试**
   - 新增 `tests/test_vector_memory.py`(15 条)、`tests/test_api.py`(8 条)、
     `tests/test_report_verifier.py`(13 条), `test_graph_builder.py` 追加记忆检索 6 条 +
     HITL 5 条; 全量 `python -m pytest tests` → **172 passed**(约 45s)。

6. **依赖与文档**
   - `requirements.txt` 新增 `fastapi>=0.110.0` / `uvicorn>=0.29.0`(chromadb/langgraph
     既有); `.env.example` 补充 `MEMORY_ENABLED` / `MEMORY_TOP_K` / `MEMORY_CHUNK_SIZE` /
     `MEMORY_CHUNK_OVERLAP` / `HUMAN_IN_THE_LOOP` 说明; 版本号升至 1.6.0。

### 🟧 兼容性说明

- 全部增量默认关闭或与旧行为等价: 不传 memory_store / human_in_the_loop=False 时,
  图与 1.5.0 完全一致; server.py 为新增独立入口, 不影响 Streamlit 主流程;
- 长期记忆为本地单机 ChromaDB(单进程使用), 持久化目录默认 `./chroma_db`, 已加入
  `.gitignore`; API 服务为演示级(内存任务队列, 重启即失), 见 README「已知项目局限」。

## [1.5.0] - 2026-09-08 — Agent 会话 SqliteSaver 状态持久化(断点续研)

> 本轮目标(增量开发): 在不动既有业务逻辑的前提下, 为 LangGraph Agent 会话接入
> SqliteSaver checkpoint 持久化 —— 运行中间状态落盘本地 sqlite, 程序重启后可从
> 断点恢复未完成的调研会话; core 层 / graph 节点 / Streamlit 页面 / 全部既有单元测试
> / pytest 配置 / CI / Dockerfile 均保持原有行为不变。

### 🟢 新增功能

1. **LangGraph SqliteSaver 会话 checkpoint 持久化(核心)**
   - 新增 `core/checkpoint_store.py`(纯 core 层, 不依赖 streamlit / 根目录平铺模块):
     `CheckpointStore` 封装 `langgraph.checkpoint.sqlite.SqliteSaver`(懒加载依赖), 同一
     sqlite 文件内叠加会话登记表 `agent_sessions`(主题/状态/轮次/素材数/时间), 供前端
     列出历史会话、切换恢复;
   - 数据库文件命名 `agent_checkpoints.db`, 默认存放于项目根目录, 可用环境变量
     `CHECKPOINT_DB_PATH` 覆盖; 文件已加入 `.gitignore` / `.dockerignore`(不入库、
     不打包进镜像, 容器部署由宿主机 volume 挂载);
   - 进程级单例 `get_checkpoint_store()`: `CHECKPOINT_PERSIST=false` / 依赖缺失 /
     初始化失败一律返回 None —— 调用方自动退回原有 InMemorySaver 内存模式, 旧流程零影响;
   - 图执行方式不变: `build_graph(..., checkpointer=store.saver)`(build_graph 的
     checkpointer 可选参数为既有接口, 本轮未改任何节点逻辑, 仅更新注释文档)。

2. **Streamlit 最小改动: 会话选择组件(断点续研)**
   - `main.py` 侧边栏新增「🗂️ Agent 会话(断点续研)」: 展示持久化状态、列出历史会话
     (状态/主题/thread_id/轮次素材), 可"新建空白会话(默认)"或选中历史会话恢复;
   - 恢复语义: 选中"中断/进行中"会话后点「开始调研」, 以原 thread_id 从最后一个完成的
     checkpoint 继续执行(中断的节点会重跑), 已搜集素材/子任务/轮次均从 checkpoint 恢复;
   - 默认行为不变: 不选历史会话 = 新建空白会话, 与旧版表现完全一致; 会话完成/异常
     的素材兜底(partial JSON)、历史报告入库等既有机制全部保留。

3. **单元测试**
   - 新增 `tests/test_checkpoint_store.py`(13 条, 全部离线): 默认库文件名与 env 解析、
     表结构、会话登记簿 CRUD/倒序/无记录 no-op、`has_checkpoint`、同库"重启"后状态
     可读可续、真实 research 图"运行中崩溃 → 重建 → 恢复"端到端、不传 checkpointer
     时纯内存模式守护、模块 import 无副作用 —— sqlite 一律 `:memory:`(不产生磁盘文件);
   - 既有 113 条用例零改动; 全量 `python -m pytest tests` → **126 passed**。

4. **依赖与文档**
   - 新增依赖 `langgraph-checkpoint-sqlite>=3.1.1`(提供 `langgraph.checkpoint.sqlite`,
     与 langgraph 1.2.x / langgraph-checkpoint 4.x 配套); `requirements.txt` /
     `pyproject.toml` / `requirements-lock.txt` 同步更新(锁文件补充
     langgraph-checkpoint-sqlite==3.1.1 / aiosqlite==0.22.1 / sqlite-vec==0.1.9,
     CI 一致性校验通过);
   - README 新增「Agent 会话持久化」说明(见 §4 依赖 / §5.1 Docker volume / §7 功能
     特性 / §8 已知局限 / §9 目录 / §11 升级迁移); `.env.example` 补充
     `CHECKPOINT_PERSIST` / `CHECKPOINT_DB_PATH` 说明; 版本号升至 1.5.0。

### 🟧 兼容性说明

- 不传 checkpointer(或持久化被关闭/依赖缺失)时, 图与页面走原内存模式, 行为与 1.4.0
  完全一致; 旧 `report_history.json` / partial 素材兜底 / 上传清理逻辑均未改动;
- 持久化边界: 单进程使用(多进程/多实例并发写同一 sqlite 文件不受支持), 见
  README「已知项目局限」。

## [1.4.0] - 2026-09-04 — P0 高危项闭环 + P1 完整可交付版本

> 本轮目标: 修复 P0 高危安全问题与可靠性缺口、补齐 P1 交付项(P2 仅记录不实现)。
> 详细说明见 README(安全声明 / 升级迁移 / FAQ / 工程边界备注)。

### 🟥 P0(发布前必须完成, 本轮全部实现)

1. **反思节点时序校验(P0-1): 先判断、后截断**
   - 重构 `graph_builder.make_reflection_node` 为显式两阶段: 阶段一"判断"先做素材全量
     无损盘点(逐条真实 token 计数、零截断), 按"判断信封"组织 LLM 结构化判定输入;
     阶段二"截断"严格发生在判定完成后, 才允许裁剪展示用文本。
   - 新增 `_corpus_token_total` / `_reflection_judgment_envelope`: 判断信封随素材全量
     动态扩容(只受模型上下文硬预算 `_context_cap` 约束) —— 素材总量超出旧的固定
     3 万 token 软预算时不再提前截断, 杜绝"素材提前截断 → LLM 误判信息不足 →
     空转搜集轮次/浪费 API 额度"。
   - 配套单元测试(见 `tests/test_graph_builder.py`, P0-1 一组): 尾部素材标记必须在判断
     输入中完整出现、展示截断发生在结构化判定之后、素材 State 本体不被截断改写。

2. **README 安全声明醒目置顶(P0-2)**
   - README 开头新增独立「🚨 安全与部署边界声明(先读)」: 明确本项目沙盒为**非生产级
     沙箱**——静态黑名单仅是简单文本字面拦截, 可被字符串拼接、exec/eval、动态 import、
     变量别名绕过; **缺少 CPU/内存/fork 炸弹资源限制**; 所有用户代码运行在**同一 UID**、
     仅目录隔离; **禁止公网多租户对外部署, 仅用于内部演示与本地使用**(不藏在文档深处)。
   - 沙盒模块 `tools/code_exec_tool.py`、`tools/_sandbox_runner.py` 同步注明局限。

3. **反思 JSON 解析失败逻辑优化(P0-3): 先内部重试、后降级、防空转**
   - `_llm_json_ask` 增加 `max_retries` 参数(默认 `MAX_JSON_RETRY`); 反思节点显式传
     `REFLECT_JSON_RETRY=2`: JSON 解析失败先在节点内部重试反思请求(附解析错误提醒),
     全部重试失败才降级为"信息不足"继续搜集。
   - 新增状态字段 `reflection_failures`(StateSchema): 判定成功清零、失败 +1; 连续失败
     达到 `MAX_REFLECT_FAILURES=2` 后, `route_after_reflection` 强制进入报告节点
     (基于现有素材出报告), 不再把 `MAX_ITERATIONS` 轮次空耗在必然失败的反思上。
   - 配套单元测试: 解析失败调用次数 = 1+REFLECT_JSON_RETRY 后降级 / 内部重试成功清零 /
     路由防空转分支。

4. **全异常分支素材落盘 & UI 校验(P0-4): 杜绝静默失败**
   - `main.py` 将"构建 LLM → 图流式执行"全程纳入异常兜底范围(`_salvage_run_materials`):
     LLM 重试全部失败、规划/报告节点异常、任务运行异常、build_llm 配置错误等任意一步
     抛错, 素材来源按"checkpointer 快照 → 本地增量追踪素材(含上传文件预读素材)"两级
     取回并落盘 `temp_upload/partial_*.json`(文件内记录错误原因), UI 一律给出明确提示
     并支持下载; 无素材时也显式提示"无素材可保留", 兜底自身失败时显式报错并写日志。
   - 上传文件保存失败不再静默跳过, UI 明确报错; partial 文件提示"下次任务开始时清理,
     请尽快下载"。
   - `_save_partial_run` 落盘内容增加 `error` 字段便于追溯。

### 🟧 P1(完整可交付版本, 本轮实现)

1. **核心模块单元测试补全**
   - 新增 `tests/test_graph_builder.py`(31 条, 全部离线、注入假 LLM):
     token 裁剪逻辑(预算内不改 / 超限截断带尾注 / 无 tiktoken 字符估算回退)、
     token budget 预算告警(≥90% 触发 warning / 低占用不告警)、反思 JSON 解析与
     字段规整、素材去重逻辑、P0-1 时序、P0-3 重试与防空转、规划/报告/路由节点。
   - `tests/test_sandbox.py` 重构为 pytest 函数式(15 条), 保留 `python
     tests/test_sandbox.py` 直接运行能力(内部转调 pytest)。
   - 新增 `pytest.ini`: 只收集 `test_*.py`, `_e2e_test.py` / `test_search.py`
     (消耗真实 API) 天然不进 CI。
   - 离线验证: `python -m pytest tests` → **46 passed**(不调用 LLM API / 不联网)。

2. **CI 配置(兼容 GitHub Actions / GitLab CI)**
   - `.github/workflows/ci.yml` + `.gitlab-ci.yml`: ① 安装锁文件后运行全部离线
     pytest; ② ruff 代码 lint; ③ `scripts/check_lock_consistency.py` 锁文件一致性校验
     (顶层约束 vs 精确锁定版本, 无网络依赖, packaging 缺失时内置回退比较器)。

3. **CHANGELOG.md**(本文件)建立, 供后续版本迭代追溯。

4. **README 补充**
   - 「老用户升级迁移指南」: `.env` 新增环境变量清单、prompts/ 模板目录约定、
     旧版本迁移注意事项(checkpointer 状态不跨重启、partial 素材兜底、安全边界变化);
   - FAQ 新增: tiktoken 安装失败处理、requirements-lock.txt 使用/重新生成方式、
     如何关闭沙盒代码执行(`ALLOW_CODE_EXEC=false`, 本轮已实现该开关);
   - 显著标注 `tests/_e2e_test.py` 与 `tests/test_search.py` **真实调用 LLM/搜索 API,
     执行前务必确认密钥与额度, 防止意外高额费用**; CI 不运行二者。

### 🟩 P2(后续迭代, 本轮仅记录不实现)

- [ ] P2-1 Dockerfile 容器化部署(多租户/公网部署需先解决 P0-2 沙盒边界);
- [ ] P2-2 ruff + black + isort 完整代码格式化 lint 配置(当前 CI 仅默认规则集
      E4/E7/E9/F 检查);
- [ ] P2-3 checkpointer 增加文件/Redis 等持久化 saver(解决 InMemorySaver 服务重启
      丢失会话状态), 候选 SqliteSaver / RedisSaver(见 graph_builder.build_graph 注释);
- [ ] P2-4 沙盒增加 CPU/内存配额与进程树限制(防御 fork 炸弹、资源耗尽攻击),
      候选 resource 模块 rlimit / cgroup(见 code_exec_tool 注释);
- [ ] P2-5 素材语义去重优化(embedding 相似度), 解决仅文本比对无法识别语义近似素材
      (见 graph_builder._is_duplicate_material 注释)。

### 🛠 工程边界备注(本轮文档化, 见 README「已知项目局限」)

- RotatingFileHandler **多进程日志风险**: 当前为单进程 Streamlit 模式可用; 切换多进程
  部署会出现日志轮转竞争、文件损坏(见 logging_setup.py 注释);
- InMemorySaver 限制: checkpointer 会话状态仅存单进程内存, 服务重启即丢失, 仅适合
  单机演示; 中途异常素材靠 partial JSON 落盘兜底(见 main.py / build_graph 注释);
- temp_upload/ 清理竞争条件: 全局共享目录在多进程/多实例并发下会互相删除对方正在
  使用的文件, 仅支持单进程模式(见 main.py `_cleanup_temp_files` 注释)。

## [1.3.0] - 2026-08(基线版本, 无逐条记录)

- 反思节点改造为结构化 JSON 判定 {sufficient, reason, missing_topics}(替代字符串
  关键字匹配), 条件路由直接消费结构化结果;
- LLM 调用统一 `_invoke_llm`: 客户端超时 + 指数退避重试; JSON 解析失败通用重试
  `MAX_JSON_RETRY`;
- 素材拼接升级为 tiktoken 真实 token 预算裁剪(替代纯字符截断), 输出 max_tokens 上限
  与上下文余量告警;
- 代码沙盒加固: 真实路径运行时白名单(拦截拼接字符串越权读 .env)、子进程隔离 +
  超时强杀; 上传文件预读、素材文本级去重、Streamlit 实时日志与历史 JSON 持久化;
- 提示词全部抽离到 prompts/*.txt, 报告强制标注素材编号+来源 URL。
