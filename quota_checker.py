#!/usr/bin/env python3
"""Pick the Tavily key with the largest remaining quota (ties: lex-first key).

Reads /etc/tavily-mcp/tavily-keys.list, queries https://api.tavily.com/usage
for each, parses ``account.plan_limit - account.plan_usage`` as remaining,
then atomically writes the chosen key to /etc/tavily-mcp/active-key and
sets /etc/tavily-mcp/active-key.status accordingly.

Exit codes:
  0  selection successful (or all keys had identical remaining=0)
  2  failed to obtain quota for ANY key -> active-key.status="unknown"
     (active-key is emptied so the backend returns 503)
"""
import json
import logging
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TAVILY_MCP_CONFIG_DIR", "/etc/tavily-mcp"))
KEYS_FILE = CONFIG_DIR / "tavily-keys.list"
ACTIVE_KEY_FILE = CONFIG_DIR / "active-key"
ACTIVE_KEY_STATUS = CONFIG_DIR / "active-key.status"
USAGE_URL = "https://api.tavily.com/usage"
TIMEOUT = 10

log = logging.getLogger("tavily-mcp-quota")


def read_keys():
    if not KEYS_FILE.exists():
        log.error("keys file not found: %s", KEYS_FILE)
        return []
    out = []
    for line in KEYS_FILE.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def fetch_quota(key):
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {key}", "User-Agent": "tavily-mcp-quota/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        log.warning("HTTP %s for key %s…", e.code, key[:14])
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("network error for key %s…: %s", key[:14], e)
        return None
    if status != 200:
        log.warning("non-200 %s for key %s…", status, key[:14])
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("bad JSON for key %s…: %s", key[:14], e)
        return None
    account = data.get("account") or {}
    plan_limit = account.get("plan_limit")
    plan_usage = account.get("plan_usage")
    if not isinstance(plan_limit, (int, float)) or not isinstance(plan_usage, (int, float)):
        log.warning("missing/invalid plan fields for key %s…: %s", key[:14], account)
        return None
    return {"remaining": plan_limit - plan_usage, "used": plan_usage, "limit": plan_limit}


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    keys = read_keys()
    if not keys:
        log.error("no tavily keys configured")
        atomic_write(ACTIVE_KEY_FILE, "")
        atomic_write(ACTIVE_KEY_STATUS, "unknown\n")
        return 2

    results = []
    for k in keys:
        info = fetch_quota(k)
        if info is not None:
            results.append((k, info))
        else:
            log.warning("skipping key %s… (quota unavailable)", k[:14])

    if not results:
        log.error("no quota data for any of %d keys -> fail-loud (503)", len(keys))
        atomic_write(ACTIVE_KEY_FILE, "")
        atomic_write(ACTIVE_KEY_STATUS, "unknown\n")
        return 2

    # Sort: max remaining first; tie -> lex-first key string
    results.sort(key=lambda r: (-r[1]["remaining"], r[0]))
    chosen_key, chosen_info = results[0]

    atomic_write(ACTIVE_KEY_FILE, chosen_key + "\n")
    atomic_write(ACTIVE_KEY_STATUS, "ok\n")

    log.info(
        "selected key=%s… remaining=%s (used=%s/%s); considered %d/%d",
        chosen_key[:14],
        chosen_info["remaining"],
        chosen_info["used"],
        chosen_info["limit"],
        len(results),
        len(keys),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.exception("unhandled: %s", e)
        try:
            atomic_write(ACTIVE_KEY_FILE, "")
            atomic_write(ACTIVE_KEY_STATUS, "unknown\n")
        except Exception:
            pass
        sys.exit(2)