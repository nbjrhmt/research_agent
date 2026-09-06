"""
main.py —— 程序入口(Streamlit 网页界面)

功能(规格文档):
    1. 文本输入框: 填写调研主题
    2. 文件上传组件: 上传 PDF / CSV(自动预读为素材; CSV 给出列名+行数+前20行预览)
    3. 实时日志面板: 逐步展示拆解的子任务、调用工具、素材片段、反思结论
    4. 结果面板: 展示最终调研报告 + 代码生成的图表图片
    5. 历史持久化: 每次完成的报告自动存入侧边栏历史, 并同步写入根目录 report_history.json,
       浏览器关闭 / 重启 streamlit 后历史可恢复; 侧边栏【清空全部历史】同步清空 json 文件

运行: streamlit run main.py
"""
import os
import time
import uuid

import streamlit as st

# ---- 2026 工程重构 P1: 纯业务逻辑已迁入 core/(配置/历史持久化/临时文件/上传预读) ----
# main.py 只保留 Streamlit UI 与事件编排; core 包不依赖 streamlit, 可独立 import 与单测。
# 以下全部以"旧私有名"别名导入, 保证本文件既有调用点零改动。
from core.file_store import (  # 上传落盘 / 清理 / 删除 / partial 快照 / 图表发现
    cleanup_temp_files as _cleanup_temp_files,
    delete_uploaded_files as _delete_uploaded_files,
    find_new_images,
    save_partial_run as _save_partial_run,
    save_upload,
)
from core.history_store import (  # 历史 JSON 持久化 / 记录构造 / 文件名辅助
    MAX_HISTORY,
    clear_report_history_on_disk as _clear_report_history_on_disk,
    clip_topic as _clip_topic,
    load_report_history_from_disk as _load_report_history_from_disk,
    new_report_record,
    report_filename as _report_filename,
    save_report_history_to_disk as _save_report_history_to_disk,
)
from core.ingest import ingest_upload as _ingest_upload  # 上传文件预读为素材

from logging_setup import get_logger

_logger = get_logger("main")


# 页面配置必须在任何 st 组件之前
st.set_page_config(page_title="本地个人调研Agent", page_icon="🔎", layout="wide")

# ============================ 📜 历史报告存储: 会话内存 + JSON 本地持久化 ============================
# 存储策略(与旧版完全一致, 实现已迁 core/history_store.py, 本文件只保留路径与内存态):
#   1. 运行期历史保存在 st.session_state.report_history(会话内存), 供侧边栏选择/预览/下载;
#   2. 每次新调研完成 / 点击【🗑️ 清空全部历史】时, 同步把完整列表覆盖写入根目录 report_history.json;
#   3. main.py 启动(页面首次加载)时: 若 report_history.json 存在则读取恢复历史, 不存在则初始化为空列表;
#   4. 容错: 文件缺失 / JSON 损坏 / IO 异常时只打印警告并降级为"纯内存模式", 程序照常运行, 不崩溃。
# 已知限制(沿用旧版): 单用户本地原型, report_history.json 中存放的是全部历史报告的完整文本。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # 项目根目录
REPORT_HISTORY_FILE = os.path.join(BASE_DIR, "report_history.json")  # 历史报告持久化载体(仅此一个文件)

if "report_history" not in st.session_state:
    # 每个元素的结构:
    # {"id": 唯一id, "topic": 调研主题, "finished_at": 完成时间,
    #  "file_stamp": 下载文件名时间戳(如 2026-09-02_192017), "report": 完整markdown报告文本}
    st.session_state.report_history = _load_report_history_from_disk(REPORT_HISTORY_FILE)  # 有 json 文件则恢复, 无则空列表

TEMP_UPLOAD_DIR = os.path.join(BASE_DIR, "temp_upload")  # 上传文件/图表存放目录
os.makedirs(TEMP_UPLOAD_DIR, exist_ok=True)


# ============================ 🧹 临时文件生命周期管理 ============================
# 实现已迁入 core/file_store.py(2026 工程重构 P1), 行为与旧版完全一致:
#   1) 应用启动(首个会话): 清空 temp_upload/ 残留(上一进程遗留的上传/图表/沙盒临时文件);
#   2) 每次开始新调研前: 清理上一轮遗留文件, 但保留当前结果面板仍在引用的图表;
#   3) 每次调研结束(成功/失败): 删除本次上传文件的磁盘副本(内容已进入素材/历史);
#   4) 任务异常兜底: 已搜集素材落盘 temp_upload/partial_*.json(_save_partial_run)。
# 本文件只负责按上面时机调用(_cleanup_temp_files / _delete_uploaded_files /
# _save_partial_run / find_new_images 均从 core.file_store 别名导入, 见文件顶部),
# 具体逻辑与"单进程工程边界备注"见 core/file_store.py。


if "temp_boot_cleanup_done" not in st.session_state:
    # 应用启动(首个会话): 清理上一进程遗留的临时文件, 防止磁盘持续膨胀
    _cleanup_temp_files()
    st.session_state["temp_boot_cleanup_done"] = True

try:
    # 依赖导入放在 try 里, 方便给用户明确的安装提示
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))

    from graph_builder import (
        ALLOW_CODE_EXEC, DEFAULT_MODEL, MAX_ITERATIONS, build_graph, build_llm,
    )
    from langgraph.checkpoint.memory import InMemorySaver  # 异常兜底: 取回已搜集素材
    from tools.search_tool import bocha_web_search  # 联网搜索: 博查(Bocha) API
except Exception as exc:  # noqa: BLE001
    st.error(f"依赖导入失败, 请先安装依赖: pip install -r requirements.txt\n\n原始错误:\n{exc}")
    st.stop()

# 节点显示用图标
_NODE_ICONS = {
    "planner_node": "📋 规划节点",
    "tool_node": "🛠 工具调用",
    "reflection_node": "🧠 反思评估",
    "report_node": "📝 报告生成",
}

MAX_FILE_SIZE_MB = 30  # 单个上传文件大小上限


# ============================ 上传文件相关 ============================
# 实现已迁入 core/(2026 工程重构 P1):
#   _save_upload    → core.file_store.save_upload(data, original_name)
#   _preview_csv    → core.ingest.preview_csv(file_path)
#   _ingest_upload  → core.ingest.ingest_upload(fname)(本文件顶部别名 _ingest_upload)
# 本文件只保留上传大小校验与页面交互逻辑。


# ============================ 图执行与实时日志 ============================
def _salvage_run_materials(exc: Exception, graph, config, user_query: str,
                           fallback_materials: list) -> None:
    """
    P0-4 异常素材兜底(由 _render_graph_run 的 except 分支调用, 覆盖全部异常路径):
        1. 优先读取 LangGraph checkpointer 快照(已跑过节点时, 素材最完整);
           读不到快照(异常发生在 build_llm / 首个节点之前)则退回本地追踪的
           fallback_materials(至少包含上传文件预读素材);
        2. 素材非空 → 落盘 temp_upload/partial_*.json, 并把路径写入
           st.session_state["last_salvage"], 供外层 UI 提示 + 下载按钮;
        3. 素材为空 → last_salvage.path=None, UI 明确提示"无素材可保留"(不静默);
        4. 兜底自身失败 → 记录 last_salvage_error, UI 显式报错, 不掩盖原始任务异常。
    """
    try:
        snapshot_values = None
        if graph is not None and config is not None:
            try:
                snapshot = graph.get_state(config)
                values = getattr(snapshot, "values", None)
                if values is None and isinstance(snapshot, tuple) and snapshot:
                    values = snapshot[0]
                if isinstance(values, dict):
                    snapshot_values = values
            except Exception as inner:  # noqa: BLE001 —— 无任何节点快照属正常(异常发生得过早)
                _logger.warning("异常兜底-读取 checkpointer 快照失败, 改用本地追踪素材: %s", inner)
        if snapshot_values is not None:
            state_values = snapshot_values
        else:
            # 还没跑过任何节点(如 build_llm 抛错): 退回本地追踪素材
            state_values = {
                "user_query": user_query,
                "sub_tasks": [],
                "iteration_count": 0,
                "collected_info": fallback_materials,
            }
        materials = state_values.get("collected_info") or []
        if not materials and fallback_materials:  # 防御性合并: 快照异常为空时用本地追踪
            state_values = dict(state_values)
            state_values["collected_info"] = fallback_materials
            materials = fallback_materials

        error_text = f"{type(exc).__name__}: {exc}"
        if materials:
            salvage_path = _save_partial_run(state_values, error_text=error_text)
            st.session_state["last_salvage"] = {
                "path": salvage_path,
                "count": len(materials),
                "query": user_query,
            }
            _logger.warning("任务异常(%s), 已落盘 %s 条素材到 %s",
                            error_text[:200], len(materials), salvage_path)
        else:
            st.session_state["last_salvage"] = {"path": None, "count": 0, "query": user_query}
            _logger.warning("任务异常(%s), 异常前尚未搜集到素材, 无 partial 文件可保留",
                            error_text[:200])
    except Exception as exc2:  # noqa: BLE001 —— 兜底失败也要在 UI 显式报错, 杜绝静默失败
        st.session_state["last_salvage"] = {"path": None, "count": -1, "query": user_query}
        st.session_state["last_salvage_error"] = f"{type(exc2).__name__}: {exc2}"
        _logger.exception("任务异常后的素材落盘兜底也失败(原始异常 %s): %s",
                          type(exc).__name__, exc2)


def _render_graph_run(user_query: str, fnames: list):
    """
    运行 LangGraph 主流程并实时展示每一步:
    返回 (最终报告, 轮次, 素材总数, 生成图表路径列表)
    """
    # ---- 0. 初始化 State: 上传文件先自动预读为素材 ----
    initial_materials = []
    logs = []
    for fname in fnames:
        material, log_line = _ingest_upload(fname)
        if material:
            initial_materials.append(material)
        logs.append(log_line)
    for line in logs:
        st.markdown(f"📎 {line}")

    initial_state = {
        "user_query": user_query,
        "sub_tasks": [],
        "collected_info": initial_materials,   # 已预读素材
        "reflection": "",
        "final_report": "",
        "iteration_count": 0,
        "uploaded_files": fnames,
        "steps_log": [],
    }

    # =====================================================================
    # P0-4 异常素材兜底: 从"构建 LLM"到"图流式执行"全程纳入 try 范围 —— 任何一步抛错
    # (LLM 重试全部失败 / 沙盒致命错误 / 搜索 API 报错 / 任务运行异常等), 只要存在
    # 已搜集素材就落盘 partial JSON, UI 给出提示并支持下载, 杜绝静默失败。
    # 素材来源优先级: ① LangGraph checkpointer 快照(已跑过节点时, 含最新素材);
    #                ② 本地增量追踪的 fallback_materials(尚未产生任何节点快照时,
    #                  至少保留上传文件预读素材)。
    # =====================================================================
    progress_bar = st.progress(0.0)
    run_started_at = time.time()      # 用于识别本轮新生成的图表
    fallback_materials = list(initial_materials)   # 随事件流增量追加, 兜底素材源
    total_materials = len(initial_materials)
    max_round = 0
    final_report = ""
    llm = None
    graph = None
    config = None

    # stream_mode="updates": 每执行完一个节点就产出一次 {节点名: 该节点更新的字段}
    try:
        llm = build_llm()   # 未配置 Key 时会在这里抛错, 由下方兜底 + 外层提示
        # LangGraph checkpointer 兜底(工程边界: InMemorySaver 仅单进程内存, 服务重启即丢,
        # 只适合单机演示; 中途异常素材靠下面落盘 partial JSON 保留, 见 README 已知局限)
        checkpointer = InMemorySaver()
        thread_id = uuid.uuid4().hex[:12]
        config = {"configurable": {"thread_id": thread_id}}
        graph = build_graph(llm, web_search_tool=bocha_web_search, checkpointer=checkpointer)  # 博查API联网搜索
        for event in graph.stream(initial_state, config=config, stream_mode="updates"):
            for node_name, payload in event.items():
                if node_name.startswith("__"):
                    continue  # 跳过 LangGraph 内部节点

                icon_title = _NODE_ICONS.get(node_name, node_name)
                st.markdown(f"**▶ {icon_title}**")

                # 1) 实时日志行
                for line in payload.get("steps_log") or []:
                    st.markdown(f"- {line}")

                # 2) 规划结果: 展示子任务列表
                if node_name == "planner_node" and payload.get("sub_tasks"):
                    for i, task in enumerate(payload["sub_tasks"], start=1):
                        st.markdown(f"  🎯 子任务{i}: {task}")

                # 3) 工具节点: 展示本轮新增素材片段 + 进度条
                if node_name == "tool_node":
                    for entry in payload.get("collected_info") or []:
                        with st.expander("查看本轮素材片段(前 1200 字)", expanded=False):
                            st.text(str(entry)[:1200])
                    max_round = int(payload.get("iteration_count") or max_round)
                    added = payload.get("collected_info") or []
                    total_materials += len(added)
                    fallback_materials.extend(added)   # 增量追踪, 供异常兜底使用
                    progress_bar.progress(min(max_round / MAX_ITERATIONS, 1.0))

                # 4) 反思节点: 高亮结论
                if node_name == "reflection_node" and payload.get("reflection"):
                    reflection_text = str(payload["reflection"])
                    marker = "✅ 反思结论(信息充足)" if "任务信息充足" in reflection_text or "信息充足" in reflection_text else "⚠️ 反思结论(继续搜集)"
                    with st.expander(f"{marker}: 查看完整反思", expanded=False):
                        st.text(reflection_text)

                # 5) 报告节点: 暂存报告, 最后统一渲染
                if node_name == "report_node" and payload.get("final_report"):
                    final_report = str(payload["final_report"])
    except Exception as exc:  # noqa: BLE001 —— P0-4: 全部异常路径统一走素材落盘兜底
        _salvage_run_materials(exc, graph, config, user_query, fallback_materials)
        raise  # 原始异常继续抛出, 由外层(main 执行区)统一渲染错误提示

    progress_bar.empty()

    # ---- 收集本轮生成的图表图片(实现见 core.file_store.find_new_images) ----
    new_images = find_new_images(TEMP_UPLOAD_DIR, run_started_at)

    return final_report, max_round, total_materials, new_images


# ============================ 📜 历史记录辅助(实现见 core/history_store.py) ============================
# _clip_topic / _safe_filename_part / _report_filename / new_report_record / MAX_HISTORY
# 均从 core.history_store 别名导入(见文件顶部), 字段与文件名格式与旧版完全一致。


def _save_report_to_history(topic: str, report: str) -> None:
    """调研完整跑完拿到 final_report 后调用: 自动存入历史列表(内存), 并同步落盘持久化。

    - 新报告插入列表最顶部(索引0);
    - 最多保留 MAX_HISTORY(20) 条, 超出自动丢弃最老的记录(内存与 json 文件保持一致);
    - 落盘(写入 report_history.json)失败只打印警告, 降级为纯内存模式, 不影响本轮结果;
    - 记录构造(new_report_record)与落盘(_save_report_history_to_disk)实现在 core/history_store.py。
    """
    if not (report or "").strip():
        return  # 未生成有效报告文本时不入历史
    record = new_report_record(topic, report)
    history = st.session_state.setdefault("report_history", [])
    history.insert(0, record)    # 新报告插入列表最顶部
    del history[MAX_HISTORY:]    # 超过 20 条自动丢弃最老记录
    # 同步把完整列表覆盖写回 report_history.json(本地持久化)
    _save_report_history_to_disk(history, REPORT_HISTORY_FILE)


# ============================ 页面 ============================
st.title("🔎 本地个人调研分析 Agent")
st.caption("输入主题 → 自动拆解子任务 → 联网搜索 / 读取上传文档 → 反思迭代 → 输出结构化调研报告。"
           "所有输出仅来源于搜索返回与上传文件内容, 不联网时不会编造。")

with st.sidebar:
    st.subheader("⚙️ 运行配置(.env)")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key and not api_key.startswith("你的"):
        st.success("✅ API Key 已从 .env 环境变量加载完成")
    else:
        st.error("❌ 未配置 API Key：请编辑项目根目录 .env 文件，配置完成后刷新页面")
    st.write(f"- 模型: {os.getenv('LLM_MODEL', '').strip() or DEFAULT_MODEL}")
    st.write(f"- 接口: {os.getenv('OPENAI_BASE_URL', '').strip() or 'OpenAI 官方'}")
    bocha_key = os.getenv("BOCHA_API_KEY", "").strip()
    if bocha_key and not bocha_key.startswith("你的"):
        st.write("✅ 联网搜索: 博查(Bocha) Web Search API 已配置")
    else:
        st.write("❌ 联网搜索: 未配置 BOCHA_API_KEY(联网搜索将不可用)")
    st.divider()
    st.subheader("🛡️ 安全边界")
    st.write(f"- 工具最多迭代 {MAX_ITERATIONS} 轮, 到达上限自动生成报告")
    if ALLOW_CODE_EXEC:
        st.write("- 代码沙盒仅允许 pandas / matplotlib, 禁止删除修改文件")
    else:
        st.write("- 代码沙盒: 已整体关闭(ALLOW_CODE_EXEC=false), Agent 不执行任何代码")
    st.write("- 报告只基于素材, 严禁编造素材中不存在的事实")
    st.write("- 上传文件与图表保存在: `temp_upload/`")

    # ============================ 📜 历史调研报告面板 ============================
    # 数据来源: st.session_state.report_history(内存, 每次启动从 report_history.json 自动恢复;
    # 新调研完成 / 点清空按钮时同步读写该 json 文件, 见文件顶部"存储策略")。
    st.divider()
    with st.expander("📜 历史调研报告", expanded=False):
        history = st.session_state.get("report_history") or []
        if not history:
            st.caption("暂无历史报告，完成调研后会出现在这里")
        else:
            # 下拉框每条展示格式: 时间｜主题(主题最多截取 25 个字符)
            label_map = {
                rec["id"]: f"{rec['finished_at']}｜{_clip_topic(rec['topic'], 25)}"
                for rec in history
            }
            sel_id = st.selectbox(
                "选择报告",
                options=list(label_map.keys()),          # 用唯一 id 做选项值, 避免同主题重复串选
                format_func=lambda rid: label_map[rid],  # 显示为"时间｜主题(截断)"
                key="history_report_selector",
            )
            # 按 id 取回选中的记录(防御性兜底: 找不到则取最新一条)
            selected = next((rec for rec in history if rec["id"] == sel_id), history[0])

            # 选中条目的元信息: 完整主题 + 完成时间
            st.markdown(f"**📋 完整主题:** {selected['topic']}")
            st.caption(f"⏰ 完成时间: {selected['finished_at']}")

            # 下载: data 取自内存文本 -> 浏览器直接下载, 服务器不生成物理文件
            st.download_button(
                "⬇️ 下载该报告为 MD",
                data=selected["report"].encode("utf-8"),
                file_name=_report_filename(selected),   # 如: 2026-09-02_192017_主题.md
                mime="text/markdown",
                use_container_width=True,
            )

            # 折叠预览: 展开后在网页内直接渲染完整 markdown 报告(纯内存数据)
            with st.expander("👀 预览报告内容", expanded=False):
                st.markdown(selected["report"])

            # 清空: 同时清空 st.session_state 内存历史与 report_history.json 文件内容
            # (当前"last_run"结果区不受影响)
            if st.button("🗑️ 清空全部历史", use_container_width=True):
                st.session_state.report_history = []
                st.session_state.pop("history_report_selector", None)  # 重置下拉选中态, 防止残留索引
                _clear_report_history_on_disk()   # 同步清空 report_history.json 文件
                st.rerun()

# ---------- 输入区 ----------
st.subheader("1️⃣ 输入调研主题")
user_query = st.text_area(
    "调研主题",
    placeholder="例如: 2024 年中国新能源汽车销量 TOP10 品牌及主要技术路线对比",
    height=90,
    label_visibility="collapsed",
)

st.subheader("2️⃣ 上传素材文件(可选)")
uploaded_files = st.file_uploader(
    "支持 PDF / CSV, 上传后会自动读取为素材",
    type=["pdf", "csv"],
    accept_multiple_files=True,
    label_visibility="collapsed",
)

start_clicked = st.button("🚀 开始调研", type="primary", use_container_width=True)

# ---------- 执行区 ----------
if start_clicked:
    topic = (user_query or "").strip()
    if not topic:
        st.warning("请先输入调研主题。")
    else:
        # 0) 启动前置校验: BOCHA_API_KEY 未配置时明确提示联网搜索不可用(不阻断, 可仅基于上传素材运行)
        bocha_key = (os.getenv("BOCHA_API_KEY", "") or "").strip()
        if not bocha_key or bocha_key.startswith("你的"):
            st.warning("未配置 BOCHA_API_KEY: 联网搜索当前不可用, 任务将只能基于上传素材/已有资料进行。"
                       "如需联网检索, 请在项目根目录 .env 填入博查 Key 后刷新页面。")

        # 0.5) 开始新任务前自动清理过期临时文件(保留当前结果面板仍在引用的图表)
        keep_images = (st.session_state.get("last_run") or {}).get("images") or []
        _cleanup_temp_files(keep_files=keep_images)

        # 1) 保存上传文件(保存失败也要明确提示, 不静默跳过)
        fnames = []
        for up in uploaded_files:
            if up.size is not None and up.size > MAX_FILE_SIZE_MB * 1024 * 1024:
                st.warning(f"跳过超大文件: {up.name}(>{MAX_FILE_SIZE_MB}MB)")
                continue
            try:
                fnames.append(save_upload(up.getbuffer(), up.name))
            except OSError as exc:
                st.error(f"保存上传文件 {up.name} 失败(磁盘/权限问题): {exc}, 该文件将被跳过")

        st.markdown("---")
        st.subheader("3️⃣ 实时执行日志")
        status = None
        try:
            with st.status("⏳ 任务执行中……", expanded=True) as status:
                report, used_rounds, material_count, images = _render_graph_run(topic, fnames)
                status.update(label=f"✅ 执行完成(共 {used_rounds} 轮, {material_count} 条素材)", state="complete", expanded=False)

            # 存入 session_state, 后续任何页面交互都不会丢失本次结果
            st.session_state["last_run"] = {
                "report": report,
                "used_rounds": used_rounds,
                "material_count": material_count,
                "images": images,
                "query": topic,
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            # ---- 调研完整跑完, 拿到 final_report 后自动存入历史(内存 + 同步落盘 json) ----
            # 说明: 函数内部先插入 session_state 内存历史, 再把完整列表覆盖写入 report_history.json。
            _save_report_to_history(topic, report)   # 新报告插到最顶部, 最多保留 20 条, 持久化到本地
            _delete_uploaded_files(fnames)           # 任务结束: 自动清理本次上传文件的磁盘副本
            st.session_state.pop("last_salvage", None)  # 成功后清掉上一次的异常兜底提示
            st.session_state.pop("last_salvage_error", None)
            _logger.info("调研完成: 「%s」 共 %s 轮 / %s 条素材", topic, used_rounds, material_count)
        except Exception as exc:  # 捕获 LLM 网络错误 / JSON 重试失败 / Key 未配置等, 给出友好提示
            if status is not None:
                status.update(label="❌ 任务异常终止", state="error")
            st.error(f"任务终止: {exc}")
            st.info("排查建议: ① .env 的 Key/模型名是否正确且已刷新页面; ② 网络能否访问所配接口; "
                    "③ 换一个更常见的模型名; ④ 查看上方日志确认哪一步出错。")

            # ---- 异常兜底展示(P0-4): 无论异常发生在哪一步, 都给出明确的素材去向提示 ----
            salvage = st.session_state.pop("last_salvage", None)
            if salvage:
                if salvage.get("count", 0) == -1:
                    st.error("⛑ 异常兜底: 部分素材落盘失败, 本次无法保存素材文件。原因: "
                             f"{st.session_state.pop('last_salvage_error', '未知')}, "
                             "详见 logs/app.log; 上传文件副本已清理。")
                elif salvage.get("path"):
                    st.warning(f"⛑ 已自动保留异常前搜集的 {salvage['count']} 条素材(未白跑, "
                               f"含上传文件预读素材): `{salvage['path']}`")
                    try:
                        with open(salvage["path"], encoding="utf-8") as f:
                            partial_text = f.read()
                        st.download_button(
                            "⬇️ 下载已搜集素材(JSON)",
                            data=partial_text.encode("utf-8"),
                            file_name=os.path.basename(salvage["path"]),
                            mime="application/json",
                        )
                        st.caption("提示: partial_*.json 会在下次任务开始清理临时文件时被删除, 请尽快下载。")
                    except Exception:  # noqa: BLE001 —— 展示失败不影响主错误提示
                        pass
                else:
                    st.info("⛑ 任务异常, 且异常发生在搜集到素材之前: 本次没有可保留/下载的部分素材, "
                            "请根据上方日志排查后重新运行。")
            else:
                st.info("⛑ 任务异常, 未检测到可保留的部分素材(发生在素材搜集阶段之前)。")
            _delete_uploaded_files(fnames)  # 异常终止也清理本次上传文件副本(素材已落盘兜底)
            _logger.exception("调研任务异常终止: 「%s」", topic)

# ---------- 结果区(展示最近一次成功结果) ----------
last_run = st.session_state.get("last_run")
if last_run:
    st.markdown("---")
    st.subheader("4️⃣ 调研报告")
    st.caption(f"主题: {last_run['query']}　|　完成时间: {last_run['finished_at']}　"
               f"|　工具轮次: {last_run['used_rounds']}/{MAX_ITERATIONS}　|　素材条数: {last_run['material_count']}")
    st.markdown(last_run["report"])

    if last_run["images"]:
        st.subheader("📊 代码生成的图表")
        for img_path in last_run["images"]:
            st.image(img_path, caption=os.path.basename(img_path), use_container_width=True)

    st.info("⚠️ 报告由大模型仅依据上方已搜集素材整理生成; 若素材中有获取失败的记录, 请以报告中的"
            "「资料获取情况」说明为准, 不要把缺失内容当作事实。")

    # ---------------- 长期记忆(可选模块, MVP 先注释) ----------------
    # 主流程跑通后, 在这里接入 ChromaDB 记忆, 例如:
    #   from memory.vector_memory import MemoryStore
    #   store = MemoryStore()
    #   store.save_run(last_run["query"], ["素材1", ...], last_run["report"])   # 保存本次成果
    #   hits = store.search("新调研主题")                                          # 新任务复用时检索