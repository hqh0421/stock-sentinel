FROM python:3.11-slim

WORKDIR /app

# 构建期：清空 Docker Desktop 可能注入的失效代理(http.docker.internal:3128)，
# 构建 VM 可直连 PyPI/镜像源，故置空走直连，避免 pip/curl 卡死。
ENV HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" NO_PROXY="" no_proxy=""

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Copy dependency file first for better layer caching
COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

# 星耀数智(AmazingData) 私有 SDK：随仓库 wheels/ 提供，构建时手动安装（主行情源依赖）
COPY wheels/ /tmp/wheels/
RUN uv pip install --system /tmp/wheels/*.whl 2>/dev/null || pip install /tmp/wheels/*.whl; rm -rf /tmp/wheels

# Copy application code
COPY app/ app/
COPY frontend/ frontend/
COPY config/ config/

# Create logs directory
RUN mkdir -p logs

EXPOSE 8888

# Disable proxy by default, can be overridden with USE_PROXY=1
ENV USE_PROXY=0
ENV PYTHONUNBUFFERED=1
# 清空构建期代理，确保运行时镜像不带残留代理（服务器由 compose 注入真实出网代理）
ENV HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" NO_PROXY="" no_proxy=""

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888"]
