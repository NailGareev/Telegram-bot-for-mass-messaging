#!/usr/bin/env bash
# Установка бота билетов на сервер с Debian 10 / 11 / 12 / 13.
#
#   sudo bash install.sh
#
# Скрипт ставит всё необходимое, спрашивает токен бота и Telegram ID главного
# администратора, регистрирует бота как службу systemd и запускает его.
# Запускать можно повторно — например, после обновления кода (git pull).
#
# Остальные настройки (ключ Groq) задаются уже в приложении Mini App:
# раздел «Настройки», виден только главному администратору.

set -euo pipefail

SERVICE_NAME="ticket-bot"
PYTHON_VERSION="3.12"   # ставится через uv, если системный Python старше 3.10
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
ENV_FILE="$APP_DIR/.env"
TOOLS="$APP_DIR/.tools"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"

if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
step() { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$RESET"; }
ok()   { printf '%s✓ %s%s\n' "$GREEN" "$1" "$RESET"; }
warn() { printf '%s! %s%s\n' "$YELLOW" "$1" "$RESET" >&2; }
die()  { printf '%s✗ %s%s\n' "$RED" "$1" "$RESET" >&2; exit 1; }

# ---------- проверки ----------

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -E bash "${BASH_SOURCE[0]}" "$@"
  fi
  die "Запустите скрипт от root:  su -c 'bash $0'"
fi

[ -f "$APP_DIR/requirements.txt" ] && [ -d "$APP_DIR/bot" ] \
  || die "install.sh должен лежать в папке проекта (рядом с requirements.txt и папкой bot)."

OS_ID=""; OS_VERSION=""
if [ -r /etc/os-release ]; then
  OS_ID="$(. /etc/os-release && echo "${ID:-}")"
  OS_VERSION="$(. /etc/os-release && echo "${VERSION_ID:-}")"
fi
case "$OS_ID:${OS_VERSION%%.*}" in
  debian:10|debian:11|debian:12|debian:13)
    ok "Система: Debian ${OS_VERSION%%.*}" ;;
  *)
    if [ "${ALLOW_OTHER_OS:-}" = "1" ]; then
      warn "Система ${OS_ID:-неизвестна} ${OS_VERSION} не проверялась — продолжаю, потому что задан ALLOW_OTHER_OS=1."
    else
      die "Скрипт рассчитан на Debian 10, 11, 12 и 13, а здесь: ${OS_ID:-неизвестно} ${OS_VERSION}. Чтобы всё равно попробовать: ALLOW_OTHER_OS=1 bash install.sh"
    fi ;;
esac

# Владелец папки проекта — от его имени будет работать бот.
RUN_USER="$(stat -c %U "$APP_DIR")"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="root"

# ---------- системные пакеты ----------

step "Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
if ! apt-get update -qq; then
  # У Debian 10 репозитории переехали в архив — обновление списка может не пройти.
  warn "apt-get update завершился с ошибкой. Пробую продолжить с тем, что уже установлено."
fi
apt-get install -y -qq ca-certificates curl >/dev/null 2>&1 \
  || command -v curl >/dev/null 2>&1 \
  || die "Не удалось установить curl. Проверьте репозитории apt (apt-get update)."
ok "curl и сертификаты на месте"

# ---------- Python и зависимости ----------

step "Python и библиотеки"
system_python_ok() {
  [ -x /usr/bin/python3 ] && /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
}

install_with_system_python() {
  apt-get install -y -qq python3-venv >/dev/null 2>&1 || return 1
  [ -x "$VENV/bin/python" ] || /usr/bin/python3 -m venv "$VENV" || return 1
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip || return 1
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"
}

install_with_uv() {
  # uv — установщик Python от Astral: ставит свежий Python в папку проекта,
  # не трогая системный. Нужен на Debian 10 и 11, где Python слишком старый.
  mkdir -p "$TOOLS"
  export UV_PYTHON_INSTALL_DIR="$TOOLS/python" UV_CACHE_DIR="$TOOLS/cache"
  local uv="$TOOLS/uv"
  if [ ! -x "$uv" ]; then
    if command -v uv >/dev/null 2>&1; then
      uv="$(command -v uv)"
    else
      echo "Скачиваю uv (astral.sh)..."
      curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_UNMANAGED_INSTALL="$TOOLS" sh >/dev/null 2>&1 \
        || die "Не удалось скачать uv. Проверьте доступ сервера в интернет (astral.sh, github.com)."
    fi
  fi
  if [ ! -x "$VENV/bin/python" ] \
     || ! "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    rm -rf "$VENV"
    "$uv" venv --quiet --seed --python "$PYTHON_VERSION" "$VENV" \
      || die "Не удалось установить Python $PYTHON_VERSION через uv."
  fi
  "$uv" pip install --quiet --python "$VENV/bin/python" -r "$APP_DIR/requirements.txt" \
    || die "Не удалось установить библиотеки Python."
  rm -rf "$UV_CACHE_DIR"
}

if [ "${FORCE_UV:-}" != "1" ] && system_python_ok && install_with_system_python; then
  ok "Использую системный Python: $("$VENV/bin/python" --version 2>&1)"
else
  [ "${FORCE_UV:-}" = "1" ] || system_python_ok \
    || echo "Системный Python старше 3.10 — ставлю отдельный Python $PYTHON_VERSION в папку проекта."
  install_with_uv
  ok "Установлен отдельный Python: $("$VENV/bin/python" --version 2>&1)"
fi
"$VENV/bin/python" -c 'import aiogram, cv2, qrcode, PIL' \
  || die "Библиотеки установились не полностью (см. сообщения выше)."
ok "Библиотеки установлены"

# ---------- токен и главный администратор ----------

step "Настройки бота"
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -n 1 | tr -d '\r' \
    | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
env_set() {
  # Заменяет строку KEY=... в .env или дописывает её, остальное не трогает.
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ]; then
    grep -v "^[[:space:]]*$key[[:space:]]*=" "$ENV_FILE" > "$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}
ask() {
  # Вопрос задаётся в терминал, даже если скрипт запущен через конвейер.
  local prompt="$1" answer=""
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf '%s' "$prompt" > /dev/tty
    IFS= read -r answer < /dev/tty || true
  else
    die "Нет терминала для ввода. Передайте значения так: BOT_TOKEN=... MAIN_ADMIN_IDS=... bash install.sh"
  fi
  printf '%s' "$answer" | tr -d '[:space:]'
}

TOKEN="${BOT_TOKEN:-$(env_get BOT_TOKEN)}"
ADMINS="${MAIN_ADMIN_IDS:-$(env_get MAIN_ADMIN_IDS)}"
INTERACTIVE_TOKEN=0

while :; do
  if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$'; then
    [ -z "$TOKEN" ] || warn "Это не похоже на токен бота."
    echo "Токен выдаёт @BotFather в Telegram, выглядит так: 123456789:AAH...xyz"
    TOKEN="$(ask 'Вставьте токен бота: ')"
    INTERACTIVE_TOKEN=1
    continue
  fi
  # Проверяем токен у Telegram и заодно узнаём имя бота.
  REPLY_JSON="$(curl -sS -m 20 "https://api.telegram.org/bot$TOKEN/getMe" 2>/dev/null || true)"
  if printf '%s' "$REPLY_JSON" | grep -q '"ok":true'; then
    BOT_NAME="$(printf '%s' "$REPLY_JSON" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
    ok "Токен принят: бот @$BOT_NAME"
    break
  elif printf '%s' "$REPLY_JSON" | grep -q '"ok":false'; then
    warn "Telegram не принял этот токен. Проверьте, что он скопирован целиком."
    [ "$INTERACTIVE_TOKEN" = "1" ] || [ -r /dev/tty ] || die "Неверный BOT_TOKEN."
    TOKEN=""
  else
    warn "Не удалось связаться с Telegram для проверки токена — сохраняю как есть."
    break
  fi
done

while ! printf '%s' "$ADMINS" | grep -Eq '^[0-9]{3,15}(,[0-9]{3,15})*$'; do
  [ -z "$ADMINS" ] || warn "Нужно число (или несколько чисел через запятую)."
  echo "Свой Telegram ID можно узнать у бота @userinfobot."
  ADMINS="$(ask 'Введите Telegram ID главного администратора: ')"
done
ok "Главный администратор: $ADMINS"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# Создано install.sh. Ключ Groq задаётся в приложении Mini App: «Настройки».
EOF
fi
env_set BOT_TOKEN "$TOKEN"
env_set MAIN_ADMIN_IDS "$ADMINS"
chmod 600 "$ENV_FILE"

mkdir -p "$APP_DIR/data"
chown -R "$RUN_USER": "$VENV" "$ENV_FILE" "$APP_DIR/data"
[ ! -d "$TOOLS" ] || chown -R "$RUN_USER": "$TOOLS"

# ---------- служба systemd ----------

step "Запуск"
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  warn "systemd здесь не работает, поэтому службу не создаю. Запускайте бота вручную:"
  echo "    cd $APP_DIR && .venv/bin/python -m bot"
  exit 0
fi

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Telegram-бот билетов с QR-кодами
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python -m bot
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 4
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  journalctl -u "$SERVICE_NAME" -n 30 --no-pager >&2 || true
  die "Бот не запустился — причина в сообщениях выше."
fi

ok "Бот запущен и будет стартовать сам после перезагрузки сервера"
cat <<EOF

${BOLD}Что дальше${RESET}
  1. Откройте бота в Telegram и нажмите /start — слева от поля ввода появится кнопка «Mini App».
  2. Всё управление — в приложении. Ключ Groq задаётся там же: раздел «Настройки»
     (его видит только главный администратор).

${BOLD}Управление${RESET}
  Состояние:   systemctl status $SERVICE_NAME
  Журнал:      journalctl -u $SERVICE_NAME -f
  Перезапуск:  systemctl restart $SERVICE_NAME
  Остановка:   systemctl stop $SERVICE_NAME
  Обновление:  git pull && sudo bash install.sh
EOF
