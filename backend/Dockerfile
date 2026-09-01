# Backend Dockerfile — Platform Control Layer (FastAPI)
FROM python:3.12-slim

WORKDIR /app

# bsdtar (libarchive) is the extraction backend rarfile uses for .rar archives.
# apt 源切清华镜像：与下方 pip 的 tsinghua 镜像一致，构建不依赖外网代理。
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libarchive-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (tsinghua mirror for reliability)
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# Copy application code
COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
