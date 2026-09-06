# ==================================================================================
# research_agent —— Docker 镜像(单进程本地/内网演示用)
#
# 安全与部署边界(与 README 顶部声明一致):
#   - 本项目为【单用户本地原型】, 代码沙盒不是生产级沙箱; 此镜像只应部署在
#     可信内网/本机, 禁止暴露到公网或多租户使用;
#   - 镜像【不打包 .env / API Key】: 运行时通过环境变量注入(见下方 docker run 示例),
#     构建层与运行层均不落盘密钥;
#   - 数据目录 temp_upload/ logs/ report_history.json 通过 volume 持久化(可选)。
#
# 构建:  docker build -t research-agent:1.4.0 .
# 运行(示例, 密钥经环境变量注入):
#   docker run -d --name research-agent -p 8501:8501 \
#     -e OPENAI_API_KEY=sk-xxx \
#     -e OPENAI_BASE_URL=https://api.deepseek.com/v1 \
#     -e LLM_MODEL=deepseek-chat \
#     -e BOCHA_API_KEY=sk-xxx \
#     research-agent:1.4.0
#   或使用 docker run --env-file .env 方式注入(注意 .env 不要提交到仓库/镜像)。
# ==================================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# ① 先装锁定依赖(利用镜像层缓存: 依赖不变时不重复安装)
COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt

# ② 再拷业务代码(提示词目录必须完整, 见 README §11.2)
COPY main.py graph_builder.py state_schema.py logging_setup.py ./
COPY core/ ./core/
COPY tools/ ./tools/
COPY prompts/ ./prompts/

# ③ 运行时目录(上传文件/图表/日志/历史), 可按需挂载 volume
RUN mkdir -p /app/temp_upload /app/logs

# 非 root 运行(降低容器内权限风险)
RUN useradd -m -u 1000 agent && chown -R agent:agent /app
USER agent

EXPOSE 8501

# headless + 0.0.0.0(容器内必须监听所有网卡, 端口由 -p 映射到宿主)
CMD ["streamlit", "run", "main.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
