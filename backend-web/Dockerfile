# Backend-Web 服务 Dockerfile
# 单阶段构建，直接使用Python源码

# 基础镜像走 docker.io 官方名（由 buildx 的 buildkitd 镜像加速配置拉取，见流水线 BUILDKIT 配置）
FROM python:3.11-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 配置国内镜像源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

RUN apt-get update && apt-get install -y curl ca-certificates gcc \
    fonts-dejavu-core fonts-liberation \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制项目源码
COPY common /app/common
COPY backend-web /app/backend-web
COPY launcher /app/launcher

# 从pyproject.toml提取并安装依赖
# 除主依赖外，一并安装 [asr] 可选依赖（faster-whisper），
# 保证服务器/Windows等Docker环境也能用本地离线语音转文字，不依赖任何API Key
RUN python -c "\
import tomllib; \
proj = tomllib.load(open('/app/backend-web/pyproject.toml','rb')).get('project',{}); \
deps = list(proj.get('dependencies', [])); \
deps += list(proj.get('optional-dependencies', {}).get('asr', [])); \
open('/tmp/requirements.txt','w').write('\n'.join(deps))" && \
    pip install --no-cache-dir -r /tmp/requirements.txt

RUN python -m playwright install --with-deps chromium
RUN python -m patchright install --with-deps chromium

# 创建日志目录与本地语音模型目录
# HF_HOME 指向 /app/data/asr_models：该目录由 compose 挂卷持久化，
# 避免容器重建后重新下载上百MB的语音模型
RUN mkdir -p /app/backend-web/logs /app/static/uploads /app/data/asr_models

# 暴露端口
EXPOSE 8089

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV TZ=Asia/Shanghai
ENV HF_HOME=/app/data/asr_models
# 国内直连 huggingface 很慢，走镜像；xet 通道不认镜像域名会 401，必须关掉
ENV HF_ENDPOINT=https://hf-mirror.com
ENV HF_HUB_DISABLE_XET=1

# 启动服务
CMD ["python", "backend-web/main.py"]
