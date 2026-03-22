#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="mail_api"
PROJECT_DIR="/opt/mail_api"
VENV_DIR="${PROJECT_DIR}/venv"
APP_SOURCE="./app.py"
APP_DEST="${PROJECT_DIR}/app.py"

SERVICE_WEB="mail-api-web.service"
SERVICE_SMTP="mail-api-smtp.service"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}>>> $1${NC}"; }
ok()   { echo -e "${GREEN}>>> $1${NC}"; }
warn() { echo -e "${YELLOW}>>> $1${NC}"; }
err()  { echo -e "${RED}>>> $1${NC}"; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "请使用 root 运行：sudo ./install.sh"
        exit 1
    fi
}

check_source_file() {
    if [ ! -f "${APP_SOURCE}" ]; then
        err "当前目录未找到 app.py"
        echo "请把 install.sh 和 app.py 放在同一个目录后再执行。"
        exit 1
    fi
}

wait_for_apt() {
    log "检查 APT 是否被占用..."
    local waited=0
    while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || \
          fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/cache/apt/archives/lock >/dev/null 2>&1; do
        warn "APT 正被其他进程占用，等待中..."
        sleep 3
        waited=$((waited + 3))
        if [ "$waited" -ge 180 ]; then
            err "等待 APT 超时，请稍后重试。"
            exit 1
        fi
    done
}

install_system_packages() {
    wait_for_apt
    log "安装系统依赖..."
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        curl \
        ufw \
        sqlite3
    ok "系统依赖安装完成。"
}

get_public_ip() {
    local ip=""
    ip=$(curl -4 -s --max-time 8 https://icanhazip.com || true)
    ip=$(echo "${ip}" | tr -d '[:space:]')

    if [ -z "${ip}" ]; then
        warn "无法自动获取公网 IP。"
        read -rp "请输入服务器公网 IP: " ip
    fi

    if [ -z "${ip}" ]; then
        err "公网 IP 不能为空。"
        exit 1
    fi

    echo "${ip}"
}

backup_existing_install() {
    if [ -d "${PROJECT_DIR}" ]; then
        warn "检测到已存在安装目录：${PROJECT_DIR}"
        local backup_dir="/opt/mail_api_backup_$(date +%Y%m%d_%H%M%S)"
        mkdir -p "${backup_dir}"

        if [ -f "${PROJECT_DIR}/emails.db" ]; then
            cp -f "${PROJECT_DIR}/emails.db" "${backup_dir}/emails.db"
            ok "已备份数据库到 ${backup_dir}/emails.db"
        fi

        if [ -f "${PROJECT_DIR}/app.py" ]; then
            cp -f "${PROJECT_DIR}/app.py" "${backup_dir}/app.py"
        fi

        if [ -f "${PROJECT_DIR}/last_cleanup.txt" ]; then
            cp -f "${PROJECT_DIR}/last_cleanup.txt" "${backup_dir}/last_cleanup.txt"
        fi

        ok "已备份旧文件到 ${backup_dir}"
    fi
}

create_project_dir() {
    mkdir -p "${PROJECT_DIR}"
}

setup_venv() {
    log "创建 Python 虚拟环境..."
    python3 -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
    ok "虚拟环境已创建。"
}

write_requirements() {
    cat > "${PROJECT_DIR}/requirements.txt" <<'EOF'
flask
gunicorn
aiosmtpd
werkzeug
markupsafe
backports.zoneinfo; python_version < "3.9"
EOF
}

install_python_packages() {
    log "安装 Python 依赖..."
    "${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
    ok "Python 依赖安装完成。"
}

copy_app() {
    log "复制 app.py 到安装目录..."
    cp -f "${APP_SOURCE}" "${APP_DEST}"
    ok "app.py 已复制到 ${APP_DEST}"
}

generate_secret_key() {
    "${VENV_DIR}/bin/python" - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
}

generate_password_hash() {
    local plain_password="$1"
    ADMIN_PASSWORD_RAW="${plain_password}" "${VENV_DIR}/bin/python" - <<'PY'
import os
from werkzeug.security import generate_password_hash
print(generate_password_hash(os.environ["ADMIN_PASSWORD_RAW"]))
PY
}

patch_app_config() {
    log "写入配置到 app.py..."

    export APP_DEST
    export ADMIN_USERNAME_VALUE
    export ADMIN_PASSWORD_HASH_VALUE
    export SYSTEM_TITLE_VALUE
    export SERVER_PUBLIC_IP_VALUE
    export SECRET_KEY_VALUE
    export SMTP_PASSWORD_VALUE
    export DEFAULT_SENDER_VALUE

    "${VENV_DIR}/bin/python" - <<'PY'
import os
import re

app_path = os.environ["APP_DEST"]

with open(app_path, "r", encoding="utf-8") as f:
    content = f.read()

def replace_line(pattern, replacement, text):
    new_text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count == 0:
        raise RuntimeError(f"未找到要替换的配置项: {pattern}")
    return new_text

content = replace_line(
    r'^ADMIN_USERNAME\s*=\s*.*$',
    f'ADMIN_USERNAME = "{os.environ["ADMIN_USERNAME_VALUE"]}"',
    content
)

content = replace_line(
    r'^ADMIN_PASSWORD_HASH\s*=\s*.*$',
    f'ADMIN_PASSWORD_HASH = "{os.environ["ADMIN_PASSWORD_HASH_VALUE"]}"',
    content
)

content = replace_line(
    r'^SYSTEM_TITLE\s*=\s*.*$',
    f'SYSTEM_TITLE = "{os.environ["SYSTEM_TITLE_VALUE"]}"',
    content
)

content = replace_line(
    r'^SERVER_PUBLIC_IP\s*=\s*.*$',
    f'SERVER_PUBLIC_IP = "{os.environ["SERVER_PUBLIC_IP_VALUE"]}"',
    content
)

content = replace_line(
    r'^SMTP_PASSWORD\s*=\s*.*$',
    f'SMTP_PASSWORD = "{os.environ["SMTP_PASSWORD_VALUE"]}"',
    content
)

content = replace_line(
    r'^DEFAULT_SENDER\s*=\s*.*$',
    f'DEFAULT_SENDER = "{os.environ["DEFAULT_SENDER_VALUE"]}"',
    content
)

content = replace_line(
    r"^app\.config\['SECRET_KEY'\]\s*=\s*.*$",
    f'app.config[\'SECRET_KEY\'] = "{os.environ["SECRET_KEY_VALUE"]}"',
    content
)

with open(app_path, "w", encoding="utf-8") as f:
    f.write(content)
PY

    ok "app.py 配置写入完成。"
}

write_web_runner() {
    cat > "${PROJECT_DIR}/web_runner.py" <<EOF
from app import app, init_db

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=${WEB_PORT})
EOF
}

write_smtp_runner() {
    cat > "${PROJECT_DIR}/smtp_runner.py" <<EOF
import asyncio
from aiosmtpd.controller import Controller
from app import app, init_db, CustomSMTPHandler

init_db()
controller = Controller(CustomSMTPHandler(), hostname="0.0.0.0", port=${SMTP_PORT})

if __name__ == "__main__":
    controller.start()
    app.logger.info("SMTP 服务器启动，监听端口 ${SMTP_PORT}...")
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        app.logger.info("SMTP 服务器已关闭。")
EOF
}

write_systemd_services() {
    log "创建 systemd 服务..."

    cat > "/etc/systemd/system/${SERVICE_WEB}" <<EOF
[Unit]
Description=Mail API Web Service
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/gunicorn -w 2 -b 0.0.0.0:${WEB_PORT} app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    cat > "/etc/systemd/system/${SERVICE_SMTP}" <<EOF
[Unit]
Description=Mail API SMTP Service
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/smtp_runner.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    ok "systemd 服务文件已创建。"
}

reload_and_start_services() {
    log "重载并启动服务..."
    systemctl daemon-reload
    systemctl enable "${SERVICE_WEB}"
    systemctl enable "${SERVICE_SMTP}"
    systemctl restart "${SERVICE_WEB}"
    systemctl restart "${SERVICE_SMTP}"
    ok "服务已启动并设置开机自启。"
}

configure_firewall() {
    log "配置 UFW 防火墙..."
    ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true
    ufw allow "${SMTP_PORT}/tcp" >/dev/null 2>&1 || true
    ok "防火墙规则已放行端口 ${WEB_PORT} 和 ${SMTP_PORT}。"
}

show_summary() {
    echo
    echo "========================================"
    ok "安装完成"
    echo "安装目录: ${PROJECT_DIR}"
    echo "Web 端口: ${WEB_PORT}"
    echo "SMTP 端口: ${SMTP_PORT}"
    echo "公网 IP : ${SERVER_PUBLIC_IP_VALUE}"
    echo
    echo "后台访问地址:"
    echo "  http://${SERVER_PUBLIC_IP_VALUE}:${WEB_PORT}/login"
    echo
    echo "常用命令:"
    echo "  systemctl status ${SERVICE_WEB}"
    echo "  systemctl status ${SERVICE_SMTP}"
    echo "  journalctl -u ${SERVICE_WEB} -f"
    echo "  journalctl -u ${SERVICE_SMTP} -f"
    echo
    echo "数据库位置:"
    echo "  ${PROJECT_DIR}/emails.db"
    echo "========================================"
}

uninstall_server() {
    warn "即将卸载邮件服务。"
    read -rp "请输入 yes 确认卸载: " confirm
    if [ "${confirm}" != "yes" ]; then
        echo "已取消。"
        exit 0
    fi

    local backup_dir="/root/mail_api_uninstall_backup_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "${backup_dir}"

    if [ -f "${PROJECT_DIR}/emails.db" ]; then
        cp -f "${PROJECT_DIR}/emails.db" "${backup_dir}/emails.db"
        ok "数据库已备份到 ${backup_dir}/emails.db"
    fi

    systemctl stop "${SERVICE_WEB}" 2>/dev/null || true
    systemctl stop "${SERVICE_SMTP}" 2>/dev/null || true
    systemctl disable "${SERVICE_WEB}" 2>/dev/null || true
    systemctl disable "${SERVICE_SMTP}" 2>/dev/null || true

    rm -f "/etc/systemd/system/${SERVICE_WEB}"
    rm -f "/etc/systemd/system/${SERVICE_SMTP}"
    systemctl daemon-reload

    rm -rf "${PROJECT_DIR}"

    ok "卸载完成。"
    echo "如需恢复数据库，请查看备份目录：${backup_dir}"
    exit 0
}

main_menu() {
    echo "========================================"
    echo " 小龙女她爸邮局服务系统 - 新版安装脚本"
    echo "========================================"
    echo "1) 安装 / 重装"
    echo "2) 卸载"
    echo "========================================"
    read -rp "请选择 [1-2]: " action

    case "${action}" in
        1) ;;
        2) uninstall_server ;;
        *) err "无效选择"; exit 1 ;;
    esac
}

collect_inputs() {
    read -rp "请输入 Web 端口 [默认 2099]: " WEB_PORT
    WEB_PORT="${WEB_PORT:-2099}"

    read -rp "请输入 SMTP 端口 [默认 25]: " SMTP_PORT
    SMTP_PORT="${SMTP_PORT:-25}"

    read -rp "请输入管理员用户名 [默认 admin]: " ADMIN_USERNAME_VALUE
    ADMIN_USERNAME_VALUE="${ADMIN_USERNAME_VALUE:-admin}"

    while true; do
        read -rsp "请输入管理员密码: " ADMIN_PASSWORD_PLAIN
        echo
        read -rsp "请再次输入管理员密码: " ADMIN_PASSWORD_PLAIN2
        echo
        if [ -z "${ADMIN_PASSWORD_PLAIN}" ]; then
            err "管理员密码不能为空。"
            continue
        fi
        if [ "${ADMIN_PASSWORD_PLAIN}" != "${ADMIN_PASSWORD_PLAIN2}" ]; then
            err "两次密码输入不一致，请重新输入。"
            continue
        fi
        break
    done

    read -rp "请输入系统标题 [默认 小龙女她爸邮局服务系统]: " SYSTEM_TITLE_VALUE
    SYSTEM_TITLE_VALUE="${SYSTEM_TITLE_VALUE:-小龙女她爸邮局服务系统}"

    SERVER_PUBLIC_IP_VALUE="$(get_public_ip)"
    read -rp "请输入服务器公网 IP [默认 ${SERVER_PUBLIC_IP_VALUE}]: " SERVER_PUBLIC_IP_INPUT
    SERVER_PUBLIC_IP_VALUE="${SERVER_PUBLIC_IP_INPUT:-${SERVER_PUBLIC_IP_VALUE}}"

    read -rp "请输入 SendGrid API Key（留空则不配置发信功能）: " SMTP_PASSWORD_VALUE
    SMTP_PASSWORD_VALUE="${SMTP_PASSWORD_VALUE:-}"

    read -rp "请输入默认发件邮箱（留空则不配置发信功能）: " DEFAULT_SENDER_VALUE
    DEFAULT_SENDER_VALUE="${DEFAULT_SENDER_VALUE:-}"

    SECRET_KEY_VALUE="$(generate_secret_key)"
    ADMIN_PASSWORD_HASH_VALUE="$(generate_password_hash "${ADMIN_PASSWORD_PLAIN}")"
}

install_flow() {
    backup_existing_install
    create_project_dir
    install_system_packages
    setup_venv
    write_requirements
    install_python_packages
    copy_app
    patch_app_config
    write_web_runner
    write_smtp_runner
    write_systemd_services
    reload_and_start_services
    configure_firewall
    show_summary
}

require_root
check_source_file
main_menu
collect_inputs
install_flow
