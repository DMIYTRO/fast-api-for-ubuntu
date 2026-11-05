#!/usr/bin/env bash
# Включает callas для fastapi-app и перезапускает службу. Запускать: sudo scripts/enable_callas.sh [merge|full]
#   merge (по умолчанию) — только склейка PDF; full — ещё render и JPEG-превью.
# Токен читается из /mnt/shared/callas-image-magic.token и пишется в /etc/default/fastapi-app-callas (600), не выводится.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo." >&2; exit 1; }
MODE="${1:-merge}"
TOKEN_SRC=/mnt/shared/callas-image-magic.token
ENV_FILE=/etc/default/fastapi-app-callas
DROPIN=/etc/systemd/system/fastapi-app.service.d/callas.conf
[ -r "$TOKEN_SRC" ] || { echo "Нет токена: $TOKEN_SRC (шара смонтирована?)" >&2; exit 1; }
umask 077
{
  echo "IMAGE_MAGIC_CALLAS_URL=http://192.168.88.109:8765"
  echo "IMAGE_MAGIC_CALLAS_TOKEN=$(tr -d '\r\n' < "$TOKEN_SRC")"
  echo "IMAGE_MAGIC_CALLAS_MERGE_ENABLED=1"
  if [ "$MODE" = full ]; then
    echo "IMAGE_MAGIC_CALLAS_RENDER_ENABLED=1"
    echo "IMAGE_MAGIC_PREVIEW_FORMAT=jpeg"
  fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
printf '[Service]\nEnvironmentFile=%s\n' "$ENV_FILE" > "$DROPIN"
systemctl daemon-reload
systemctl restart fastapi-app
sleep 3
systemctl show fastapi-app -p MainPID -p ExecMainStartTimestamp -p ActiveState
