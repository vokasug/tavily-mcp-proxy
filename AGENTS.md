# Tavily MCP без VPN — прокси с автопереключением по квоте

Полная документация по обходу гео-блокировки Tavily и автоматическому выбору ключа с наибольшим остатком квоты. Репозиторий: <repo-URL>.

> **Безопасность перед публикацией.** Реальные Tavily-ключи, access-ключи, IP-адреса VPS и имена аккаунтов **не должны** попадать в этот репозиторий. Все чувствительные значения в этом документе заменены плейсхолдерами вида `tvly-dev-PLACEHOLDER-…`, `tvmcp_PLACEHOLDER_…`, `<your-VPS-IP>`, `<YOUR-HOST>.sslip.io`, `account-N`. Перед форком/пушем замените плейсхолдеры на свои реальные значения **только локально** на VPS; в git их быть не должно.

## Проблема

Tavily блокирует российские IP (и ряд других санкционных территорий) на уровне AWS ELB (см. tavily.com/terms — «Sanctioned Territory»: Россия, Куба, Иран и др.). С такого IP любой запрос к `mcp.tavily.com` / `api.tavily.com` получает `403 Forbidden` от `awselb/2.0` ещё до проверки API-ключа (запрос без ключа даёт 403 вместо 401). Домашний IP клиента: динамический, RU.

Дополнительно: при наличии нескольких Tavily-аккаунтов (например, 4 dev-ключа по 1000 кредитов/мес на Researcher-плане) хочется автоматически использовать ключ с наибольшим остатком, а не считать вручную.

## Решение

```
Клиент (RU IP)
  → https://<YOUR-HOST>.sslip.io/mcp/?accessKey=tvmcp_<…>
  → nginx (TLS, ACME, fail2ban на 401)
       └─► http://127.0.0.1:8741/mcp/?accessKey=…
            tavily-mcp-backend (systemd, Python 3 + aiohttp)
              ├─ сверяет accessKey по /etc/tavily-mcp/access-keys.list
              ├─ читает активный Tavily-ключ из /etc/tavily-mcp/active-key
              └─ проксирует на https://mcp.tavily.com/mcp/?tavilyApiKey=<active>&…

systemd timer (05:00 Europe/Moscow, daily):
  tavily-mcp-quota.service (oneshot)
    ├─ читает /etc/tavily-mcp/tavily-keys.list
    ├─ GET https://api.tavily.com/usage для каждого ключа
    ├─ выбирает max(account.plan_limit − account.plan_usage);
    │   при равенстве — лексикографически первый ключ
    └─ атомарно пишет /etc/tavily-mcp/active-key + active-key.status=ok
       (если ни один ключ не удалось проверить — пишет пустой active-key
        и active-key.status=unknown → backend отдаёт 503 всем клиентам)
```

- Весь путь зашифрован (HTTPS на обоих плечах). На клиенте меняется только URL в его MCP-конфиге.
- nginx терминирует TLS и пишет access-лог для fail2ban; вся авторизация и подмена ключа — в бэкенде.
- fail2ban банит IP за 5×401 за 24 ч → перманентный бан (порты 80/443, nftables DROP; разбан вручную).

## Компоненты

### VPS (сервер)

- SSH: ключ `~/.ssh/id_ed25519`. Локация — страна, где Tavily доступен напрямую (например, NL).
- Если на клиентской машине включён VPN, прямой SSH может ломаться. Обход: ssh через relay-узел с agent forwarding.
- Ubuntu 24.04, nginx 1.24, certbot 2.9, fail2ban 1.0.2, Python 3.12.3.
- Порты 80/443 заняты nginx; ufw inactive; системный iptables INPUT policy ACCEPT.

### Домен и сертификат

- Имя: `<YOUR-HOST>.sslip.io` → ваш IP (бесплатный wildcard-DNS sslip.io; зеркало `nip.io` — запасное при rate-limit LE).
- Сертификат Let's Encrypt: `/etc/letsencrypt/live/<YOUR-HOST>.sslip.io/`. Срок ~90 дней, автопродление `certbot.timer`.
- ACME HTTP-01 challenge обслуживается блоком на 80 порту (`location /.well-known/acme-challenge/`).
- Имя хоста публично видно в Certificate Transparency логах — это нормально, безопасность держится на секретности ключей.

### nginx

- Конфиг: `/etc/nginx/sites-available/tavily-mcp` (symlink в sites-enabled). Права 600.
- `location /mcp/` → `proxy_pass http://127.0.0.1:8741;` с `proxy_http_version 1.1`, `proxy_set_header Host $host`, `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 3600s`, `proxy_send_timeout 3600s` (SSE-стриминг MCP).
- `location /healthz` → проксирует на `/healthz` бэкенда (loopback); `access_log off`.
- `location /` → `return 404`.
- 80 порт: ACME-challenge + редирект на HTTPS, `access_log off`.
- Лог MCP: `/var/log/nginx/mcp_access.log` формат `mcp_nosecret = '$remote_addr [$time_local] $status $request_method $uri'` (без query string → без секретов). Ошибки: `/var/log/nginx/mcp_error.log`.
- После правок: `nginx -t && systemctl reload nginx`.

### fail2ban

- Фильтр `/etc/fail2ban/filter.d/tavily-mcp.conf`: `failregex = ^<ADDR> \[[^\]]*\] 401 (GET|POST|DELETE|HEAD) /mcp/`
- Jail `/etc/fail2ban/jail.d/tavily-mcp.conf`: port 80,443; logpath `/var/log/nginx/mcp_access.log`; maxretry 5; findtime 86400 (24 ч); **bantime -1 (перманентный бан)**; banaction nftables-multiport.
- 401 генерируется бэкендом при неверном/отсутствующем `accessKey`. 503/fail-loud **не** триггерит fail2ban (regex требует именно 401).
- **Счётчики ошибок после рестарта fail2ban обнуляются**: pyinotify-бекенд не перечитывает историю при старте.
- **ignoreself**: тесты бана с самого VPS не срабатывают; тестировать — с другой машины (НЕ с домашнего IP — забанится он).
- Управление: `fail2ban-client status tavily-mcp`, разбан: `fail2ban-client unban <IP>`.
- После изменения jail-конфигов: `systemctl restart fail2ban`.

### Конфигурация прокси: `/etc/tavily-mcp/`

Каталог `/etc/tavily-mcp/`, `chmod 750 root:root`.

| Файл | Права | Формат | Назначение |
|---|---|---|---|
| `tavily-keys.list` | 600 | по одному Tavily-ключу на строку; пустые строки и `#`-комментарии игнорируются | Список всех ключей для проверки квоты |
| `access-keys.list` | 600 | `<name><whitespace><key>` на строку | Список access-ключей для аутентификации клиентов |
| `active-key` | 644 | одна строка — текущий активный Tavily-ключ | Подставляется в upstream-запрос бэкендом |
| `active-key.status` | 644 | `ok` или `unknown` | Флаг: «quota-checker успешно обновил» или «обновить не удалось» |

Бэкенд читает `access-keys.list` при старте + при SIGHUP, `active-key` + `active-key.status` — **на каждый запрос** (дёшево, ~60 байт). Это даёт мгновенное переключение без рестарта.

**Генерация access-ключа:** `tvmcp_` + URL-safe base64 от 36 случайных байт, итого `tvmcp_<48 chars>`. Префикс для grep'а по логам бэкенда.

### Бэкенд: `/opt/tavily-mcp/`

| Файл | Назначение |
|---|---|
| `backend.py` | aiohttp-сервис, слушает `127.0.0.1:8741` |
| `quota_checker.py` | Утилита выбора ключа (sync, urllib) |
| `requirements.txt` | `aiohttp>=3.9` |
| `venv/` | Python-окружение |

**backend.py** (`/opt/tavily-mcp/venv/bin/python backend.py`):
- `GET /healthz` → `200 ok\n`.
- `* /mcp` / `/mcp/` / `/mcp/{tail}` → основной обработчик:
  1. Извлекает `accessKey` из query string. Нет или неверный → `401` (без upstream-запроса).
  2. Читает `/etc/tavily-mcp/active-key.status` и `/etc/tavily-mcp/active-key`. Статус ≠ `ok` или ключ пуст → `503` (fail-loud).
  3. Конструирует upstream URL: тот же path + query string с заменой `accessKey` → `tavilyApiKey=<active>`.
  4. Проксирует на `https://mcp.tavily.com<path>?<query>` через `aiohttp.ClientSession` (TLS-хоп напрямую к Tavily).
  5. Если upstream отвечает `text/event-stream` / `Transfer-Encoding: chunked` / `Content-Length` отсутствует — ретранслирует чанки через `web.StreamResponse` (SSE). Иначе — обычный `web.Response`.
- Таймауты: connect 30s, total/sock_read 3600s.
- Логи: journal (через stderr) — INFO для обычных запросов, ERROR для fail-loud / upstream-ошибок.
- Сигналы: SIGTERM/SIGINT — graceful shutdown; SIGHUP — перечитать `access-keys.list` (для ротации без рестарта).

**quota_checker.py** (`/opt/tavily-mcp/venv/bin/python quota_checker.py`):
- Парсит `tavily-keys.list` (пропускает пустые/комменты).
- Последовательно делает `GET https://api.tavily.com/usage` с `Authorization: Bearer <key>` (timeout 10s на ключ).
- Парсит JSON, берёт `account.plan_limit - account.plan_usage` как `remaining`. Не-200 / битый JSON / нет полей → ключ пропускается (WARNING).
- Если **все** ключи пропущены → пишет пустой `active-key` + `active-key.status=unknown`, exit code **2**.
- Иначе выбирает `max(remaining)`, при равенстве — лексикографически первый ключ.
- Атомарная запись `active-key` + `active-key.status=ok`: `tempfile → fsync → os.replace`.
- Логирует выбор через journal.

### systemd-юниты

**`/etc/systemd/system/tavily-mcp-backend.service`** — Type=simple, root, `ExecStart=/opt/tavily-mcp/venv/bin/python /opt/tavily-mcp/backend.py`, `Restart=on-failure`. `enable --now` при деплое.

**`/etc/systemd/system/tavily-mcp-quota.service`** — Type=oneshot, выполняет quota_checker.py.

**`/etc/systemd/system/tavily-mcp-quota.timer`** — `OnCalendar=*-*-* 05:00:00 Europe/Moscow`, `Persistent=true`, `AccuracySec=30s`. `enable --now` при деплое.

`systemctl list-timers | grep tavily-mcp` — статус расписания. Ручной запуск: `systemctl start tavily-mcp-quota.service`.

### Клиент

MCP-конфиг (пример для OpenCode; другие клиенты — аналогично):

```json
"tavily": {
  "type": "remote",
  "url": "https://<YOUR-HOST>.sslip.io/mcp/?accessKey=tvmcp_<your-generated-key>",
  "enabled": true
}
```

Изменения применяются при перезапуске сессии клиента. Локальные `/etc/hosts` менять не нужно.

## Проверки

С другой машины (НЕ с домашнего IP — расходует счётчик fail2ban) или с VPS:

```bash
# MCP initialize, ожидание HTTP 200 + SSE с serverInfo tavily-mcp
curl -s -m 30 -X POST "https://<YOUR-HOST>.sslip.io/mcp/?accessKey=tvmcp_<KEY>" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"diag","version":"1.0"}}}'

# неверный accessKey → 401 (от бэкенда)
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://<YOUR-HOST>.sslip.io/mcp/?accessKey=wrong"

# без accessKey → 401; чужой путь → 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://<YOUR-HOST>.sslip.io/mcp/"
curl -s -o /dev/null -w '%{http_code}\n' "https://<YOUR-HOST>.sslip.io/anything/"

# fail-loud: пустой active-key + status=unknown → 503
ssh vps ": > /etc/tavily-mcp/active-key && echo unknown > /etc/tavily-mcp/active-key.status"
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://<YOUR-HOST>.sslip.io/mcp/?accessKey=<KEY>" -d '{}'
ssh vps "/opt/tavily-mcp/venv/bin/python /opt/tavily-mcp/quota_checker.py"

# ручной запуск quota-checker (selftest)
ssh vps "/opt/tavily-mcp/venv/bin/python /opt/tavily-mcp/quota_checker.py"
cat /etc/tavily-mcp/active-key       # должен быть лексикографически первый среди max(remaining)
```

**Не отправляйте accessKey в историю шелла** — берите из `/etc/tavily-mcp/access-keys.list` через `ssh vps 'awk "{print \$2}" /etc/tavily-mcp/access-keys.list'`.

**Каждый 401 от домашнего IP расходует счётчик fail2ban.** 5-й 401 за 24 ч → перманентный бан. Не «прощупывайте» 401-кейсы с домашней машины; используйте `ssh vps 'curl http://127.0.0.1:8741/…'` — там ignoreself.

## Диагностика «Tavily заблокирован vs работает»

- С домашнего (RU) IP: `curl https://api.tavily.com/usage` (без ключа) → 403 от awselb = гео-блок.
- Нормальный доступ: тот же запрос → 401 (JSON), `mcp.tavily.com` с мусорным телом → 406/400, не 403.
- Через прокси с VPS: `curl https://api.tavily.com/usage` → 401, не 403. Если вдруг 403 — проверить `curl -s https://ipinfo.io/json` (country должен соответствовать выбранной локации); возможно, IP VPS сменился (тогда перевыпустить sslip.io-имя).

## Troubleshooting

- **502/504 от прокси** — бэкенд не смог достучаться до Tavily: `journalctl -u tavily-mcp-backend -n 20`. Проверить `curl -m 5 https://mcp.tavily.com` с VPS (должен быть 406/400, не 403).
- **503 на все запросы** — fail-loud: `cat /etc/tavily-mcp/active-key` (пуст?) и `cat /etc/tavily-mcp/active-key.status` (`unknown`?). Если оба — quota-checker не смог проверить ни один ключ: `journalctl -u tavily-mcp-quota -n 10`. Восстановить вручную: `/opt/tavily-mcp/venv/bin/python /opt/tavily-mcp/quota_checker.py`. Типичные причины массовых 401/429 от `/usage`: rate-limit (Tavily лимитирует `/usage`; не запускайте quota_checker чаще, чем раз в несколько минут — он рассчитан на 1 запуск в сутки).
- **Бэкенд не стартует** — `journalctl -u tavily-mcp-backend -e`. Типичные причины: не установлен aiohttp в venv, занят порт 8741, syntax error в backend.py после правок.
- **Квоты не обновляются** — `systemctl list-timers tavily-mcp-quota.timer` (NEXT/LAST). Если NEXT в прошлом — `systemctl start tavily-mcp-quota.service` принудительно; если снова не помогло — проверить `OnCalendar=*-*-* 05:00:00 Europe/Moscow` и часовой пояс: `timedatectl`.
- **Клиент: server unavailable** — сначала curl-тест initialize (см. «Проверки»); затем лог клиента (для OpenCode: `grep tavily ~/.local/share/opencode/log/opencode.log`).
- **Свой IP в бане** — `ssh vps 'fail2ban-client unban <IP>'` (порт 22 не банится). После разбана не повторять опечаток в течение 24 ч.
- **Скомпрометирован access-ключ (ротация)**:
  ```bash
  # На VPS
  NEW_KEY=$(python3 -c 'import secrets,base64;print("tvmcp_"+base64.urlsafe_b64encode(secrets.token_bytes(36)).decode().rstrip("="))')
  sed -i "/^my\t/ c\my\t${NEW_KEY}" /etc/tavily-mcp/access-keys.list
  systemctl reload tavily-mcp-backend.service   # или kill -HUP $(pgrep -f backend.py)
  # На клиенте: обновить URL в MCP-конфиге, перезапустить сессию
  ```
- **Добавить новый Tavily-ключ** — дописать строку в `/etc/tavily-mcp/tavily-keys.list`; следующий запуск quota-checker учтёт.
- **Добавить второй access-ключ** — дописать строку в `/etc/tavily-mcp/access-keys.list` (`other_name\ttvmcp_…`); `kill -HUP $(pgrep -f backend.py)` для перечитки без рестарта.
- **IP VPS сменился** — sslip.io-имя привязывается к IP. Сгенерировать новое имя `<новый-IP-с-дефисами>.sslip.io` (или перейти на nip.io), обновить `server_name` в nginx + URL клиента, перевыпустить сертификат.

## Восстановление с нуля (runbook)

Полный порядок действий, если VPS обнулён (тот же IP — иначе имя sslip.io изменится: `<новый-IP-с-дефисами>.sslip.io`, и надо обновить server_name + URL клиента).

```bash
# 0. Доступ: ssh vps (root@<VPS-IP>). Ubuntu 24.04.
curl -s -o /dev/null -w '%{http_code}\n' https://api.tavily.com/usage   # ожидание 401, не 403

# 1. Пакеты
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    nginx certbot python3-certbot-nginx fail2ban python3-venv python3-pip

# 2. Сертификат Let's Encrypt — временный сайт на 80 порту
cat > /etc/nginx/sites-available/tavily-mcp <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name <YOUR-HOST>.sslip.io;
    location / { return 200 "ok\n"; }
}
EOF
ln -sf /etc/nginx/sites-available/tavily-mcp /etc/nginx/sites-enabled/tavily-mcp
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
certbot --nginx -d <YOUR-HOST>.sslip.io --agree-tos --register-unsafely-without-email -n
# при rate-limit: то же с -d <YOUR-HOST>.nip.io + правка server_name и URL клиента
systemctl list-timers | grep certbot   # автопродление активно

# 3. /etc/tavily-mcp/ и /opt/tavily-mcp/
mkdir -p /etc/tavily-mcp && chmod 750 /etc/tavily-mcp
mkdir -p /opt/tavily-mcp && chmod 755 /opt/tavily-mcp

# Tavily-ключи (формат — deploy/tavily-keys.list.example; реальные ключи не в git!):
cat > /etc/tavily-mcp/tavily-keys.list <<'EOF'
tvly-dev-PLACEHOLDER-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
tvly-dev-PLACEHOLDER-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
tvly-dev-PLACEHOLDER-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
tvly-dev-PLACEHOLDER-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
EOF
chmod 600 /etc/tavily-mcp/tavily-keys.list

# Access-ключ — сгенерировать:
ACCESS_KEY="tvmcp_$(python3 -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(36)).decode().rstrip("="))')"
printf 'my\t%s\n' "$ACCESS_KEY" > /etc/tavily-mcp/access-keys.list
chmod 600 /etc/tavily-mcp/access-keys.list
echo "ACCESS_KEY=$ACCESS_KEY  (сохранить локально — пригодится для клиентского MCP-конфига)"

# active-key init — первый ключ из списка (после первого cron выберется оптимальный)
head -n 1 /etc/tavily-mcp/tavily-keys.list > /etc/tavily-mcp/active-key
chmod 644 /etc/tavily-mcp/active-key
printf 'ok\n' > /etc/tavily-mcp/active-key.status
chmod 644 /etc/tavily-mcp/active-key.status

# 4. Скопировать код бэкенда и quota-checker
scp backend.py quota_checker.py requirements.txt vps:/opt/tavily-mcp/
ssh vps "chmod 755 /opt/tavily-mcp/backend.py /opt/tavily-mcp/quota_checker.py
cd /opt/tavily-mcp
python3 -m venv venv
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt"

# 5. systemd-юниты
scp deploy/tavily-mcp-backend.service deploy/tavily-mcp-quota.service deploy/tavily-mcp-quota.timer \
    vps:/etc/systemd/system/
ssh vps "systemctl daemon-reload
systemctl enable --now tavily-mcp-backend tavily-mcp-quota.timer"

# 6. nginx — финальный конфиг (см. deploy/tavily-mcp.nginx.conf; заменить <YOUR-HOST>.sslip.io):
scp deploy/tavily-mcp.nginx.conf vps:/etc/nginx/sites-available/tavily-mcp
ssh vps "chmod 600 /etc/nginx/sites-available/tavily-mcp
nginx -t && systemctl reload nginx"

# 7. fail2ban — фильтр и jail
cat > /etc/fail2ban/filter.d/tavily-mcp.conf <<'EOF'
[Definition]
failregex = ^<ADDR> \[[^\]]*\] 401 (GET|POST|DELETE|HEAD) /mcp/
ignoreregex =
EOF

cat > /etc/fail2ban/jail.d/tavily-mcp.conf <<'EOF'
[tavily-mcp]
enabled = true
port = 80,443
filter = tavily-mcp
logpath = /var/log/nginx/mcp_access.log
maxretry = 5
findtime = 86400
bantime = -1
banaction = nftables-multiport
backend = auto
EOF

systemctl restart fail2ban
fail2ban-client status tavily-mcp

# 8. Проверки — см. раздел «Проверки».

# 9. На клиенте: обновить MCP-конфиг (пример для OpenCode):
#    "url": "https://<YOUR-HOST>.sslip.io/mcp/?accessKey=$ACCESS_KEY"
#    "enabled": true
#    Перезапустить сессию клиента.
```

## Откат

1. На клиенте: вернуть `"url": "https://mcp.tavily.com/mcp/?tavilyApiKey=<tvly-…>"` (работает только с VPN).
2. На VPS:
   ```bash
   systemctl disable --now tavily-mcp-backend tavily-mcp-quota.timer
   rm -rf /opt/tavily-mcp /etc/tavily-mcp /etc/systemd/system/tavily-mcp-*
   rm /etc/nginx/sites-enabled/tavily-mcp
   nginx -t && systemctl reload nginx
   systemctl disable --now fail2ban
   certbot delete --cert-name <YOUR-HOST>.sslip.io
   apt-get purge -y nginx certbot python3-certbot-nginx fail2ban python3-venv python3-pip
   ```

## Заметки по безопасности

- Эндпоинт считается публично обнаружимым (сканеры + CT-логи). Защита:
  - whitelist access-ключей (без валидного ключа — 401);
  - fail2ban против перебора (5×401 → перманентный бан);
  - Tavily-ключи никогда не покидают VPS (бэкенд подставляет их в upstream-запрос);
  - ключи не пишутся в логи (формат `mcp_nosecret` без query string; backend логирует только метаданные без секретов).
- **Компрометация access-ключа** = любой может расходовать Tavily-квоту. Лечится ротацией (см. Troubleshooting). Префикс `tvmcp_` облегчает grep по журналам.
- **Компрометация Tavily-ключа** (например, через утечку логов VPS) = потеря месячной квоты этого аккаунта. Перевыпуск: app.tavily.com. Удалить из `/etc/tavily-mcp/tavily-keys.list` + `systemctl start tavily-mcp-quota.service`.
- **Fail2ban самобан**: 5 опечаток в access-ключе за 24 ч → перманентный тотальный бан включая SSH. Лечится `fail2ban-client unban <IP>` через SSH (порт 22 не банится) или сменой IP. `ignoreip` для домашней сети не добавлен сознательно (иначе злоумышленник из той же сети не банится).
- **fail2ban recidive отсутствует** — после `fail2ban-client unban` счётчик 401-событий сохраняется, и 5-я опечатка в ближайшие 24 ч снова = бан.

## Защита VPS

На VPS с прокси настроен fail2ban для защиты от внешних атак. Все баны перманентные (`bantime -1`), блокируют весь TCP через `nftables-allports` (правила без `tcp dport`, reject с `icmp port-unreachable`), хранятся в sqlite и переживают рестарт.

**Активные джейлы:**

- `sshd` — 5 неудачных SSH-логинов за 24 ч → перманентный тотальный бан.
- `nginx-scan` — 1 HTTP-запрос к путям сканеров (`/.env`, `/.git`, `/.aws`, `/.svn`, `/.ssh`, `/wp-admin`, `/wp-login.php`, `/xmlrpc.php`, `/phpmyadmin`, `/shell.php`, `/backup`, `/.sql`, `/vendor`, `/jenkins`, `/manager/html` и т.п.) → перманентный тотальный бан.
- `tavily-mcp` — описан выше в разделе «fail2ban».

`recidive` отключён — не нужен при перманентных банах. Свои домашние IP в `ignoreip` не добавлены осознанно (иначе злоумышленник из той же сети не банится).

### ⚠️ Самобан — критично

5 провалов SSH за сутки (протухший/чужой ключ в `ssh-agent`) или 1 запрос к путям сканеров из браузера/curl = перманентный тотальный бан, включая SSH с вашего IP. Это означает полную потерю доступа к VPS по этому IP.

**ЗАПРЕЩЕНО** тестировать баны с домашней машины.

Если уже забанились — подключиться через альтернативный IP (мобильный хотспот, VPN) или через веб-консоль хостера, затем `ssh vps 'fail2ban-client unban <домашний IP>'`.

### Управление

```bash
fail2ban-client status                      # список джейлов
fail2ban-client status <jail>               # баны конкретного джейла
fail2ban-client banned                      # все забаненные IP
fail2ban-client unban <IP>                  # разбанить глобально
nft list table inet f2b-table               # сеты и правила
tail -f /var/log/fail2ban.log               # Ban/Unban/Found события
```

После правок конфигов: `systemctl restart fail2ban` (reload не подхватывает новые джейлы).

`fail2ban-client get sshd banaction` в v1.0.2 не работает — allports проверять через `nft list table inet f2b-table` (правило без `tcp dport` = тотальный бан).

## Файлы и ссылки

| Где | Что |
|---|---|
| `/etc/nginx/sites-available/tavily-mcp` | nginx-конфиг |
| `/etc/letsencrypt/live/<YOUR-HOST>.sslip.io/` | Сертификат LE |
| `/etc/fail2ban/filter.d/tavily-mcp.conf` | fail2ban-фильтр |
| `/etc/fail2ban/jail.d/tavily-mcp.conf` | fail2ban-jail |
| `/etc/tavily-mcp/tavily-keys.list` | Tavily-ключи (600, root) |
| `/etc/tavily-mcp/access-keys.list` | Access-ключи клиентов (600, root) |
| `/etc/tavily-mcp/active-key` | Текущий активный Tavily-ключ |
| `/etc/tavily-mcp/active-key.status` | `ok` / `unknown` |
| `/opt/tavily-mcp/backend.py` | aiohttp-бэкенд |
| `/opt/tavily-mcp/quota_checker.py` | Селектор ключа по квоте |
| `/opt/tavily-mcp/venv/` | Python venv с aiohttp |
| `/opt/tavily-mcp/requirements.txt` | `aiohttp>=3.9` |
| `/etc/systemd/system/tavily-mcp-backend.service` | Юнит бэкенда |
| `/etc/systemd/system/tavily-mcp-quota.{service,timer}` | Расписание 05:00 MSK |
| `/var/log/nginx/mcp_access.log` | Лог для fail2ban (без секретов) |
| `/var/log/nginx/mcp_error.log` | nginx error |
| `journalctl -u tavily-mcp-backend` | Лог бэкенда (без секретов) |
| `journalctl -u tavily-mcp-quota` | Лог quota-checker'а |