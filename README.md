# tavily-mcp-proxy

Reverse-прокси для эндпоинта [Tavily MCP](https://www.tavily.com), который:

1. **Обходит гео-блокировку** для клиентов на IP из санкционных территорий (RU и др.) — прокси живёт на VPS в стране, откуда Tavily доступен, и пересылает запросы. Tavily блокирует такие IP на уровне AWS ELB (см. tavily.com/terms — «Sanctioned Territory»): любой запрос к `mcp.tavily.com` / `api.tavily.com` получает `403 Forbidden` от `awselb/2.0` ещё до проверки API-ключа (запрос без ключа даёт 403 вместо 401).
2. **Ротирует несколько Tavily API-ключей**, выбирая раз в сутки ключ с наибольшим остатком квоты. Если ни один ключ не удалось проверить — прокси возвращает **HTTP 503** всем клиентам (fail-loud) до восстановления.
3. **Аутентифицирует клиентов** по query-параметру `accessKey`, чтобы реальные Tavily-ключи никогда не попадали на клиентские машины и не светились в клиентских конфигах.

```
Клиент (RU IP)
  → https://<YOUR-HOST>.sslip.io/mcp/?accessKey=tvmcp_<…>
  → nginx (TLS, ACME)
       └─► http://127.0.0.1:8741/mcp/?accessKey=…
            tavily-mcp-backend (systemd, Python 3 + aiohttp)
              ├─ сверяет accessKey по /etc/tavily-mcp/access-keys.list
              ├─ читает активный Tavily-ключ из /etc/tavily-mcp/active-key
              └─ проксирует на https://mcp.tavily.com/mcp/?tavilyApiKey=<active>&…

systemd timer (05:00 Europe/Moscow, daily):
  tavily-mcp-quota.service (oneshot)
    ├─ читает /etc/tavily-mcp/tavily-keys.list
    ├─ GET https://api.tavily.com/usage для каждого ключа
    ├─ выбирает max(plan_limit − plan_usage); при равенстве — лексикографически первый ключ
    └─ атомарно пишет /etc/tavily-mcp/active-key + active-key.status=ok
       (если ни один ключ не удалось проверить — пишет пустой active-key
        и active-key.status=unknown → backend отдаёт 503 всем клиентам)
```

Весь путь зашифрован (HTTPS на обоих плечах). На клиенте меняется только URL в его MCP-конфиге. nginx терминирует TLS; вся авторизация и подмена ключа — в бэкенде.

## Состав репозитория

| Путь | Назначение |
|---|---|
| `backend.py` | aiohttp-сервис: аутентифицирует клиентов и проксирует `/mcp/*` к Tavily с активным ключом |
| `quota_checker.py` | Ежедневный селектор: выбирает Tavily-ключ с максимальным остатком квоты, при равенстве — лексикографически первый |
| `requirements.txt` | `aiohttp>=3.9` |
| `deploy/tavily-mcp-backend.service` | systemd-юнит бэкенда |
| `deploy/tavily-mcp-quota.{service,timer}` | systemd oneshot + таймер на 05:00 Europe/Moscow |
| `deploy/tavily-mcp.nginx.conf` | Шаблон nginx: TLS + ACME + reverse-прокси (плейсхолдер `YOUR-HOST.sslip.io`) |
| `deploy/tavily-keys.list.example` | Пример формата `/etc/tavily-mcp/tavily-keys.list` (без реальных ключей) |
| `deploy/access-keys.list.example` | Пример формата `/etc/tavily-mcp/access-keys.list` (без реальных ключей) |

## Быстрый старт (TL;DR)

Полный runbook — ниже в «Восстановление с нуля». Самый короткий путь:

```bash
# На VPS (Ubuntu 24.04)
apt-get install -y nginx certbot python3-certbot-nginx python3-venv python3-pip

mkdir -p /etc/tavily-mcp /opt/tavily-mcp
# заполнить /etc/tavily-mcp/tavily-keys.list и access-keys.list (см. deploy/*.example)

cd /opt/tavily-mcp
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp deploy/tavily-mcp-backend.service deploy/tavily-mcp-quota.service \
   deploy/tavily-mcp-quota.timer /etc/systemd/system/
cp deploy/tavily-mcp.nginx.conf /etc/nginx/sites-available/tavily-mcp
# отредактировать YOUR-HOST.sslip.io в конфиге nginx, затем:
ln -sf /etc/nginx/sites-available/tavily-mcp /etc/nginx/sites-enabled/tavily-mcp
nginx -t && systemctl reload nginx

systemctl daemon-reload
systemctl enable --now tavily-mcp-backend tavily-mcp-quota.timer
```

Конфигурация MCP-клиента (OpenCode, Claude Desktop, MCP CLI и т.п.):

```
https://YOUR-HOST.sslip.io/mcp/?accessKey=tvmcp_<ваш-сгенерированный-ключ>
```

Изменения применяются при перезапуске сессии клиента. Локальные `/etc/hosts` менять не нужно.

## Конфигурационные файлы на сервере

Каталог `/etc/tavily-mcp/`, `chmod 750 root:root`:

| Путь | Права | Формат | Назначение |
|---|---|---|---|
| `/etc/tavily-mcp/tavily-keys.list` | 0600 | по одному Tavily-ключу на строку; пустые строки и `#`-комментарии игнорируются | Все ключи, между которыми ротирует quota-checker |
| `/etc/tavily-mcp/access-keys.list` | 0600 | `<имя><пробел/таб><ключ>` на строку | Access-ключи клиентов |
| `/etc/tavily-mcp/active-key` | 0644 | одна строка — текущий активный Tavily-ключ | Читается заново на каждом запросе бэкенда |
| `/etc/tavily-mcp/active-key.status` | 0644 | `ok` или `unknown` | Говорит бэкенду: обслуживать или отдавать 503 |

`active-key` переписывается **атомарно** (`tempfile → fsync → os.replace`) скриптом `quota_checker.py`. Бэкенд читает `active-key` и `active-key.status` на каждый запрос (дёшево, ~60 байт), поэтому переключение ключа происходит без рестарта. `access-keys.list` читается при старте + по SIGHUP.

## Генерация access-ключа

```bash
python3 -c 'import secrets, base64; print("tvmcp_" + base64.urlsafe_b64encode(secrets.token_bytes(36)).decode().rstrip("="))'
```

Итого `tvmcp_<48 chars>`; префикс нужен для grep'а по логам. После изменения `/etc/tavily-mcp/access-keys.list`:

```bash
kill -HUP $(pgrep -f backend.py)
```

…или перезапустить systemd-сервис.

## Как устроен бэкенд

`backend.py` (запуск: `/opt/tavily-mcp/venv/bin/python backend.py`):

- `GET /healthz` → `200 ok\n` (nginx проксирует `/healthz` на него, `access_log off`).
- `* /mcp`, `/mcp/`, `/mcp/{tail}` → основной обработчик:
  1. Извлекает `accessKey` из query string. Нет или неверный → `401` (без upstream-запроса).
  2. Читает `active-key.status` и `active-key`. Статус ≠ `ok` или ключ пуст → `503` (fail-loud).
  3. Конструирует upstream URL: тот же path + query string с заменой `accessKey` → `tavilyApiKey=<active>`.
  4. Проксирует на `https://mcp.tavily.com<path>?<query>` через `aiohttp.ClientSession`.
  5. Если upstream отвечает `text/event-stream` / `Transfer-Encoding: chunked` / нет `Content-Length` — ретранслирует чанки через `web.StreamResponse` (SSE). Иначе — обычный `web.Response`.
- Таймауты: connect 30s, total/sock_read 3600s.
- Логи в journal: `req: key=<имя> client=<реальный IP> method=<m> path=<p> rpc=<jsonrpc-метод>[:<инструмент> <args≤200 chars>]` — без секретов (встроенный access-log aiohttp отключён: он слил бы `accessKey` из query string). Реальный IP берётся из `X-Forwarded-For`, который ставит nginx. Статистика по ключам: `journalctl -u tavily-mcp-backend | grep -c 'req: key=<имя>'`.
- Сигналы: SIGTERM/SIGINT — graceful shutdown; SIGHUP — перечитать `access-keys.list`.

`quota_checker.py`:

- Парсит `tavily-keys.list` (пропускает пустые/комменты).
- Последовательно делает `GET https://api.tavily.com/usage` с `Authorization: Bearer <key>` (timeout 10s на ключ).
- Берёт `account.plan_limit − account.plan_usage` как `remaining`. Не-200 / битый JSON / нет полей → ключ пропускается (WARNING).
- Если **все** ключи пропущены → пишет пустой `active-key` + `active-key.status=unknown`, exit code **2**.
- Иначе выбирает `max(remaining)`, при равенстве — лексикографически первый ключ.
- Атомарная запись `active-key` + `active-key.status=ok`, лог через journal.
- Рассчитан на 1 запуск в сутки — не запускайте чаще, чем раз в несколько минут (Tavily rate-limit'ит `/usage`).

## nginx

- Конфиг: `/etc/nginx/sites-available/tavily-mcp` (symlink в sites-enabled), права 600.
- `location /mcp/` → `proxy_pass http://127.0.0.1:8741;` с `proxy_http_version 1.1`, `proxy_set_header Host $host`, `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`, `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 3600s`, `proxy_send_timeout 3600s` (SSE-стриминг MCP).
- `location /healthz` → проксирует на бэкенд; `access_log off`.
- `location /` → `return 404`.
- 80 порт: ACME-challenge (`location /.well-known/acme-challenge/`) + редирект на HTTPS, `access_log off`. Порт 80 должен быть открыт снаружи для HTTP-01 challenge.
- Лог MCP: `/var/log/nginx/mcp_access.log` формат `mcp_nosecret = '$remote_addr [$time_local] $status $request_method $uri'` (без query string → без секретов). Ошибки: `/var/log/nginx/mcp_error.log`.
- После правок: `nginx -t && systemctl reload nginx`.

## HTTPS / домен

1. **Wildcard-DNS через публичные сервисы.** `sslip.io` и `nip.io` — это разные сервисы (НЕ зеркала друг друга), оба резолвят имя вида `<IP-С-ДЕФИСАМИ>.<service>.io` в IP, зашитый в имя. Регистрация в DNS не нужна.
2. **Сертификат Let's Encrypt** через `certbot --nginx -d <YOUR-HOST>.sslip.io` (HTTP-01 challenge на 80 порту). Файлы: `/etc/letsencrypt/live/<YOUR-HOST>.../fullchain.pem`, `privkey.pem`. **Срок действия — 90 дней.**
3. **Автопродление** — `certbot.timer` (systemd): каждые ~12 часов проверяет, осталось ли <15 дней до истечения, и автоматически перевыпускает (на практике — на 75-й день жизни). Порог задан в `/etc/letsencrypt/renewal/<domain>.conf` через `renew_before_expiry = 15 days`. Статус: `systemctl status certbot.timer`, `journalctl -u certbot.service`. Если таймер не сработал — `certbot renew` вручную.
4. **Когда LE упирается в rate-limit** (5 сертификатов в неделю на один домен): сгенерировать имя `<IP-С-ДЕФИСАМИ>.nip.io`, получить сертификат для него, обновить `server_name` в nginx **и URL в MCP-клиенте**. Для LE это разные домены — rate-limit sslip.io не действует на nip.io.

Имя хоста видно в публичных логах Certificate Transparency — это нормально. Безопасность держится на секретности ключей.

### ⚠️ Что ломает работу MCP-клиента при смене IP VPS

Имя `<IP-С-ДЕФИСАМИ>.sslip.io` привязано к конкретному IP. Если **IP VPS меняется** (переустановка, миграция, смена тарифа):

- Резолв старого имени перестаёт указывать на новый IP.
- Старый сертификат остаётся валидным по дате, но клиент обращается к чужому IP → TLS-хендшейк не пройдёт.
- **MCP-клиент перестаёт работать**, пока на клиентской машине не обновится URL на новое имя.

Порядок действий — в Troubleshooting → «IP VPS сменился».

## Проверки

С другой машины или с VPS:

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
ssh vps "cat /etc/tavily-mcp/active-key"       # лексикографически первый среди max(remaining)
```

**Не отправляйте accessKey в историю шелла** — берите из `/etc/tavily-mcp/access-keys.list` через `ssh vps 'awk "{print \$2}" /etc/tavily-mcp/access-keys.list'`.

401-кейсы удобно проверять с самого VPS: `ssh vps 'curl http://127.0.0.1:8741/…'`.

## Диагностика «Tavily заблокирован vs работает»

- С домашнего (RU) IP: `curl https://api.tavily.com/usage` (без ключа) → 403 от awselb = гео-блок.
- Нормальный доступ: тот же запрос → 401 (JSON), `mcp.tavily.com` с мусорным телом → 406/400, не 403.
- Через VPS: `curl https://api.tavily.com/usage` → 401, не 403. Если вдруг 403 — проверить `curl -s https://ipinfo.io/json` (country); возможно, IP VPS сменился (тогда перевыпустить sslip.io-имя).

## Troubleshooting

- **502/504 от прокси** — бэкенд не смог достучаться до Tavily: `journalctl -u tavily-mcp-backend -n 20`. Проверить `curl -m 5 https://mcp.tavily.com` с VPS (должен быть 406/400, не 403).
- **503 на все запросы** — fail-loud: `cat /etc/tavily-mcp/active-key` (пуст?) и `active-key.status` (`unknown`?). Если оба — quota-checker не смог проверить ни один ключ: `journalctl -u tavily-mcp-quota -n 10`. Восстановить вручную: `/opt/tavily-mcp/venv/bin/python /opt/tavily-mcp/quota_checker.py`. Типичная причина массовых 401/429 от `/usage` — rate-limit (не запускайте quota_checker чаще, чем раз в несколько минут).
- **Бэкенд не стартует** — `journalctl -u tavily-mcp-backend -e`. Типичные причины: не установлен aiohttp в venv, занят порт 8741, syntax error в backend.py после правок.
- **Квоты не обновляются** — `systemctl list-timers tavily-mcp-quota.timer` (NEXT/LAST). Если NEXT в прошлом — `systemctl start tavily-mcp-quota.service` принудительно; если снова не помогло — проверить `OnCalendar=*-*-* 05:00:00 Europe/Moscow` и часовой пояс: `timedatectl`.
- **Клиент: server unavailable** — сначала curl-тест initialize (см. «Проверки»); затем лог клиента (для OpenCode: `grep tavily ~/.local/share/opencode/log/opencode.log`).
- **Скомпрометирован access-ключ (ротация)**:
  ```bash
  # На VPS
  NEW_KEY=$(python3 -c 'import secrets,base64;print("tvmcp_"+base64.urlsafe_b64encode(secrets.token_bytes(36)).decode().rstrip("="))')
  sed -i "/^my\t/ c\my\t${NEW_KEY}" /etc/tavily-mcp/access-keys.list
  systemctl reload tavily-mcp-backend.service   # или kill -HUP $(pgrep -f backend.py)
  # На клиенте: обновить URL в MCP-конфиге, перезапустить сессию
  ```
- **Скомпрометирован Tavily-ключ** — перевыпуск на app.tavily.com, удалить старый из `/etc/tavily-mcp/tavily-keys.list`, `systemctl start tavily-mcp-quota.service`.
- **Добавить новый Tavily-ключ** — дописать строку в `/etc/tavily-mcp/tavily-keys.list`; следующий запуск quota-checker учтёт.
- **Добавить второй access-ключ** — дописать строку в `/etc/tavily-mcp/access-keys.list` (`other_name\ttvmcp_…`); `kill -HUP $(pgrep -f backend.py)`.
- **IP VPS сменился** — sslip.io-имя привязано к IP, после смены оно указывает на чужой адрес, MCP-клиент перестаёт работать до обновления URL. Чеклист:
  1. Определить новый IP (`ip -4 addr show` на VPS или через панель хостера).
  2. Сгенерировать новое имя: `<НОВЫЙ-IP-С-ДЕФИСАМИ>.sslip.io` (или `.nip.io`, если sslip.io в LE rate-limit).
  3. На VPS: `certbot --nginx -d <НОВОЕ-ИМЯ>` (выпустит сертификат, обновит `server_name`, перезагрузит nginx). Альтернативно вручную: получить сертификат, прописать пути в `/etc/nginx/sites-available/tavily-mcp`, `nginx -t && systemctl reload nginx`.
  4. На клиенте: обновить URL в MCP-конфиге (для OpenCode — `~/.config/opencode/opencode.jsonc`) на `https://<НОВОЕ-ИМЯ>/mcp/?accessKey=<тот же accessKey>`, перезапустить сессию. **Этот шаг обязателен.**
  5. Старый сертификат можно удалить: `certbot delete --cert-name <СТАРОЕ-ИМЯ>`.

## Восстановление с нуля (runbook)

Полный порядок действий, если VPS обнулён (тот же IP — иначе имя sslip.io изменится: `<новый-IP-с-дефисами>.sslip.io`, и надо обновить server_name + URL клиента).

```bash
# 0. Доступ: ssh vps (root@<VPS-IP>). Ubuntu 24.04.
curl -s -o /dev/null -w '%{http_code}\n' https://api.tavily.com/usage   # ожидание 401, не 403

# 1. Пакеты
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    nginx certbot python3-certbot-nginx python3-venv python3-pip

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
EOF
chmod 600 /etc/tavily-mcp/tavily-keys.list

# Access-ключ — сгенерировать:
ACCESS_KEY="tvmcp_$(python3 -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(36)).decode().rstrip("="))')"
printf 'my\t%s\n' "$ACCESS_KEY" > /etc/tavily-mcp/access-keys.list
chmod 600 /etc/tavily-mcp/access-keys.list
echo "ACCESS_KEY=$ACCESS_KEY  (сохранить локально — пригодится для клиентского MCP-конфига)"

# active-key init — первый ключ из списка (после первого таймера выберется оптимальный)
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

# 7. Проверки — см. раздел «Проверки».

# 8. На клиенте: обновить MCP-конфиг:
#    "url": "https://<YOUR-HOST>.sslip.io/mcp/?accessKey=$ACCESS_KEY"
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
   certbot delete --cert-name <YOUR-HOST>.sslip.io
   apt-get purge -y nginx certbot python3-certbot-nginx python3-venv python3-pip
   ```

## Заметки по безопасности

- Эндпоинт считается публично обнаружимым (сканеры + CT-логи). Защита: whitelist access-ключей (без валидного ключа — 401), Tavily-ключи никогда не покидают VPS, ключи не пишутся в логи (формат nginx `mcp_nosecret` без query string; backend логирует только метаданные).
- **Компрометация access-ключа** = любой может расходовать Tavily-квоту. Лечится ротацией (см. Troubleshooting). Префикс `tvmcp_` облегчает grep по журналам.
- **Компрометация Tavily-ключа** = потеря месячной квоты этого аккаунта. Перевыпуск: app.tavily.com.
- В git не должны попадать реальные Tavily-ключи, access-ключи, IP-адреса VPS и имена аккаунтов — только плейсхолдеры.

## Файлы и ссылки (на VPS)

| Где | Что |
|---|---|
| `/etc/nginx/sites-available/tavily-mcp` | nginx-конфиг |
| `/etc/letsencrypt/live/<YOUR-HOST>.sslip.io/` | Сертификат LE |
| `/etc/tavily-mcp/` | Ключи и active-key (см. таблицу выше) |
| `/opt/tavily-mcp/` | backend.py, quota_checker.py, venv, requirements.txt |
| `/etc/systemd/system/tavily-mcp-backend.service` | Юнит бэкенда |
| `/etc/systemd/system/tavily-mcp-quota.{service,timer}` | Расписание 05:00 MSK |
| `/var/log/nginx/mcp_access.log` | Access-лог nginx (без секретов) |
| `/var/log/nginx/mcp_error.log` | nginx error |
| `journalctl -u tavily-mcp-backend` | Лог бэкенда (без секретов) |
| `journalctl -u tavily-mcp-quota` | Лог quota-checker'а |

## Лицензия

MIT (заглушка — поменяйте по желанию).
