# tavily-mcp-proxy

Reverse-прокси для эндпоинта [Tavily MCP](https://www.tavily.com), который:

1. **Обходит гео-блокировку** для клиентов на IP из санкционных территорий (RU и др.) — прокси живёт на VPS в стране, откуда Tavily доступен, и пересылает запросы.
2. **Ротирует несколько Tavily API-ключей**, выбирая раз в сутки ключ с наибольшим остатком квоты. Если ни один ключ не удалось проверить — прокси возвращает **HTTP 503** всем клиентам (fail-loud) до восстановления.
3. **Аутентифицирует клиентов** по query-параметру `accessKey`, чтобы реальные Tavily-ключи никогда не попадали на клиентские машины и не светились в клиентских конфигах.

```
┌──────────┐    ?accessKey=tvmcp_…     ┌─────────┐    ?tavilyApiKey=<active>    ┌────────┐
│ Клиент   │ ───────────────────────► │  nginx  │ ───────────────────────────► │ Tavily │
└──────────┘     TLS + fail2ban        │  (TLS)  │                              └────────┘
                                       └────┬────┘
                                            │ http://127.0.0.1:8741
                                            ▼
                              ┌──────────────────────────────┐
                              │  tavily-mcp-backend (aiohttp) │
                              │   • проверяет accessKey      │
                              │   • подставляет активный     │
                              │     Tavily-ключ              │
                              │   • стримит SSE upstream     │
                              └──────────────────────────────┘
                                            ▲
                                            │ ежедневно в 05:00 Europe/Moscow
                                            │
                              ┌──────────────────────────────┐
                              │   quota_checker.py           │
                              │   • GET /usage на каждый ключ│
                              │   • атомарно пишет active-key│
                              └──────────────────────────────┘
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
- **fail2ban** (перманентные баны через `nftables-allports`, подробности — в `AGENTS.md` → «Защита VPS»):
  - `sshd`: 5 неудачных SSH-логинов за 24 ч → тотальный бан (весь TCP).
  - `nginx-scan`: 1 запрос к путям сканеров (`.env`, `wp-admin`, `xmlrpc.php`, `phpmyadmin`, …) → тотальный бан.
  - `tavily-mcp`: 5 × HTTP 401 на `/mcp/` за 24 ч → перманентный бан портов 80/443.
  - Порт 22 (SSH) не банится никогда. Разбан вручную: `ssh vps 'fail2ban-client unban <IP>'`.
  - **Самобан — критично:** 5 неудачных SSH-попыток (протухший ключ в `ssh-agent`) или 1 случайный `curl` к пути сканера = потеря доступа к VPS с вашего домашнего IP. Восстановление: мобильный хотспот / VPN / веб-консоль хостера, затем разбан.

## HTTPS / домен

Прокси требует публичный HTTPS-эндпоинт. Настройка (полные детали — в `AGENTS.md` → «Домен и сертификат» и runbook):

1. **Бесплатный wildcard-DNS**: имя `<IP-С-ДЕФИСАМИ>.sslip.io` (или `nip.io`) автоматически резолвится в IP, зашитый в имя. Регистрация в DNS не нужна.
2. **Сертификат Let's Encrypt** через `certbot --nginx -d <YOUR-HOST>.sslip.io` (HTTP-01 challenge на 80 порту).
3. **Автопродление** — `certbot.timer` (обновляет примерно за 30 дней до истечения).
4. Зеркало на `nip.io` используется как запасное при rate-limit Let's Encrypt на основной домен.

Имя хоста видно в публичных логах Certificate Transparency — это нормально. Безопасность держится на секретности ключей (`accessKey` у клиентов, реальные Tavily-ключи — только на VPS).

## Лицензия

MIT (заглушка — поменяйте по желанию).
