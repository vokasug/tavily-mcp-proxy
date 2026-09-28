# tavily-mcp-proxy

Reverse-прокси для эндпоинта [Tavily MCP](https://www.tavily.com), который:

1. **Обходит гео-блокировку** для клиентов на IP из санкционных территорий (RU и др.) — прокси живёт на VPS в стране, откуда Tavily доступен, и пересылает запросы.
2. **Ротирует несколько Tavily API-ключей**, выбирая раз в сутки ключ с наибольшим остатком квоты. Если ни один ключ не удалось проверить — прокси возвращает **HTTP 503** всем клиентам (fail-loud) до восстановления.
3. **Аутентифицирует клиентов** по query-параметру `accessKey`, чтобы реальные Tavily-ключи никогда не попадали на клиентские машины и не светились в клиентских конфигах.

```
┌──────────┐    ?accessKey=tvmcp_…     ┌─────────────┐    ?tavilyApiKey=<active>    ┌────────┐
│  Клиент  │ ────────────────────────► │    nginx    │ ───────────────────────────► │ Tavily │
└──────────┘     TLS + fail2ban        │    (TLS)    │                              └────────┘
                                       └─────┴───────┘
                                             │ http://127.0.0.1:8741
                                             ▼
                         ┌────────────────────────────────────────┐
                         │      tavily-mcp-backend (aiohttp)      │
                         │          • проверяет accessKey         │
                         │   • подставляет активный Tavily-ключ   │
                         │         • стримит SSE upstream         │
                         └────────────────────────────────────────┘
                                   ежедневно в 05:00 Europe/Moscow
                                             ▲
                         ┌────────────────────────────────────────┐
                         │            quota_checker.py            │
                         │       • GET /usage на каждый ключ      │
                         │       • атомарно пишет active-key      │
                         └────────────────────────────────────────┘
```

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
| `AGENTS.md` | Полное руководство: развёртывание, troubleshooting, безопасность, runbook |

## Быстрый старт (TL;DR)

Полный runbook — в `AGENTS.md`. Самый короткий путь:

```bash
# На VPS (Ubuntu 24.04) — детали в AGENTS.md
apt-get install -y nginx certbot python3-certbot-nginx fail2ban python3-venv python3-pip

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

## Конфигурационные файлы на сервере

| Путь | Права | Формат | Назначение |
|---|---|---|---|
| `/etc/tavily-mcp/tavily-keys.list` | 0600 | по одному Tavily-ключу на строку | Все ключи, между которыми ротирует quota-checker |
| `/etc/tavily-mcp/access-keys.list` | 0600 | `<имя><пробел/таб><ключ>` на строку | Access-ключи клиентов |
| `/etc/tavily-mcp/active-key` | 0644 | одна строка — текущий активный Tavily-ключ | Читается заново на каждом запросе бэкенда |
| `/etc/tavily-mcp/active-key.status` | 0644 | `ok` или `unknown` | Говорит бэкенду: обслуживать или отдавать 503 |

`active-key` переписывается **атомарно** (`tempfile → fsync → os.replace`) скриптом `quota_checker.py`. Бэкенд читает файл на каждый запрос, поэтому переключение ключа происходит без рестарта.

## Генерация access-ключа

```bash
python3 -c 'import secrets, base64; print("tvmcp_" + base64.urlsafe_b64encode(secrets.token_bytes(36)).decode().rstrip("="))'
```

После изменения `/etc/tavily-mcp/access-keys.list`:

```bash
kill -HUP $(pgrep -f backend.py)
```

…или перезапустить systemd-сервис.

## Безопасность

- Реальные Tavily-ключи живут только на VPS (0600, root). Клиентам никогда не передаются.
- Реальные access-ключи (`tvmcp_…`) живут только на VPS и в клиентских конфигах под вашим контролем — не коммитьте их в публичные репозитории.
- Формат лога nginx `mcp_nosecret` не пишет query string, поэтому ни Tavily-ключи, ни access-ключи в access-логе не оседают.
- Бэкенд логирует в journal каждый принятый запрос с **именем** access-ключа (не самим ключом): `req: key=<имя> client=<ip> …` — это даёт статистику использования по ключам (`journalctl -u tavily-mcp-backend | grep -c 'req: key=<имя>'`). Встроенный access-log aiohttp отключён — он слил бы `accessKey` из query string.
- **fail2ban** банит **IP-адрес источника** перманентно через `nftables-allports` (подробности — в `AGENTS.md` → «Защита VPS»):
  - `sshd`: 5 неудачных SSH-логинов с одного IP за 24 ч → IP блокируется на всех TCP-портах.
  - `nginx-scan`: 1 запрос с IP к путям сканеров (`.env`, `wp-admin`, `xmlrpc.php`, `phpmyadmin`, …) → IP блокируется на всех TCP-портах.
  - `tavily-mcp`: 5 × HTTP 401 с одного IP на `/mcp/` за 24 ч → IP блокируется на портах 80/443 (остальные порты работают).
  - Порт 22 (SSH) не банится ни в одном джейле — зайти по SSH можно всегда, в том числе чтобы разбанить себя. Разбан вручную: `ssh vps 'fail2ban-client unban <забаненный-IP>'`.
  - **Самобан — критично:** 5 неудачных SSH-попыток (протухший ключ в `ssh-agent`) или 1 случайный `curl` к пути сканера = ваш домашний IP уходит в перманентный бан → потеря доступа к VPS. Восстановление: мобильный хотспот / VPN / веб-консоль хостера (зайти с другого IP), затем `fail2ban-client unban <свой-IP>`.

## HTTPS / домен

Прокси требует публичный HTTPS-эндпоинт. Полный runbook — в `AGENTS.md` → «Домен и сертификат» и «Восстановление с нуля»; здесь — короткая версия.

1. **Wildcard-DNS через публичные сервисы.** `sslip.io` и `nip.io` — это разные сервисы (НЕ зеркала друг друга), оба резолвят имя вида `<IP-С-ДЕФИСАМИ>.<service>.io` в IP, зашитый в имя. Регистрация в DNS не нужна.
2. **Сертификат Let's Encrypt** через `certbot --nginx -d <YOUR-HOST>.sslip.io` (HTTP-01 challenge на 80 порту). Файлы лежат в `/etc/letsencrypt/live/<YOUR-HOST>.../`. **Срок действия — 90 дней** (стандарт Let's Encrypt).
3. **Автопродление** — `certbot.timer` (systemd): каждые ~12 часов проверяет, осталось ли <15 дней до истечения, и автоматически перевыпускает сертификат (на практике — на 75-й день жизни). Порог задан в `/etc/letsencrypt/renewal/<domain>.conf` через `renew_before_expiry = 15 days`. Если таймер по какой-то причине не сработал — `certbot renew` вручную.
4. **Когда LE упирается в rate-limit** (5 сертификатов в неделю на один домен): сгенерировать имя `<IP-С-ДЕФИСАМИ>.nip.io`, получить сертификат для него, обновить `server_name` в nginx, обновить URL в MCP-клиенте. Это **разные DNS-сервисы**, поэтому для LE это разные домены — rate-limit sslip.io не действует на nip.io.

### ⚠️ Что ломает работу MCP-клиента

Имя `<IP-С-ДЕФИСАМИ>.sslip.io` привязано к конкретному IP-адресу VPS. Если **IP VPS меняется** (переустановка у хостера, смена тарифа, миграция), то:

- Резолв старого имени перестаёт указывать на новый IP.
- Старый сертификат остаётся валидным по дате, но для другого IP он бесполезен (TLS-хендшейк успешный, но клиент обращается к чужому IP).
- **OpenCode (и любой MCP-клиент) перестаёт работать до тех пор, пока на клиентской машине не обновится URL** на новое имя.

Порядок действий при смене IP: сгенерировать новое имя `<НОВЫЙ-IP-С-ДЕФИСАМИ>.sslip.io` (или `.nip.io`) → `certbot --nginx -d <НОВОЕ-ИМЯ>` на VPS → обновить `mcp.tavily.url` в `~/.config/opencode/opencode.jsonc` на Mac → перезапустить сессию OpenCode. Полный чеклист — в `AGENTS.md` → Troubleshooting → «IP VPS сменился».

Имя хоста видно в публичных логах Certificate Transparency — это нормально. Безопасность держится на секретности ключей (`accessKey` у клиентов, реальные Tavily-ключи — только на VPS).

## Лицензия

MIT (заглушка — поменяйте по желанию).
