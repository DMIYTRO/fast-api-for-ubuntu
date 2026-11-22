#!/usr/bin/env bash
# Автоматическое монтирование исходной шары «Получить заказ»: \\10.20.2.20\a → /mnt/work20.
#
# Запуск (один раз, нужен root):
#   sudo bash scripts/setup_work20_mount.sh                       # спросит логин/пароль
#   sudo WORK20_USER=admin WORK20_PASSWORD='…' bash scripts/setup_work20_mount.sh
#
# Что делает (повторный запуск безопасен):
#   1. пишет учётку в /etc/cifs-work20.cred (только root, 600);
#   2. пробной монтировкой подбирает рабочую версию SMB (3.1.1 → 3.0 → 2.1 → по умолчанию);
#   3. добавляет/заменяет строку в /etc/fstab (резервная копия /etc/fstab.bak-work20-<время>)
#      с x-systemd.automount — шара монтируется сама при обращении и после перезагрузки;
#   4. запускает automount и проверяет, что папки ORDER видны.
# cache=strict,actimeo=1 обязательны: с cache=loose проверка «файл ещё записывается»
# видит устаревшие размеры (см. CLAUDE.md, «Order fetch»).
set -euo pipefail

SHARE="${WORK20_SHARE:-//10.20.2.20/a}"
MOUNT_POINT="${WORK20_MOUNT_POINT:-/mnt/work20}"
CRED_FILE="${WORK20_CRED_FILE:-/etc/cifs-work20.cred}"
OWNER_UID="${WORK20_UID:-1000}"
OWNER_GID="${WORK20_GID:-1000}"
CHECK_DIR="WORK/digital/ORDER/Auto"

if [[ $EUID -ne 0 ]]; then
    echo "Запустите через sudo: sudo bash $0" >&2
    exit 1
fi
command -v mount.cifs >/dev/null || { echo "Нет mount.cifs: apt install cifs-utils" >&2; exit 1; }

user="${WORK20_USER:-}"
password="${WORK20_PASSWORD:-}"
if [[ -z "$user" || -z "$password" ]] && [[ -f "$CRED_FILE" ]]; then
    echo "Учётка берётся из существующего $CRED_FILE"
else
    [[ -n "$user" ]] || read -rp "Логин для $SHARE: " user
    [[ -n "$password" ]] || { read -rsp "Пароль: " password; echo; }
    install -m 600 -o root -g root /dev/null "$CRED_FILE"
    printf 'username=%s\npassword=%s\n' "$user" "$password" >"$CRED_FILE"
    [[ -n "${WORK20_DOMAIN:-}" ]] && printf 'domain=%s\n' "$WORK20_DOMAIN" >>"$CRED_FILE"
    echo "Учётка записана в $CRED_FILE"
fi

base_opts="credentials=$CRED_FILE,uid=$OWNER_UID,gid=$OWNER_GID,noperm,cache=strict,actimeo=1,soft,rsize=1048576,wsize=1048576,noatime"

# Снять текущее монтирование/automount, чтобы проба шла начисто.
systemctl stop mnt-work20.automount mnt-work20.mount 2>/dev/null || true
umount "$MOUNT_POINT" 2>/dev/null || true
mkdir -p "$MOUNT_POINT"

probe="$(mktemp -d /tmp/work20-probe.XXXXXX)"
trap 'umount "$probe" 2>/dev/null || true; rmdir "$probe" 2>/dev/null || true' EXIT
chosen=""
for vers in 3.1.1 3.0 2.1 ""; do
    opts="$base_opts${vers:+,vers=$vers}"
    if mount -t cifs "$SHARE" "$probe" -o "$opts" 2>/tmp/work20-probe.err; then
        if [[ -d "$probe/$CHECK_DIR" ]]; then
            chosen="${vers:-default}"
            umount "$probe"
            break
        fi
        echo "Смонтировалось (vers=${vers:-default}), но нет папки $CHECK_DIR" >&2
        umount "$probe"
    else
        echo "vers=${vers:-default}: $(tr '\n' ' ' </tmp/work20-probe.err)" >&2
    fi
done
rm -f /tmp/work20-probe.err
if [[ -z "$chosen" ]]; then
    echo "Не удалось смонтировать $SHARE ни с одной версией SMB. Проверьте логин/пароль" >&2
    echo "(нужен ли domain=… — задайте WORK20_DOMAIN) и доступность 10.20.2.20:445." >&2
    exit 2
fi
echo "Рабочая версия SMB: $chosen"

fstab_opts="$base_opts"
[[ "$chosen" != "default" ]] && fstab_opts+=",vers=$chosen"
fstab_opts+=",_netdev,x-systemd.automount,x-systemd.mount-timeout=15,x-systemd.idle-timeout=0"
line="$SHARE $MOUNT_POINT cifs $fstab_opts 0 0"

cp -a /etc/fstab "/etc/fstab.bak-work20-$(date +%Y%m%d-%H%M%S)"
# Убрать прежнюю строку для этой точки монтирования и добавить актуальную.
awk -v mp="$MOUNT_POINT" '!($2 == mp && $1 !~ /^#/)' /etc/fstab >/etc/fstab.work20.tmp
echo "$line" >>/etc/fstab.work20.tmp
cat /etc/fstab.work20.tmp >/etc/fstab && rm -f /etc/fstab.work20.tmp
findmnt --verify --tab-file /etc/fstab >/dev/null 2>&1 || echo "Внимание: findmnt --verify сообщает о замечаниях в /etc/fstab" >&2
echo "Строка в /etc/fstab: $line"

systemctl daemon-reload
systemctl restart mnt-work20.automount
count="$(ls -1 "$MOUNT_POINT/$CHECK_DIR" | wc -l)"
echo "Готово: $SHARE смонтирована в $MOUNT_POINT (automount), в $CHECK_DIR файлов: $count"
findmnt "$MOUNT_POINT" -o TARGET,SOURCE,FSTYPE,OPTIONS | sed 's/password=[^,]*/password=***/'
