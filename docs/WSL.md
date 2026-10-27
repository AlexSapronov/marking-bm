# Локальный запуск marking-bm в WSL2 (без VPS, Tailscale и публичного HTTPS)

Это **боевое приложение** с реальными клиентами ГИС МТ и СУЗ. Никаких деморежимов, тестовых кодов или фиктивных ответов нет. Сервер работает внутри WSL2, браузер и подписант (Windows КриптоПро + Рутокен) на том же ПК. Для соединения между ними используется только localhost.

**ВАЖНО:** Если переносите существующие накладные/очередь с VPS, **не допускайте одновременной работы двух экземпляров**, особенно после включения `BM_ALLOW_EXTERNAL_WRITES=1`. В старой БД уже есть подписанный заказ КМ, который **может стать реальным платным заказом при включении внешней отправки**. Дубликаты не создавайте.

## 1. Однократно остановить старый VPS (до миграции)

Подключитесь к старому серверу по SSH (можно по его публичному IP), затем:

```bash
systemctl disable --now marking-bm.service
systemctl is-active marking-bm.service || true
grep '^BM_ALLOW_EXTERNAL_WRITES=' /etc/marking-bm.env
```

Желаемый результат: `inactive`. Не включайте старую службу повторно после переноса.

Сохраните согласованную копию работающей SQLite БД:

```bash
install -d -o markingbm -g markingbm -m 700 /var/lib/marking-bm/backups
sudo -u markingbm /opt/marking-bm/.venv/bin/python /opt/marking-bm/scripts/backup.py \
 /var/lib/marking-bm/marking-bm.sqlite3 /var/lib/marking-bm/backups/wsl-migration.sqlite3
```

Этот backup включает документы, КМ, файлы, заявки, подписи и очередь. Секреты из `/etc/marking-bm.env` и содержимое `tokens.json` переносить **необязательно**; локальные секреты создаются заново. В таком случае ГИС МТ и СУЗ нужно авторизовать заново. При неподтверждённом результате внешних операций сначала сверяйте их с ЧЗ и не повторяйте отправку.

В Windows PowerShell перенесите бэкап однократно по SSH (это не требует Tailscale, но нужен доступ к порту 22 старого VPS):

```powershell
scp root@38.244.174.235:/var/lib/marking-bm/backups/wsl-migration.sqlite3 "$env:USERPROFILE\Downloads\marking-bm-wsl.sqlite3"
```

После импорта удалите временную копию из Windows Downloads, так как БД содержит полные КМ и подписи.

Если **не нужны старые документы**, пропустите миграцию целиком, но всё равно остановите VPS, чтобы старый подписанный заказ не был отправлен случайно.

## 2. Windows: установить WSL2 / Ubuntu, если ещё не установлены

В Windows PowerShell:

```powershell
wsl --list --verbose
```

Если Ubuntu уже стоит, используйте её. Если WSL ещё нет:

```powershell
wsl --install -d Ubuntu-24.04
```

После установки при необходимости перезагрузите Windows, откройте Ubuntu и создайте Linux-пользователя.

## 3. Ubuntu внутри WSL: установить приложение

Эти команды выполняются **в терминале Ubuntu**, не в Windows PowerShell:

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates fonts-dejavu-core
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12
git clone --branch main \
  https://github.com/AlexSapronov/marking-bm.git "$HOME/marking-bm"
cd "$HOME/marking-bm"
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-lock.txt
.venv/bin/python scripts/setup_wsl_local.py
```

Мастер спросит новый пароль администратора (не менее 12 символов) и SHA-1 отпечаток сертификата КриптоПро (40 шестнадцатеричных символов). Последний **не является** токеном соединения. Новый случайный bearer-токен создаётся автоматически, без вывода на экран.

Конфигурация `~/.config/marking-bm/local.env` содержит секреты, имеет права 600 и находится в файловой системе Ubuntu, **не на /mnt/c**. БД — `~/.local/share/marking-bm/marking-bm.sqlite3`. Портал: `http://localhost:8010`; cookies без флага Secure разрешены только для этого локального HTTP, наружу сервер не слушает.

## 4. Импорт старой БД, если нужно сохранить работу с VPS

Сделайте это **до первого запуска локального портала**:

```bash
install -m 600 /mnt/c/Users/Alex/Downloads/marking-bm-wsl.sqlite3 \
 "$HOME/.local/share/marking-bm/marking-bm.sqlite3"
```

Путь с Windows-именем `Alex` используйте только если он соответствует вашей Windows учётной записи. Файл `marking-bm.sqlite3` должен принадлежать вашему обычному Linux пользователю.

Не переносите `tokens.json` со старого сервера без особой необходимости. Новая авторизация ГИС МТ/СУЗ понадобится после включения отправки.

## 5. Запустить WSL портал

В Ubuntu:

```bash
cd "$HOME/marking-bm"
.venv/bin/python scripts/run_wsl_local.py
```

Оставьте этот терминал открытым. Из Windows PowerShell проверьте:

```powershell
curl.exe -fsS http://localhost:8010/health
```

Должен вернуться ответ `{"ok":true}`. Открывайте в Chrome/Edge **http://localhost:8010/** и **http://localhost:8010/admin**.

WSL2 автоматически пробрасывает сервисы Linux на Windows localhost в большинстве конфигураций. **Не используйте `--host 0.0.0.0`, netsh portproxy, Funnel или открытие порта в интернет**.

## 6. Windows: обновить подписант для localhost

Только Windows скрипт в этой ветке умеет подключаться к `http://127.0.0.1:8010`. Для любых удалённых серверов он по-прежнему требует HTTPS.

В Windows PowerShell:

```powershell
$dir = "$env:USERPROFILE\marking-bm-signer"
$base = "https://raw.githubusercontent.com/AlexSapronov/marking-bm/main/signer"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest -Uri "$base/Start-Signer.ps1" -OutFile "$dir\Start-Signer.ps1"
Invoke-WebRequest -Uri "$base/Sign-Payload.vbs" -OutFile "$dir\Sign-Payload.vbs"
```

Получите новый локальный токен из Ubuntu **в буфер Windows**, не отправляя его в чат:

```bash
sed -n 's/^BM_SIGNER_TOKEN=//p' "$HOME/.config/marking-bm/local.env" | clip.exe
```

В Windows PowerShell:

```powershell
powershell.exe -NoProfile -File "$env:USERPROFILE\marking-bm-signer\Start-Signer.ps1" -Server "http://127.0.0.1:8010" -Thumbprint "YOUR_40_HEX_THUMBPRINT" -Diagnose
powershell.exe -NoProfile -File "$env:USERPROFILE\marking-bm-signer\Start-Signer.ps1" -Server "http://127.0.0.1:8010" -Thumbprint "YOUR_40_HEX_THUMBPRINT" -Once
Set-Clipboard -Value ""
```

При запросе `Signer connection token` вставьте токен из буфера. В режиме `-Diagnose` токен не нужен. После успешного `-Once` запустите без него, чтобы держать signer постоянно на связи; окно PowerShell должно оставаться открытым.

## 7. Реальные отправки

`BM_ALLOW_EXTERNAL_WRITES=0` задан **намеренно**. Это не деморежим, а предохранитель перед реальными платными внешними операциями. Даже если Windows подписант показывает «На связи», очередь останется в `queued` до включения отправки.

Если старую БД перенесли, в ней уже есть подписанные `auth_true`, `auth_oms` и `order`. Включение отправки запустит **всю очередь**, включая заказ КМ. Перед этим обязательно проверьте настройки организации, OMS ID, omsConnection, состав и количество заказанных КМ. Сверьте с ЛК ЧЗ, нет ли уже такого заказа.

Только после проверки и осознанного решения, в Ubuntu:

```bash
nano "$HOME/.config/marking-bm/local.env"
```

Поменяйте строго `BM_ALLOW_EXTERNAL_WRITES=0` на `BM_ALLOW_EXTERNAL_WRITES=1`. Затем остановите и снова запустите `scripts/run_wsl_local.py`. **Одно лишь сохранение файла не меняет настройки уже запущенного процесса.**

Если старые подписанные авторизации протухли, возможно, потребуется сформировать новую авторизацию вместо переподписания прежних задач; не делайте ручных SQL-правок без отдельной сверки состояний.

## 8. Ограничения локальной работы

- Портал доступен только с этого компьютера. Для коллег с других ПК нужна будет отдельная серверная публикация с HTTPS и авторизацией.
- После закрытия терминала WSL или выключения ПК портал перестанет работать; после закрытия PowerShell signer перестанет подписывать.
- Отдельно настройте регулярные backup БД на независимый диск; WSL `ext4.vhdx` на системном диске не является бэкапом.
- Старый VPS оставьте выключенным, но не удаляйте его архивы до подтверждённой успешной миграции.
