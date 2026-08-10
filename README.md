# tavily-mcp-proxy

Reverse-proxy for the [Tavily MCP](https://www.tavily.com) endpoint, designed to:

1. **Bypass geo-blocking** for clients on sanctioned-country IPs (RU, etc.) — the proxy lives on a VPS in a country where Tavily is reachable and forwards requests.
2. **Rotate across multiple Tavily API keys** by picking the one with the largest remaining quota, automatically once per day. When all quota checks fail, the proxy returns **HTTP 503** to every client (fail-loud) until the situation recovers.
3. **Authenticate clients** by an `accessKey` query parameter so that the real Tavily API keys never reach client machines and aren't exposed in client config files.

```
┌──────────┐    ?accessKey=tvmcp_…     ┌─────────┐    ?tavilyApiKey=<active>    ┌────────┐
│  Client  │ ───────────────────────► │  nginx  │ ───────────────────────────► │ Tavily │
└──────────┘     TLS + fail2ban        │  (TLS)  │                              └────────┘
                                       └────┬────┘
                                            │ http://127.0.0.1:8741
                                            ▼
                              ┌──────────────────────────────┐
                              │  tavily-mcp-backend (aiohttp) │
                              │   • checks accessKey         │
                              │   • picks active Tavily key  │
                              │   • streams SSE upstream     │
                              └──────────────────────────────┘
                                            ▲
                                            │ daily 05:00 Europe/Moscow
                                            │
                              ┌──────────────────────────────┐
                              │   quota_checker.py           │
                              │   • GET /usage per key       │
                              │   • writes active-key atomically │
                              └──────────────────────────────┘
```

## What's in this repository

| Path | Purpose |
|---|---|
| `backend.py` | aiohttp service that authenticates clients and proxies `/mcp/*` to Tavily with the active key |
| `quota_checker.py` | Daily selector: picks the Tavily key with the most remaining quota, ties broken lexicographically |
| `requirements.txt` | `aiohttp>=3.9` |
| `deploy/tavily-mcp-backend.service` | systemd unit for the backend |
| `deploy/tavily-mcp-quota.{service,timer}` | systemd oneshot + 05:00 Europe/Moscow timer |
| `deploy/tavily-mcp.nginx.conf` | nginx TLS + ACME + reverse proxy template (`YOUR-HOST.sslip.io` placeholder) |
| `deploy/tavily-keys.list.example` | Format example for `/etc/tavily-mcp/tavily-keys.list` (no real keys) |
| `deploy/access-keys.list.example` | Format example for `/etc/tavily-mcp/access-keys.list` (no real keys) |
| `AGENTS.md` | Full deployment guide, troubleshooting, security notes, and runbook |

## Quick start (TL;DR)

See `AGENTS.md` for the full runbook. The shortest path is:

```bash
# On the VPS (Ubuntu 24.04) — see AGENTS.md for details
apt-get install -y nginx certbot python3-certbot-nginx fail2ban python3-venv python3-pip

mkdir -p /etc/tavily-mcp /opt/tavily-mcp
# populate /etc/tavily-mcp/tavily-keys.list and access-keys.list (see deploy/*.example)

cd /opt/tavily-mcp
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp deploy/tavily-mcp-backend.service deploy/tavily-mcp-quota.service \
   deploy/tavily-mcp-quota.timer /etc/systemd/system/
cp deploy/tavily-mcp.nginx.conf /etc/nginx/sites-available/tavily-mcp
# edit YOUR-HOST.sslip.io in the nginx config, then:
ln -sf /etc/nginx/sites-available/tavily-mcp /etc/nginx/sites-enabled/tavily-mcp
nginx -t && systemctl reload nginx

systemctl daemon-reload
systemctl enable --now tavily-mcp-backend tavily-mcp-quota.timer
```

Configure your MCP client (e.g. OpenCode, Claude Desktop, MCP CLI) with:

```
https://YOUR-HOST.sslip.io/mcp/?accessKey=tvmcp_<your-generated-key>
```

## Configuration files on the server

| Path | Mode | Format | Purpose |
|---|---|---|---|
| `/etc/tavily-mcp/tavily-keys.list` | 0600 | one Tavily key per line | All keys the quota-checker rotates across |
| `/etc/tavily-mcp/access-keys.list` | 0600 | `<name><whitespace><key>` per line | Access keys clients must present |
| `/etc/tavily-mcp/active-key` | 0644 | one line — the currently active Tavily key | Read fresh on every backend request |
| `/etc/tavily-mcp/active-key.status` | 0644 | `ok` or `unknown` | Tells the backend whether to serve or 503 |

`active-key` is rewritten **atomically** (`tempfile → fsync → os.replace`) by `quota_checker.py`. The backend reads it on every request, so rotation takes effect without restarting anything.

## Generate an access key

```bash
python3 -c 'import secrets, base64; print("tvmcp_" + base64.urlsafe_b64encode(secrets.token_bytes(36)).decode().rstrip("="))'
```

After changing `/etc/tavily-mcp/access-keys.list`:

```bash
kill -HUP $(pgrep -f backend.py)
```

…or restart the systemd service.

## Security highlights

- Real Tavily keys live only on the VPS (0600, root). They are never sent to clients.
- Real access keys (`tvmcp_…`) live only on the VPS and in client config files you control — never commit them to a public repo.
- nginx `mcp_nosecret` log format omits the query string, so neither Tavily keys nor access keys are written to access logs.
- **fail2ban** (permanent bans via `nftables-allports`, see `AGENTS.md` → «Защита VPS» for details):
  - `sshd`: 5 failed SSH logins / 24 h → total ban (all TCP).
  - `nginx-scan`: 1 request to scanner paths (`.env`, `wp-admin`, `xmlrpc.php`, `phpmyadmin`, …) → total ban.
  - `tavily-mcp`: 5 × HTTP 401 on `/mcp/` / 24 h → permanent ban on ports 80/443.
  - SSH (port 22) is never banned. Unban manually: `ssh vps 'fail2ban-client unban <IP>'`.
  - **Self-ban hazard:** 5 wrong SSH attempts (stale key in `ssh-agent`) or 1 stray `curl` to a scanner path locks you out of the VPS from your home IP. Recover via mobile hotspot / VPN / hoster web console, then unban.

## HTTPS / domain

The proxy needs a public HTTPS endpoint. Setup (see `AGENTS.md` → «Домен и сертификат» and runbook for full details):

1. **Free wildcard DNS**: point `<YOUR-VPS-IP-WITH-DASHES>.sslip.io` (or `nip.io`) at your VPS — these services resolve `<anything>.<sslip-or-nip>.io` to whatever IP is in the name, no DNS account required.
2. **Let's Encrypt certificate** via `certbot --nginx -d <YOUR-HOST>.sslip.io` (HTTP-01 challenge on port 80).
3. **Auto-renewal** is handled by `certbot.timer` (renews ~30 days before expiry).
4. Mirror at `nip.io` is used as a fallback if Let's Encrypt rate-limits the primary hostname.

The hostname is visible in public Certificate Transparency logs — that's expected; security relies on the secrecy of the keys (`accessKey` for clients, real Tavily keys on the VPS only).

## License

MIT (placeholder — adjust to your preference).