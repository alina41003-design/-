#!/bin/bash
# Установка и запуск бота без Docker (Ubuntu/Debian). Запуск: bash install.sh
# Повторный запуск безопасен: обновит зависимости и перезапустит бота.
set -e
cd "$(dirname "$(readlink -f "$0")")"
DIR="$(pwd)"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Создан файл настроек .env. Заполните его (nano .env) и снова запустите: bash install.sh"
    exit 1
fi
sed -i 's/\r$//' .env   # убрать невидимые символы Windows

# Какой установщик программ есть на этом сервере
if command -v apt-get >/dev/null; then PKG="apt-get"
elif command -v dnf >/dev/null; then PKG="dnf"
elif command -v yum >/dev/null; then PKG="yum"
else
    echo "❌ Не знаю, как ставить программы на эту систему. Пришлите вывод команды: cat /etc/os-release"
    exit 1
fi
pkg_install() {
    if [ "$PKG" = "apt-get" ]; then apt-get install -y -qq "$@" >/dev/null
    else $PKG install -y -q "$@" >/dev/null; fi
}

echo "==> Система: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-неизвестно}"), установщик: $PKG"
echo "==> Устанавливаю Python..."
if [ "$PKG" = "apt-get" ]; then
    apt-get update -qq
    pkg_install python3 python3-venv python3-pip
else
    pkg_install python3 || true
    # Боту нужен Python 3.10 или новее; на CentOS/Alma/Rocky ставим свежий отдельно
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null ||
    for v in 3.12 3.11; do
        command -v python$v >/dev/null && break
        pkg_install python$v python$v-pip 2>/dev/null && break || true
    done
fi

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v $cand >/dev/null && $cand -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        PY="$(command -v $cand)"; break
    fi
done
if [ -z "$PY" ]; then
    echo "❌ Нужен Python 3.10 или новее, а установить его не удалось. Пришлите вывод: cat /etc/os-release"
    exit 1
fi
echo "==> Python: $($PY --version)"

echo "==> Устанавливаю библиотеки бота..."
rm -rf venv
$PY -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

echo "==> Настраиваю автозапуск..."
cat > /etc/systemd/system/guide-bot.service <<SERVICE
[Unit]
Description=Telegram guide bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable -q guide-bot
systemctl restart guide-bot

DOMAIN="$(grep -E '^DOMAIN=' .env | cut -d= -f2- | tr -d ' "')"
PORT="$(grep -E '^WEBHOOK_PORT=' .env | cut -d= -f2- | tr -d ' "')"
if [ -n "$DOMAIN" ]; then
    mkdir -p /etc/caddy
    echo "==> Настраиваю HTTPS для $DOMAIN (для уведомлений ЮKassa)..."
    if ! command -v caddy >/dev/null; then
        if [ "$PKG" != "apt-get" ]; then
            pkg_install 'dnf-command(copr)' 2>/dev/null || true
            dnf copr enable -y @caddy/caddy >/dev/null 2>&1 || true
        fi
        pkg_install caddy || echo "⚠️ Не удалось установить Caddy (HTTPS). Бот всё равно будет проверять оплаты сам каждые 30 секунд."
    fi
    if command -v firewall-cmd >/dev/null && systemctl is-active -q firewalld; then
        firewall-cmd -q --permanent --add-service=http --add-service=https && firewall-cmd -q --reload
    fi
    printf '%s {\n\treverse_proxy localhost:%s\n}\n' "$DOMAIN" "${PORT:-8080}" > /etc/caddy/Caddyfile
    if command -v caddy >/dev/null; then
        systemctl enable -q caddy
        systemctl restart caddy
    fi
fi

sleep 5
echo
echo "==> Последние записи журнала бота:"
journalctl -u guide-bot -n 15 --no-pager -o cat
echo
if systemctl is-active -q guide-bot; then
    echo "✅ Бот работает и будет запускаться сам после перезагрузки сервера."
else
    echo "❌ Бот не запустился. Пришлите скриншот журнала выше."
fi
