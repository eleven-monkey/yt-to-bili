FROM python:3.10-slim

# 更新源并安装系统依赖
# ffmpeg: 视频处理必须
# git: 部分依赖可能需要
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt packages.txt ./

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 暴露端口 (Streamlit 默认端口)
EXPOSE 8501

# 启动命令
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]