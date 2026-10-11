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

echo "==> Устанавливаю Python..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

echo "==> Устанавливаю библиотеки бота..."
python3 -m venv venv
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
    echo "==> Настраиваю HTTPS для $DOMAIN (для уведомлений ЮKassa)..."
    apt-get install -y -qq caddy >/dev/null
    printf '%s {\n\treverse_proxy localhost:%s\n}\n' "$DOMAIN" "${PORT:-8080}" > /etc/caddy/Caddyfile
    systemctl enable -q caddy
    systemctl restart caddy
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
