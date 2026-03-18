#!/bin/bash
# ═══════════════════════════════════════════════════════
#  Sports Alerts Bot — Установка на VPS (Ubuntu 22/24)
#  Запуск: bash setup_vps.sh
# ═══════════════════════════════════════════════════════

set -e

echo "═══════════════════════════════════════════"
echo "  Sports Alerts Bot — Установка на VPS"
echo "═══════════════════════════════════════════"
echo ""

BOT_DIR="/opt/sports-alerts-bot"
BOT_USER="botuser"

# 1. Обновляем систему
echo "[1/7] Обновляю систему..."
apt update -qq && apt upgrade -y -qq

# 2. Ставим Python и Git
echo "[2/7] Устанавливаю Python, Git, pip..."
apt install -y -qq python3 python3-pip python3-venv git curl

# 3. Создаём пользователя для бота (безопасность)
echo "[3/7] Создаю пользователя $BOT_USER..."
if ! id "$BOT_USER" &>/dev/null; then
    useradd -r -m -s /bin/bash "$BOT_USER"
fi

# 4. Клонируем/обновляем репо
echo "[4/7] Загружаю бота..."
if [ -d "$BOT_DIR" ]; then
    cd "$BOT_DIR"
    git pull origin main 2>/dev/null || true
else
    git clone https://github.com/kriSasaki/football-alerts-bot.git "$BOT_DIR"
fi
cd "$BOT_DIR"
chown -R "$BOT_USER":"$BOT_USER" "$BOT_DIR"

# 5. Виртуальное окружение и зависимости
echo "[5/7] Устанавливаю зависимости..."
sudo -u "$BOT_USER" python3 -m venv "$BOT_DIR/venv"
sudo -u "$BOT_USER" "$BOT_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$BOT_USER" "$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q

# 6. Настраиваем .env если его нет
if [ ! -f "$BOT_DIR/.env" ]; then
    echo ""
    echo "═══════════════════════════════════════════"
    echo "  Настройка .env"
    echo "═══════════════════════════════════════════"
    read -p "Telegram Bot Token: " BOT_TOKEN
    read -p "Admin User ID (твой Telegram ID): " ADMIN_ID

    cat > "$BOT_DIR/.env" << EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
ADMIN_USERS=$ADMIN_ID
AUTHORIZED_USERS=$ADMIN_ID
POLL_INTERVAL=120
SOFASCORE_MIN_INTERVAL=1.5
LIVE_CACHE_TTL=60
SCHEDULE_CACHE_TTL=300
MATCH_START_TOLERANCE=3600
DATABASE_PATH=alerts.db
EOF
    chown "$BOT_USER":"$BOT_USER" "$BOT_DIR/.env"
    echo "  .env создан!"
fi

# 7. Создаём systemd-сервис (автозапуск)
echo "[6/7] Создаю systemd-сервис..."
cat > /etc/systemd/system/sports-alerts-bot.service << EOF
[Unit]
Description=Sports Alerts Telegram Bot
After=network.target

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Безопасность
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$BOT_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sports-alerts-bot
systemctl start sports-alerts-bot

echo "[7/7] Проверяю статус..."
sleep 3
systemctl status sports-alerts-bot --no-pager -l

echo ""
echo "═══════════════════════════════════════════"
echo "  Готово! Бот запущен как сервис."
echo ""
echo "  Управление:"
echo "    systemctl start sports-alerts-bot"
echo "    systemctl stop sports-alerts-bot"
echo "    systemctl restart sports-alerts-bot"
echo "    systemctl status sports-alerts-bot"
echo ""
echo "  Логи:"
echo "    journalctl -u sports-alerts-bot -f"
echo "    tail -f $BOT_DIR/logs/bot.log"
echo "    tail -f $BOT_DIR/logs/errors.log"
echo ""
echo "  Обновление:"
echo "    cd $BOT_DIR && git pull && systemctl restart sports-alerts-bot"
echo ""
echo "═══════════════════════════════════════════"
