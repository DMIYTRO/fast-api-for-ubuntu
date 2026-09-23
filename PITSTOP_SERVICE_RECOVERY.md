# Восстановление интеграции PitStop Server

Эта инструкция описывает связку production FastAPI и Windows-сервера PitStop,
а также порядок диагностики, если в карточке заказа появляется сообщение
`PitStop Server завершился с кодом ...`.

Не храните в этом файле пароли, приватные SSH-ключи и токены.

## Текущая схема

```
FastAPI (10.20.2.104, systemd: fastapi-app)
  └─ SSH по ключу, пользователь Admin
       └─ PitStop Server (192.168.88.102:22)
            ├─ PitStopServerCLI.exe
            └─ \\192.168.88.100\shared\soft\Sborka_2Core.ppp
```

FastAPI создаёт PDF и вызывает `PitStopServerCLI.exe` удалённо по SSH. PitStop
читает PDF и профиль через общие папки. Поэтому для успешной проверки должны
быть одновременно доступны сеть, SSH, ключ, CLI, профиль и общая папка с PDF.

## Где находится конфигурация

На production VM:

```text
/etc/systemd/system/fastapi-app.service
/etc/default/fastapi-app-pitstop
```

Не редактируйте unit-файл без необходимости. Параметры PitStop находятся в
`/etc/default/fastapi-app-pitstop`:

```ini
IMAGE_MAGIC_PITSTOP_ENABLED=true
IMAGE_MAGIC_PITSTOP_HOST=192.168.88.102
IMAGE_MAGIC_PITSTOP_PORT=22
IMAGE_MAGIC_PITSTOP_USERNAME=Admin
IMAGE_MAGIC_PITSTOP_IDENTITY_FILE=/home/ubuntu/.ssh/id_ed25519
IMAGE_MAGIC_PITSTOP_KNOWN_HOSTS=/home/ubuntu/.ssh/known_hosts
IMAGE_MAGIC_PITSTOP_CLI_PATH=C:/Program Files/Enfocus/Enfocus PitStop Server 23/PitStopServerCLI.exe
IMAGE_MAGIC_PITSTOP_MAC_SHARED_ROOT=/mnt/shared
IMAGE_MAGIC_PITSTOP_WINDOWS_SHARED_ROOT=//192.168.88.100/shared
IMAGE_MAGIC_PITSTOP_PROFILE_DIGITAL=//192.168.88.100/shared/soft/Sborka_2Core.ppp
```

После изменения файла всегда перезапускайте именно production unit:

```bash
sudo systemctl restart fastapi-app
systemctl show fastapi-app \
  -p ActiveState -p SubState -p MainPID -p ExecMainStartTimestamp --no-pager
```

Ожидаемый результат: `ActiveState=active`, `SubState=running`, новый `MainPID`
и новое время запуска. Не запускайте `control_panel.py` вручную на production.

## Быстрая диагностика

Выполняйте команды на VM `10.20.2.104` под пользователем `ubuntu`.

### 1. Состояние web-сервиса и журнал

```bash
systemctl show fastapi-app \
  -p ActiveState -p SubState -p MainPID -p ExecMainStatus --no-pager
journalctl -u fastapi-app -n 150 --no-pager
```

### 2. Сеть и SSH до PitStop

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=10 -o IdentitiesOnly=yes \
  -i /home/ubuntu/.ssh/id_ed25519 \
  -p 22 Admin@192.168.88.102 hostname
```

Успешный результат возвращает имя Windows-компьютера и код выхода `0`.

### 3. Доступность CLI и профиля на Windows

После успешного SSH-входа выполните на PitStop в PowerShell:

```powershell
Test-Path -LiteralPath 'C:\Program Files\Enfocus\Enfocus PitStop Server 23\PitStopServerCLI.exe'
Test-Path -LiteralPath '\\192.168.88.100\shared\soft\Sborka_2Core.ppp'
Test-Path -LiteralPath '\\192.168.88.100\shared'
```

Каждая команда должна вернуть `True`.

Для поиска доступных профилей:

```powershell
Get-ChildItem -LiteralPath '\\192.168.88.100\shared\soft' -Filter '*.ppp' -File |
  Select-Object -ExpandProperty FullName
```

## SSH-ключ для Windows Admin

FastAPI использует ключ `/home/ubuntu/.ssh/id_ed25519`; password-auth в
интеграции не применяется. Получить публичную часть ключа можно так:

```bash
ssh-keygen -y -f /home/ubuntu/.ssh/id_ed25519
```

Для административного Windows-пользователя `Admin` поместите публичный ключ в:

```text
C:\ProgramData\ssh\administrators_authorized_keys
```

Не используйте только `C:\Users\Admin\.ssh\authorized_keys`: для членов
группы Administrators Windows OpenSSH обычно читает системный файл.

В PowerShell, запущенном от администратора, закрепите корректные ACL и
перезапустите службу:

```powershell
icacls.exe 'C:\ProgramData\ssh\administrators_authorized_keys' /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F'
Restart-Service sshd
```

При смене Windows-хоста сначала сверяйте его fingerprint вне недоверенной сети,
затем добавляйте ключ в known_hosts production VM:

```bash
ssh-keyscan -T 10 -t ed25519 192.168.88.102 | ssh-keygen -lf -
ssh-keyscan -T 10 -t ed25519 192.168.88.102 >> /home/ubuntu/.ssh/known_hosts
```

Для текущего сервера подтверждён fingerprint ED25519:

```text
SHA256:UmAVjCA8VQUtubJtx08En8pAeYNBGaq7Wg5ZsqEvaR0
```

Добавляйте ключ только если fingerprint совпадает.

## Расшифровка типовых ошибок

| Сообщение | Причина | Что делать |
| --- | --- | --- |
| `код 255`, `Connection timed out` | Сеть, неверный IP, закрыт TCP/22 или отключён SSH-сервис Windows | Проверить адрес, VPN/маршрутизацию, firewall и службу `sshd`. |
| `код 255`, `Host key verification failed` | Production не доверяет ключу нового Windows-хоста | Сверить fingerprint и обновить `known_hosts`. |
| `код 255`, `Permission denied` | Ключ FastAPI не разрешён для `Admin` | Добавить публичный ключ в `administrators_authorized_keys`, проверить ACL, перезапустить `sshd`. |
| `код 1` | SSH и CLI запустились, но PitStop не смог обработать аргументы, профиль или файлы | Проверить `Test-Path` CLI, `.ppp`, UNC-папок и журнал PitStop. |
| Не удаётся прочитать JSON-отчёт PitStop | CLI не создал отчёт в общей папке | Проверить права записи в shared-папку и путь `IMAGE_MAGIC_PITSTOP_WINDOWS_SHARED_ROOT`. |

## Проверка после восстановления

1. Подтвердите SSH-тест и доступность CLI/профиля.
2. Создайте **новую** проверку PDF в панели. Старый запуск сохраняет прежнюю
   ошибку и не является подтверждением исправления.
3. Убедитесь, что у новой проверки есть результат PitStop: `passed`, `warning`
   или содержательная ошибка профиля/PDF, а не техническое `PitStop недоступен`.
4. Если сервис перезапускался, зафиксируйте `MainPID` и время старта из команды
   выше в журнале инцидента.
