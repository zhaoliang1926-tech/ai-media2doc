FROM python:3.10-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY src/ ./src/
COPY config/ ./config/

# 临时文件目录
RUN mkdir -p /app/tmp

CMD ["python", "src/main.py"]
