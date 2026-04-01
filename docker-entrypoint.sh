#!/bin/bash

# 如本地db目录为空（无文件），复制初始内容到挂载目录
if [ -z "$(ls -A /app/db 2>/dev/null)" ]; then
    cp -r /app/db-init/* /app/db/
    echo "已初始化db目录内容到挂载目录。"
else
    echo "检测到db目录已有内容，跳过初始化，直接启动。"
fi

# 启动主程序
exec python app.py