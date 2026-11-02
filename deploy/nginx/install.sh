#!/usr/bin/env bash
# Устанавливает конфиг Nginx для fastapi-app на новой VM.
# Использование: sudo deploy/nginx/install.sh [UPSTREAM_PORT] [PREVIEW_CACHE_DIR] [CACHE_OWNER]
#   UPSTREAM_PORT     порт Uvicorn (по умолчанию 8000)
#   PREVIEW_CACHE_DIR каталог кэша превью (по умолчанию /var/cache/fastapi-previews),
#                     должен совпадать с IMAGE_MAGIC_PREVIEW_CACHE_DIR сервиса
#   CACHE_OWNER       владелец каталога кэша (по умолчанию пользователь, запустивший sudo)
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустите через sudo." >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8000}"
CACHE_DIR="${2:-/var/cache/fastapi-previews}"
OWNER="${3:-${SUDO_USER:-root}}"
SITE=/etc/nginx/sites-available/fastapi-app

if ! command -v nginx >/dev/null; then
    apt-get update
    apt-get install -y nginx
fi

install -d -o "$OWNER" -g "$OWNER" "$CACHE_DIR"

# Подставляем порт и каталог кэша в шаблон.
sed -e "s#127\.0\.0\.1:8000#127.0.0.1:${PORT}#" \
    -e "s#/var/cache/fastapi-previews/#${CACHE_DIR%/}/#" \
    "$HERE/fastapi-app.conf" > "$SITE"

ln -sfn "$SITE" /etc/nginx/sites-enabled/fastapi-app
# default_server в нашем конфиге конфликтует со стандартным сайтом.
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl enable nginx
systemctl reload nginx || systemctl restart nginx

echo "Nginx настроен: :80 -> 127.0.0.1:${PORT}, кэш превью: ${CACHE_DIR}"
echo "Проверка: curl -I http://127.0.0.1/login"
