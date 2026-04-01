#!/bin/bash

# 邮件查看系统一键安装脚本 v2.1 (Debian/Ubuntu)
# 支持自动检测系统，多数据库兼容，systemd优先启动
# 移除Nginx依赖，简化部署流程

set -e

echo "=========================================="
echo "    邮件查看系统一键安装脚本 v2.1        "
echo "=========================================="
echo ""

# 检查是否为root用户
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请使用root权限运行此脚本"
    echo "   sudo bash install.sh"
    exit 1
fi

# 自动检测系统版本
echo "🔍 检测系统信息..."
if command -v lsb_release &> /dev/null; then
    OS_ID=$(lsb_release -si)
    OS_VERSION=$(lsb_release -sr)
    OS_CODENAME=$(lsb_release -sc)
    echo "   操作系统: $OS_ID $OS_VERSION ($OS_CODENAME)"
elif [ -f "/etc/os-release" ]; then
    source /etc/os-release
    OS_ID="$ID"
    OS_VERSION="$VERSION_ID"
    echo "   操作系统: $PRETTY_NAME"
else
    echo "   操作系统: 未知系统"
fi

# 检查是否支持的系统
SUPPORTED=false
if [[ "$OS_ID" == "Ubuntu" ]]; then
    SUPPORTED=true
    SYSTEM_TYPE="ubuntu"
elif [[ "$OS_ID" == "Debian" ]]; then
    SUPPORTED=true
    SYSTEM_TYPE="debian"
    # 特别处理Debian 12
    if [[ "$OS_VERSION" == "12"* ]] || [[ "$OS_CODENAME" == "bookworm" ]]; then
        echo "   检测到Debian 12系统，使用适配配置"
    fi
fi

if [ "$SUPPORTED" != true ]; then
    echo "❌ 不支持的操作系统，仅支持Ubuntu和Debian"
    exit 1
fi

# 获取当前脚本目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR"

echo "   安装目录: $APP_DIR"
echo ""

# 更新系统包
echo "📦 更新系统包列表..."
apt-get update -qq

# 安装Python 3和pip
echo "🐍 安装Python 3和相关工具..."
apt-get install -y python3 python3-pip python3-venv python3-dev

# 根据系统类型安装数据库开发依赖
echo "🔧 安装系统依赖和数据库开发包..."
BASE_PACKAGES="build-essential libssl-dev libffi-dev pkg-config curl wget git supervisor"

# 数据库开发依赖 - 自动适配不同系统版本
DB_PACKAGES=""
if [[ "$SYSTEM_TYPE" == "debian" ]] && [[ "$OS_VERSION" == "12"* ]]; then
    # Debian 12 特殊处理
    DB_PACKAGES="libpq-dev default-libmysqlclient-dev"
elif [[ "$SYSTEM_TYPE" == "ubuntu" ]]; then
    # Ubuntu系统
    if dpkg --compare-versions "$OS_VERSION" ge "20.04"; then
        DB_PACKAGES="libpq-dev default-libmysqlclient-dev"
    else
        DB_PACKAGES="libpq-dev libmysqlclient-dev"
    fi
else
    # 其他Debian版本
    DB_PACKAGES="libpq-dev default-libmysqlclient-dev"
fi

echo "   安装基础包: $BASE_PACKAGES"
echo "   安装数据库包: $DB_PACKAGES"

apt-get install -y $BASE_PACKAGES $DB_PACKAGES

# 创建虚拟环境
echo "🌐 创建Python虚拟环境..."
cd "$APP_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 升级pip
echo "⬆️  升级pip..."
pip install --upgrade pip

# 安装Python依赖 - 兼容所有数据库
echo "📚 安装Python依赖包..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
else
    # 手动安装核心依赖，支持多数据库
    echo "   安装Flask核心框架..."
    pip install Flask>=3.0.0 Flask-Session>=0.7.0 werkzeug>=3.0.0
    
    echo "   安装邮件处理库..."
    pip install imapclient>=2.3.1 requests>=2.31.0 pysocks>=1.7.1 charset-normalizer>=3.3.2
    
    echo "   安装数据库驱动..."
    # SQLite (内置)
    # MySQL
    pip install mysql-connector-python>=8.2.0
    # PostgreSQL
    pip install psycopg2-binary>=2.9.9
    
    echo "   安装其他依赖..."
    pip install email-validator python-dateutil
fi

# 初始化数据库
echo "🗄️  初始化数据库..."
if [ -f "app.py" ]; then
    # 设置环境变量
    export FLASK_APP=app.py
    export DATABASE_TYPE=${DATABASE_TYPE:-sqlite}
    export PORT=${PORT:-8005}
    
    # 运行一次以初始化数据库
    timeout 10 python3 app.py &
    FLASK_PID=$!
    sleep 5
    kill $FLASK_PID 2>/dev/null || true
    wait $FLASK_PID 2>/dev/null || true
    
    echo "   ✅ 数据库初始化完成"
else
    echo "   ⚠️  未找到app.py文件"
fi

# 检测systemd支持
HAS_SYSTEMD=false
if command -v systemctl &> /dev/null && [ -d "/etc/systemd/system" ]; then
    HAS_SYSTEMD=true
    echo "🔧 检测到systemd支持，创建系统服务..."
else
    echo "🔧 未检测到systemd，将使用nohup后台运行..."
fi

if [ "$HAS_SYSTEMD" = true ]; then
    # 创建systemd服务文件
    cat > /etc/systemd/system/mail-system.service << EOF
[Unit]
Description=Mail View System
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/venv/bin
Environment=DATABASE_TYPE=sqlite
Environment=PORT=8005
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    # 重新加载systemd并启用服务
    systemctl daemon-reload
    systemctl enable mail-system
    
    echo "   ✅ systemd服务创建完成"
else
    # 创建传统启动脚本
    echo "   创建传统启动脚本..."
fi

# 创建日志目录
mkdir -p /var/log/mail-system
chown -R root:root /var/log/mail-system

# 设置文件权限
echo "🔐 设置文件权限..."
chown -R root:root "$APP_DIR"
chmod +x "$APP_DIR/app.py" 2>/dev/null || true

# 创建启动脚本
cat > "$APP_DIR/start.sh" << EOF
#!/bin/bash
cd "$APP_DIR"
source venv/bin/activate
export DATABASE_TYPE=\${DATABASE_TYPE:-sqlite}
export PORT=\${PORT:-8005}

if [ "$HAS_SYSTEMD" = true ]; then
    systemctl start mail-system
    echo "邮件查看系统已通过systemd启动"
else
    nohup python3 app.py > /var/log/mail-system/app.log 2>&1 &
    echo "邮件查看系统已后台启动，日志: /var/log/mail-system/app.log"
fi
EOF

chmod +x "$APP_DIR/start.sh"

# 创建停止脚本
cat > "$APP_DIR/stop.sh" << EOF
#!/bin/bash
if [ "$HAS_SYSTEMD" = true ]; then
    systemctl stop mail-system
    echo "邮件查看系统已停止"
else
    pkill -f "python3 app.py" || echo "未找到运行中的邮件查看系统进程"
fi
EOF

chmod +x "$APP_DIR/stop.sh"

# 创建重启脚本
cat > "$APP_DIR/restart.sh" << EOF
#!/bin/bash
if [ "$HAS_SYSTEMD" = true ]; then
    systemctl restart mail-system
    echo "邮件查看系统已重启"
else
    \$0/../stop.sh
    sleep 2
    \$0/../start.sh
fi
EOF

chmod +x "$APP_DIR/restart.sh"

# 创建状态检查脚本
cat > "$APP_DIR/status.sh" << EOF
#!/bin/bash
echo "=========================================="
echo "         邮件查看系统状态检查            "
echo "=========================================="
if [ "$HAS_SYSTEMD" = true ]; then
    echo "服务状态："
    systemctl status mail-system --no-pager -l
    echo ""
else
    echo "进程状态："
    if pgrep -f "python3 app.py" > /dev/null; then
        echo "✅ 邮件查看系统正在运行"
        ps aux | grep "python3 app.py" | grep -v grep
    else
        echo "❌ 邮件查看系统未运行"
    fi
    echo ""
fi

echo "端口占用情况："
netstat -tlnp 2>/dev/null | grep :8005 || echo "端口8005未被占用"
echo ""

echo "最近日志："
if [ -f "/var/log/mail-system/app.log" ]; then
    tail -10 /var/log/mail-system/app.log
else
    echo "暂无日志文件"
fi
EOF

chmod +x "$APP_DIR/status.sh"

# 启动服务
echo "🚀 启动服务..."
if [ "$HAS_SYSTEMD" = true ]; then
    systemctl start mail-system
    echo "   使用systemd启动服务"
else
    cd "$APP_DIR"
    source venv/bin/activate
    nohup python3 app.py > /var/log/mail-system/app.log 2>&1 &
    echo "   使用nohup后台启动服务"
fi

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 5

# 检查服务状态
echo "📊 检查服务状态..."
if [ "$HAS_SYSTEMD" = true ]; then
    if systemctl is-active --quiet mail-system; then
        echo "   ✅ 邮件查看系统服务运行正常"
    else
        echo "   ❌ 邮件查看系统服务启动失败"
        echo "   请运行以下命令查看日志："
        echo "   journalctl -u mail-system -f"
    fi
else
    if pgrep -f "python3 app.py" > /dev/null; then
        echo "   ✅ 邮件查看系统后台运行正常"
    else
        echo "   ❌ 邮件查看系统启动失败"
        echo "   请检查日志：/var/log/mail-system/app.log"
    fi
fi

# 获取IP地址
IP_ADDRESS=$(hostname -I | awk '{print $1}')

echo ""
echo "=========================================="
echo "            安装完成！                    "
echo "=========================================="
echo ""
echo "🎉 邮件查看系统安装成功！"
echo ""
echo "📍 访问地址："
echo "   本地访问: http://localhost:8005"
echo "   本地访问: http://127.0.0.1:8005"
if [ -n "$IP_ADDRESS" ]; then
echo "   网络访问: http://$IP_ADDRESS:8005"
fi
echo ""
echo "👤 默认管理员账号："
echo "   用户名: admin"
echo "   密码: admin"
echo "   ⚠️  首次登录后请立即修改密码！"
echo ""
echo "🔧 管理命令："
if [ "$HAS_SYSTEMD" = true ]; then
echo "   启动服务: systemctl start mail-system"
echo "   停止服务: systemctl stop mail-system"
echo "   重启服务: systemctl restart mail-system"
echo "   查看日志: journalctl -u mail-system -f"
else
echo "   启动服务: $APP_DIR/start.sh"
echo "   停止服务: $APP_DIR/stop.sh"
echo "   重启服务: $APP_DIR/restart.sh"
echo "   查看日志: tail -f /var/log/mail-system/app.log"
fi
echo "   查看状态: $APP_DIR/status.sh"
echo ""
echo "📁 项目目录: $APP_DIR"
echo "📁 数据库文件: $APP_DIR/db/"
echo "📁 日志目录: /var/log/mail-system"
echo ""
echo "🌐 功能特点："
echo "   ✅ 完整的邮箱管理（支持批量添加、测试连接、分页搜索）"
echo "   ✅ 统一代理池管理（HTTP/SOCKS5，支持测试和批量操作）"
echo "   ✅ 服务器地址管理（快捷选择，自动端口切换）"
echo "   ✅ 多数据库支持（SQLite/MySQL/PostgreSQL）"
echo "   ✅ 智能系统检测（systemd优先，nohup后备）"
echo "   ✅ 响应式设计（支持PC和移动端）"
echo "   ✅ 安全认证和权限管理"
echo ""
echo "📖 数据库配置说明："
echo "   默认使用SQLite数据库，无需额外配置"
echo "   如需使用MySQL，请设置环境变量："
echo "     export DATABASE_TYPE=mysql"
echo "     export MYSQL_HOST=localhost"
echo "     export MYSQL_USER=root"
echo "     export MYSQL_PASSWORD=your_password"
echo "     export MYSQL_DATABASE=mail_system"
echo ""
echo "   如需使用PostgreSQL，请设置环境变量："
echo "     export DATABASE_TYPE=postgresql"
echo "     export POSTGRES_HOST=localhost"
echo "     export POSTGRES_USER=postgres"
echo "     export POSTGRES_PASSWORD=your_password"
echo "     export POSTGRES_DATABASE=mail_system"
echo ""
echo "📖 使用说明："
echo "   1. 访问上述地址进入系统"
echo "   2. 使用默认账号登录管理后台"
echo "   3. 在邮箱管理中添加邮箱账号"
echo "   4. 配置代理池（可选）"
echo "   5. 开始使用邮件查看功能"
echo ""

# 如果有防火墙，提示开放端口
if command -v ufw &> /dev/null; then
    echo "🔥 防火墙设置提醒："
    echo "   如果启用了ufw防火墙，请运行以下命令开放端口："
    echo "   sudo ufw allow 8005"
    echo ""
fi

# 设置环境变量持久化（可选）
if [ ! -f "/etc/environment.backup" ]; then
    cp /etc/environment /etc/environment.backup 2>/dev/null || true
fi

if ! grep -q "DATABASE_TYPE" /etc/environment 2>/dev/null; then
    cat >> /etc/environment << EOF
# Mail System Environment
DATABASE_TYPE=sqlite
PORT=8005
EOF
fi

echo "安装脚本执行完成。系统已针对$OS_ID $OS_VERSION进行优化配置。"