#!/usr/bin/env python3
"""plasma-ai-quota collector.

Reads the usage / quota of your AI coding subscriptions from the same endpoints their
official CLIs and dashboards use, and prints one JSON document for the Plasma widget.

Services: Claude Code, OpenAI Codex, OpenCode Go, Command Code, Replit.

Privacy: credentials are read from the files each CLI already keeps on disk (or, for Replit,
from your browser's session cookie) and are only ever sent to that service's own API.
Tokens and cookies are never printed, logged or written to the cache.

Stdlib only, except `cryptography` (optional, Replit cookie decryption).
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

VERSION = 2
UA = "plasma-ai-quota/1.0"
TIMEOUT = 10
HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "plasma-ai-quota")
CACHE_FILE = os.path.join(CACHE_DIR, "status.json")
LOCK_FILE = os.path.join(CACHE_DIR, ".lock")
CONFIG_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")),
                           "plasma-ai-quota", "config.json")

DEFAULT_CONFIG = {
    "cache_seconds": 300,          # hit the network at most every 5 minutes
    "user_agent": None,            # optional UA for browser-session requests (Replit)
    "claude": {"enabled": True},
    "codex": {"enabled": True},
    "opencode": {"enabled": True, "org_id": None},   # org id: see README
    "commandcode": {"enabled": True},
    "replit": {"enabled": True, "browser": "auto"},  # auto | helium | chrome | chromium | brave | vivaldi
}


# ───────────────────────── helpers ─────────────────────────

class HttpError(Exception):
    def __init__(self, code: int, retry_after: float | None = None):
        super().__init__(f"HTTP {code}")
        self.code = code
        self.retry_after = retry_after


class Problem(Exception):
    """A user-facing problem: `code` is translated by the widget, `detail` is shown as-is."""
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def iso_to_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_json(path: str):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return json.load(f)


def http_json(url: str, headers: dict, data: bytes | None = None) -> dict | list:
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After")
        try:
            ra = float(ra) if ra else None
        except ValueError:
            ra = None
        raise HttpError(e.code, ra) from None
    except urllib.error.URLError as e:
        raise Problem("network", str(getattr(e, "reason", e))[:80]) from None
    except TimeoutError:
        raise Problem("network", "timeout") from None


def window(kind: str, percent, resets_ts: float | None = None, detail: str | None = None, scope: str | None = None) -> dict:
    """kind: 5h | day | week | month | extra | credit | custom"""
    p = None if percent is None else round(max(0.0, min(100.0, float(percent))), 1)
    return {"kind": kind, "scope": scope, "percent": p, "resets_at": int(resets_ts) if resets_ts else None, "detail": detail}


def service(sid: str, name: str, url: str | None) -> dict:
    return {"id": sid, "name": name, "plan": None, "state": "ok",  # ok | warn | error | no_data | disabled
            "error": None, "error_detail": None, "url": url, "source": None,
            "updated_at": int(now()), "windows": [], "info": [], "badge": None, "badge_percent": None}


def fail(s: dict, code: str, detail: str = "", state: str = "error") -> dict:
    s.update(state=state, error=code, error_detail=detail or None)
    return s


def error_of(e: Exception) -> tuple[str, str]:
    if isinstance(e, Problem):
        return e.code, e.detail
    if isinstance(e, HttpError):
        if e.code in (401, 403):
            return "auth", f"HTTP {e.code}"
        if e.code == 429:
            return "rate_limited", "HTTP 429"
        return "http", f"HTTP {e.code}"
    return "unexpected", (str(e) or e.__class__.__name__)[:120]


# ───────────────────────── Claude Code ─────────────────────────
# Same endpoint as `claude` → /usage. Token from ~/.claude/.credentials.json (never refreshed here).

CLAUDE_API = "https://api.anthropic.com"
CLAUDE_BETA = "oauth-2025-04-20"


def claude_plan(profile: dict) -> str | None:
    org = profile.get("organization") or {}
    acc = profile.get("account") or {}
    tier = (org.get("rate_limit_tier") or "").lower()
    otype = (org.get("organization_type") or "").lower()
    if "max" in otype or acc.get("has_claude_max"):
        return "Max 20x" if "20x" in tier else ("Max 5x" if "5x" in tier else "Max")
    if "pro" in otype or acc.get("has_claude_pro"):
        return "Pro"
    return otype.replace("claude_", "").title() or None


def collect_claude(prev: dict | None, cfg: dict) -> dict:
    s = service("claude", "Claude Code", "https://claude.ai/settings/usage")
    s["source"] = "api.anthropic.com/api/oauth/usage"
    try:
        cred = read_json("~/.claude/.credentials.json").get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return fail(s, "not_logged_in", "claude")
    token = cred.get("accessToken")
    if not token:
        return fail(s, "not_logged_in", "claude")
    if (cred.get("expiresAt") or 0) / 1000 < now():
        s["windows"] = (prev or {}).get("windows", [])
        s["plan"] = (prev or {}).get("plan")
        return fail(s, "token_expired", "claude", state="warn")
    h = {"Authorization": f"Bearer {token}", "anthropic-beta": CLAUDE_BETA}
    plan, plan_ts = (prev or {}).get("plan"), (prev or {}).get("_plan_ts", 0)
    if not plan or now() - plan_ts > 86400:
        try:
            plan, plan_ts = claude_plan(http_json(CLAUDE_API + "/api/oauth/profile", h)) or plan, now()
        except Exception:  # noqa: BLE001 — plan name is cosmetic
            pass
    s["plan"], s["_plan_ts"] = plan, plan_ts
    try:
        d = http_json(CLAUDE_API + "/api/oauth/usage", h)
    except HttpError as e:
        if e.code == 401:
            return fail(s, "token_expired", "claude", state="warn")
        raise
    finally:
        del h, token

    limits = d.get("limits")
    if isinstance(limits, list) and limits:
        for lim in limits:
            ts = iso_to_ts(lim.get("resets_at"))
            k = lim.get("kind")
            if k == "session":
                s["windows"].append(window("5h", lim.get("percent"), ts))
            elif k == "weekly_all":
                s["windows"].append(window("week", lim.get("percent"), ts))
            elif k == "weekly_scoped":
                sc = lim.get("scope") or {}
                name = (sc.get("model") or {}).get("display_name") or (sc.get("surface") or {}).get("display_name")
                s["windows"].append(window("week", lim.get("percent"), ts, scope=name))
            else:
                s["windows"].append(window("custom", lim.get("percent"), ts, scope=str(k)))
    else:
        for key, kind, scope in (("five_hour", "5h", None), ("seven_day", "week", None),
                                 ("seven_day_opus", "week", "Opus"), ("seven_day_sonnet", "week", "Sonnet")):
            w = d.get(key)
            if isinstance(w, dict) and w.get("utilization") is not None:
                s["windows"].append(window(kind, w["utilization"], iso_to_ts(w.get("resets_at")), scope=scope))
    ex = d.get("extra_usage") or {}
    if ex.get("is_enabled") and ex.get("utilization") is not None:
        s["windows"].append(window("extra", ex["utilization"]))
    return s if s["windows"] else fail(s, "no_windows", state="no_data")


# ───────────────────────── OpenAI Codex ─────────────────────────
# Same endpoint as `codex` → /status (ChatGPT sign-in). Falls back to the last `rate_limits`
# event in ~/.codex/sessions (offline).

def jwt_exp(tok: str) -> float | None:
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return float(json.loads(base64.urlsafe_b64decode(p)).get("exp"))
    except Exception:  # noqa: BLE001
        return None


def codex_kind(minutes: float | None) -> tuple[str, str | None]:
    if not minutes:
        return "custom", None
    if abs(minutes - 300) < 1:
        return "5h", None
    if abs(minutes - 10080) < 1:
        return "week", None
    if minutes >= 40000:
        return "month", None
    h = minutes / 60
    return "custom", (f"{h:g}h" if h < 48 else f"{h / 24:g}d")


def codex_from_sessions() -> tuple[dict | None, float | None]:
    files = glob.glob(os.path.join(HOME, ".codex/sessions/**/rollout-*.jsonl"), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    for path in files[:20]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            rl = (o.get("payload") or {}).get("rate_limits")
            if isinstance(rl, dict) and (rl.get("primary") or rl.get("secondary")):
                return rl, iso_to_ts(o.get("timestamp"))
    return None, None


def collect_codex(prev: dict | None, cfg: dict) -> dict:
    s = service("codex", "Codex", "https://chatgpt.com/codex/settings/usage")
    live_err = None
    try:
        a = read_json("~/.codex/auth.json")
        tok = (a.get("tokens") or {}).get("access_token")
        acc = (a.get("tokens") or {}).get("account_id")
        if a.get("auth_mode") != "chatgpt" or not tok:
            raise Problem("not_logged_in", "codex")
        if (jwt_exp(tok) or now() + 1) < now():
            raise Problem("token_expired", "codex")
        h = {"Authorization": f"Bearer {tok}"}
        if acc:
            h["ChatGPT-Account-Id"] = acc
        try:
            d = http_json("https://chatgpt.com/backend-api/wham/usage", h)
        finally:
            del h, tok
        s["source"] = "chatgpt.com/backend-api/wham/usage"
        s["plan"] = (d.get("plan_type") or "").title() or None
        rl = d.get("rate_limit") or {}
        for key in ("primary_window", "secondary_window"):
            w = rl.get(key)
            if isinstance(w, dict):
                kind, scope = codex_kind((w.get("limit_window_seconds") or 0) / 60)
                s["windows"].append(window(kind, w.get("used_percent"), w.get("reset_at"), scope=scope))
        if rl.get("limit_reached"):
            fail(s, "limit_reached", state="warn")
        if s["windows"]:
            return s
        live_err = ("no_windows", "")
    except HttpError as e:
        if e.code == 429:
            raise
        live_err = error_of(e)
    except Exception as e:  # noqa: BLE001
        live_err = error_of(e)

    rl, ts = codex_from_sessions()
    if not rl:
        return fail(s, *(live_err or ("no_data", "")))
    s["source"] = "~/.codex/sessions (last rate_limits event)"
    s["plan"] = (rl.get("plan_type") or "").title() or None
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict):
            kind, scope = codex_kind(w.get("window_minutes"))
            r, p = w.get("resets_at"), w.get("used_percent")
            if r and r < now():
                p = 0.0
            s["windows"].append(window(kind, p, r, scope=scope))
    return fail(s, "stale_local", datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "", state="warn")


# ───────────────────────── OpenCode Go ─────────────────────────
# Same endpoint as the opencode.ai console. Needs an API key with Billing:Read in
# ~/.local/share/opencode/auth.json (what `opencode auth login` writes) and your org id.

OC_GO_STATUS = "https://opencode.ai/console/api/go/status"


def collect_opencode(prev: dict | None, cfg: dict) -> dict:
    s = service("opencode", "OpenCode", "https://opencode.ai/workspace")
    s["source"] = "opencode.ai/console/api/go/status"
    try:
        key = read_json("~/.local/share/opencode/auth.json")["opencode"]["key"]
    except (OSError, ValueError, KeyError, TypeError):
        return fail(s, "not_logged_in", "opencode auth login", state="no_data")
    org = (cfg.get("opencode") or {}).get("org_id")
    if not org:
        return fail(s, "needs_config", "opencode.org_id", state="no_data")
    try:
        d = http_json(OC_GO_STATUS, {"Authorization": f"Bearer {key}", "x-org-id": org})
    finally:
        del key
    m = (d.get("access") or {}).get("meters") or {}
    s["plan"] = (d.get("product") or "go").title()
    for key_, kind in (("fiveHour", "5h"), ("week", "week"), ("month", "month")):
        x = m.get(key_) or {}
        lim, used = int(x.get("limitMicroCents") or 0), int(x.get("usedMicroCents") or 0)
        if lim > 0:  # 1 USD = 1e8 micro-cents
            s["windows"].append(window(kind, used * 100 / lim, iso_to_ts(x.get("resetsAt")),
                                       f"${used / 1e8:.2f} / ${lim / 1e8:.2f}"))
    return s if s["windows"] else fail(s, "no_windows", state="no_data")


# ───────────────────────── Command Code ─────────────────────────
# Same endpoints as `cmd` → /usage. Key from ~/.commandcode/auth.json.

CC_API = "https://api.commandcode.ai"
# plan id → (name, monthly credit in USD); from the command-code CLI's plan table.
CC_PLANS = {
    "individual-go-v1": ("Go", 10), "individual-goat": ("GOAT", 70), "individual-go": ("Go", 10),
    "individual-pro-v1": ("Pro", 80), "individual-pro": ("Pro", 30), "individual-provider": ("Provider", 15),
    "individual-max": ("Max", 150), "individual-ultra": ("Ultra", 300), "teams-pro": ("Teams Pro", 40),
}


def cc_plan(plan_id: str | None):
    if not plan_id:
        return None
    n = plan_id.lower().replace("_", "-")
    for k in sorted(CC_PLANS, key=len, reverse=True):
        if n.startswith(k):
            return CC_PLANS[k]
    return None


def collect_commandcode(prev: dict | None, cfg: dict) -> dict:
    s = service("commandcode", "Command Code", "https://commandcode.ai/billing")
    s["source"] = "api.commandcode.ai/alpha/{whoami,billing,usage}"
    try:
        key = read_json("~/.commandcode/auth.json").get("apiKey")
    except (OSError, ValueError):
        key = None
    if not key:
        return fail(s, "not_logged_in", "cmd login")
    h = {"Authorization": f"Bearer {key}"}
    try:
        who = http_json(CC_API + "/alpha/whoami?limits=1", h)
        org = (who.get("org") or {}).get("id")
        q = f"?orgId={urllib.parse.quote(org)}" if org else ""
        cred = http_json(CC_API + "/alpha/billing/credits" + q, h)
        try:
            sub = (http_json(CC_API + "/alpha/billing/subscriptions" + q, h) or {}).get("data") or {}
        except HttpError as e:
            if e.code == 429:
                raise
            sub = {}
        summary = None
        try:
            prm = {"since": sub["currentPeriodStart"]} if sub.get("currentPeriodStart") else {}
            if org:
                prm["orgId"] = org
            summary = http_json(CC_API + "/alpha/usage/summary" + ("?" + urllib.parse.urlencode(prm) if prm else ""), h)
        except HttpError as e:
            if e.code == 429:
                raise
    finally:
        del h, key
    login = (who.get("org") or {}).get("login") or (who.get("user") or {}).get("userName")
    if login:
        s["url"] = f"https://commandcode.ai/{login}/settings/usage"
    plan = cc_plan(sub.get("planId"))
    s["plan"] = plan[0] if plan else (sub.get("planId") or None)

    wl = cred.get("windowLimits") or {}
    for k, kind in (("fiveHour", "5h"), ("weekly", "week"), ("daily", "day")):
        w = wl.get(k)
        if isinstance(w, dict) and w.get("cap"):
            s["windows"].append(window(kind, 100.0 * (w.get("used") or 0) / w["cap"], (w.get("resetAt") or 0) / 1000 or None,
                                       f"${w.get('used') or 0:.2f} / ${w['cap']:.2f}"))
    c = cred.get("credits") or {}
    monthly, bought, free = (max(0.0, c.get(x) or 0) for x in ("monthlyCredits", "purchasedCredits", "freeCredits"))
    left = monthly + bought + free
    spent = max(0.0, (summary or {}).get("totalCost") or 0)
    plan_monthly = plan[1] if (plan and sub.get("status") == "active") else None
    pool = (max(plan_monthly, monthly) + bought + free) if plan_monthly is not None else spent + left
    if pool > 0:
        s["windows"].append(window("credit", 100.0 * (pool - left) / pool, iso_to_ts(sub.get("currentPeriodEnd")),
                                   f"${left:.2f} / ${pool:.2f}"))
    if summary:
        s["info"].append({"key": "period_requests", "value": f"{summary.get('totalCount', 0)} · ${spent:.2f}"})
    if wl.get("exceeded"):
        fail(s, "limit_reached", str(wl.get("exceeded")), state="warn")
    return s if s["windows"] else fail(s, "no_windows", state="no_data")


# ───────────────────────── Replit ─────────────────────────
# No CLI / public API: uses replit.com's own GraphQL (persisted queries, read-only) with the
# `connect.sid` session cookie read fresh from your Chromium-based browser on every run.

BROWSERS = {  # name: (cookie DB, libsecret application for v11 cookies)
    "helium": ("~/.config/net.imput.helium/Default/Cookies", "chromium"),
    "chrome": ("~/.config/google-chrome/Default/Cookies", "chrome"),
    "chromium": ("~/.config/chromium/Default/Cookies", "chromium"),
    "brave": ("~/.config/BraveSoftware/Brave-Browser/Default/Cookies", "brave"),
    "vivaldi": ("~/.config/vivaldi/Default/Cookies", "vivaldi"),
}


def browser_cookie(host: str, name: str, browser: str = "auto") -> str | None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cands = BROWSERS.items() if browser == "auto" else [(browser, BROWSERS[browser])]
    for _, (db, app) in cands:
        src = os.path.expanduser(db)
        if not os.path.exists(src):
            continue
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".c-")
        os.close(fd)
        try:
            shutil.copy(src, tmp)
            con = sqlite3.connect(tmp)
            row = con.execute("select encrypted_value from cookies where host_key=? and name=?", (host, name)).fetchone()
            con.close()
        finally:
            os.unlink(tmp)
        if not row:
            continue
        ev = row[0]
        if ev[:3] == b"v10":
            pw = b"peanuts"
        elif ev[:3] == b"v11":
            pw = subprocess.run(["secret-tool", "lookup", "application", app], capture_output=True).stdout.strip()
            if not pw:
                continue
        else:
            continue
        key = hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1, 16)
        dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        v = dec.update(ev[3:]) + dec.finalize()
        v = v[:-v[-1]]
        if v[:32] == hashlib.sha256(host.encode()).digest():  # Chromium ≥ 130 domain prefix
            v = v[32:]
        try:
            return v.decode()
        except UnicodeDecodeError:
            continue
    return None


RP_GQL = "https://replit.com/graphql"
RP_ALERT = ("BillingCreditAlert", "8319eb7a8f254fea2ac7780ab3531f61170f1f5ae2b9270ca474b67958febfb6")
RP_ACCOUNT = ("AccountUsageSectionData", "05da48a1587abb8312fe2548faefb4faf9041f7dd25f996ddfc9e3f091e33b87")


def replit_query(sid: str, op: tuple[str, str], variables: dict, ua: str) -> dict:
    body = json.dumps([{"operationName": op[0], "variables": variables,
                        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": op[1]}}}]).encode()
    d = http_json(RP_GQL, {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
                           "Origin": "https://replit.com", "Referer": "https://replit.com/settings/billing",
                           "User-Agent": ua, "Cookie": "connect.sid=" + sid}, data=body)
    d = d[0] if isinstance(d, list) else d
    if d.get("errors"):
        msg = str(d["errors"][0].get("message", ""))
        raise Problem("api_changed" if "Persisted" in msg else "http", msg[:80])
    return d.get("data") or {}


def collect_replit(prev: dict | None, cfg: dict) -> dict:
    s = service("replit", "Replit", "https://replit.com/settings/billing")
    s["source"] = "replit.com/graphql (browser session, read-only)"
    rc = cfg.get("replit") or {}
    try:
        sid = browser_cookie("replit.com", "connect.sid", rc.get("browser") or "auto")
    except ImportError:
        return fail(s, "needs_dependency", "python-cryptography", state="no_data")
    if not sid:
        return fail(s, "not_logged_in", "replit.com", state="no_data")
    ua = cfg.get("user_agent") or ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
    try:
        cust = (((replit_query(sid, RP_ALERT, {}, ua).get("currentUser") or {}).get("customer")) or {}).get("id")
        if not cust:
            return fail(s, "not_logged_in", "replit.com", state="no_data")
        cr = ((replit_query(sid, RP_ACCOUNT, {"customerId": cust, "includeAccountData": True, "includeExtraUsage": False},
                            ua).get("getCustomer") or {}).get("usageCredits")) or {}
    finally:
        del sid
    left = float(cr.get("remainingCreditsUsd") or 0)
    sub = float((cr.get("subscription") or {}).get("remainingUsd") or 0)
    s["badge"] = f"${left:.0f}"
    s["badge_percent"] = 0 if left >= 10 else (70 if left >= 3 else 95)
    s["info"].append({"key": "credits_left", "value": f"${left:.2f}"})
    s["info"].append({"key": "subscription_left", "value": f"${sub:.2f}"})
    return s


# ───────────────────────── main ─────────────────────────

COLLECTORS = [("claude", collect_claude), ("codex", collect_codex), ("opencode", collect_opencode),
              ("commandcode", collect_commandcode), ("replit", collect_replit)]
NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode",
         "commandcode": "Command Code", "replit": "Replit"}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = read_json(CONFIG_FILE)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"warning: cannot read {CONFIG_FILE}: {e}", file=sys.stderr)
    return cfg


def read_cache() -> dict | None:
    try:
        return read_json(CACHE_FILE)
    except (OSError, ValueError):
        return None


def write_cache(data: dict) -> None:
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".status-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CACHE_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(cfg: dict, prev_all: dict | None) -> dict:
    prev = {s["id"]: s for s in (prev_all or {}).get("services", [])}
    out = []
    for sid, fn in COLLECTORS:
        p = prev.get(sid)
        if not (cfg.get(sid) or {}).get("enabled", True):
            out.append(fail(service(sid, NAMES[sid], None), "disabled", state="disabled"))
            continue
        if p and (p.get("_backoff_until") or 0) > now():
            out.append(dict(p, state="warn", error="rate_limited", error_detail=None))
            continue
        try:
            s = fn(p, cfg)
        except HttpError as e:
            if e.code == 429:
                step = min(((p or {}).get("_backoff_step") or 0) + 1, 4)
                s = dict(p) if p else service(sid, NAMES[sid], None)
                s["_backoff_step"] = step
                s["_backoff_until"] = now() + min(max(e.retry_after or 0, 300 * 2 ** (step - 1)), 3600)
                fail(s, "rate_limited", state="warn")
            else:
                s = _keep_old(sid, p, e)
        except Exception as e:  # noqa: BLE001 — services are independent
            s = _keep_old(sid, p, e)
        out.append(s)
    return {"version": VERSION, "updated_at": int(now()), "services": out}


def _keep_old(sid: str, p: dict | None, e: Exception) -> dict:
    code, detail = error_of(e)
    if p and (p.get("windows") or p.get("badge")):
        return dict(p, state="warn", error=code, error_detail=detail)
    return fail(service(sid, NAMES[sid], (p or {}).get("url")), code, detail)


def public(data: dict) -> dict:
    d = dict(data)
    d["services"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in data.get("services", [])]
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="AI subscription quota collector for plasma-ai-quota")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--force", action="store_true", help="ignore the cache (still ≥60 s between network runs)")
    ap.add_argument("--cached", action="store_true", help="only print the cache, no network")
    a = ap.parse_args()
    cfg = load_config()
    ttl = float(cfg.get("cache_seconds") or 300)
    prev = read_cache()

    def emit(v):
        if a.json or a.cached:
            print(json.dumps(public(v), ensure_ascii=False))

    if a.cached and prev:
        emit(prev)
        return 0
    age = now() - ((prev or {}).get("updated_at") or 0)
    if prev and age < ttl and (not a.force or age < 60):
        emit(prev)
        return 0
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(lock, fcntl.LOCK_EX)
            emit(read_cache() or {"version": VERSION, "services": []})
            return 0
        data = run(cfg, read_cache() or prev)
        write_cache(data)
    emit(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
