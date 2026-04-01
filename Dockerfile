# 基于官方 Python 3.12 镜像
FROM python:3.12-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装常用数据库客户端和系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        default-mysql-client \
        sqlite3 \
        bash \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制全部项目文件到容器中
COPY . /app

# 安装Python依赖
RUN pip install --no-cache-dir --upgrade pip && \
    if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

# 复制一份db目录用于初始化（模板，只做首次复制用）
RUN cp -r /app/db /app/db-init

# 复制启动脚本
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# 数据持久化目录（如db下存放sqlite、上传文件等），方便本地挂载备份
VOLUME ["/app/db"]

# 启动服务（用入口脚本，初始化数据后再启动主程序）
CMD ["/app/docker-entrypoint.sh"]
