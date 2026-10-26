#!/usr/bin/env python3
"""Pick the Tavily key with the largest remaining quota (ties: lex-first key).

Reads /etc/tavily-mcp/tavily-keys.list, queries https://api.tavily.com/usage
for each, parses ``account.plan_limit - account.plan_usage`` as remaining,
then atomically writes the chosen key to /etc/tavily-mcp/active-key and
sets /etc/tavily-mcp/active-key.status accordingly.

Exit codes:
  0  selection successful (or all keys had identical remaining=0)
  2  failed to obtain quota for ANY key; a valid active key is preserved
     after temporary errors, otherwise active-key.status="unknown" and
     active-key is emptied so the backend returns 503
"""
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TAVILY_MCP_CONFIG_DIR", "/etc/tavily-mcp"))
KEYS_FILE = CONFIG_DIR / "tavily-keys.list"
ACTIVE_KEY_FILE = CONFIG_DIR / "active-key"
ACTIVE_KEY_STATUS = CONFIG_DIR / "active-key.status"
USAGE_URL = "https://api.tavily.com/usage"
TIMEOUT = 30
RETRY_DELAYS = (2, 5)

log = logging.getLogger("tavily-mcp-quota")


class QuotaFetchError(Exception):
    def __init__(self, message, transient):
        super().__init__(message)
        self.transient = transient


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
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            transient = e.code == 429 or 500 <= e.code < 600
            if not transient:
                log.warning("HTTP %s for key %s…", e.code, key[:14])
                raise QuotaFetchError(f"HTTP {e.code}", transient=False) from e
            error = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            error = str(e)
        else:
            if status == 200:
                break
            transient = status == 429 or 500 <= status < 600
            if not transient:
                log.warning("non-200 %s for key %s…", status, key[:14])
                raise QuotaFetchError(f"HTTP {status}", transient=False)
            error = f"HTTP {status}"
        if attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt]
            log.warning(
                "temporary error for key %s…: %s; retrying in %ds",
                key[:14], error, delay,
            )
            time.sleep(delay)
        else:
            log.warning("temporary error for key %s…: %s", key[:14], error)
            raise QuotaFetchError(error, transient=True)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("bad JSON for key %s…: %s", key[:14], e)
        raise QuotaFetchError("invalid JSON", transient=False) from e
    account = data.get("account") or {}
    plan_limit = account.get("plan_limit")
    plan_usage = account.get("plan_usage")
    if not isinstance(plan_limit, (int, float)) or not isinstance(plan_usage, (int, float)):
        log.warning("missing/invalid plan fields for key %s…: %s", key[:14], account)
        raise QuotaFetchError("missing/invalid plan fields", transient=False)
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


def has_active_key():
    try:
        return (
            ACTIVE_KEY_STATUS.read_text().strip() == "ok"
            and bool(ACTIVE_KEY_FILE.read_text().strip())
        )
    except OSError:
        return False


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
    failures = []
    for k in keys:
        try:
            info = fetch_quota(k)
        except QuotaFetchError as e:
            log.warning("skipping key %s… (quota unavailable: %s)", k[:14], e)
            failures.append(e)
            continue
        log.info(
            "key=%s… remaining=%s (used=%s/%s)",
            k[:14], info["remaining"], info["used"], info["limit"],
        )
        results.append((k, info))

    if not results:
        if failures and all(e.transient for e in failures) and has_active_key():
            log.error(
                "quota refresh failed temporarily for all %d keys; keeping active key",
                len(keys),
            )
            return 2
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