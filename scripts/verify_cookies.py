#!/usr/bin/env python3
"""Read-only cookie verifier. Never prints any cookie value.
Usage: python scripts/verify_cookies.py"""
import json, os, sys, time
from pathlib import Path

SECRETS = Path(__file__).resolve().parent.parent / "secrets"

EXPECTED = {
    "x_xiaohao_cookies.json": {
        "domain": ".x.com",
        "must_have": {"auth_token", "ct0", "twid"},
        "uid_cookie": "twid",  # value is u%3D<uid>
    },
    "futu_cookies.json": {
        "domain": ".futunn.com",
        "must_have": {"web_sig", "ci_sig", "uid"},
        "uid_cookie": "uid",
    },
    "xueqiu_cookies.json": {
        "domain": ".xueqiu.com",
        "must_have": {"xq_a_token", "xq_r_token", "xq_id_token", "u"},
        "uid_cookie": "u",
    },
}

def uid_tail(value: str) -> str:
    """Return last 4 chars of UID without revealing full value."""
    import urllib.parse
    decoded = urllib.parse.unquote(value)
    # twid format: u=<uid>
    if "=" in decoded:
        decoded = decoded.split("=", 1)[1]
    return decoded[-4:] if len(decoded) >= 4 else "??"

def days_until(timestamp: int) -> int:
    return max(0, (timestamp - int(time.time())) // 86400)

ok = True
print(f"{'File':<30} {'Status':<8} {'Cookies':<10} {'UID tail':<10} {'Expires in'}")
print("-" * 80)
for fname, spec in EXPECTED.items():
    path = SECRETS / fname
    if not path.exists():
        print(f"{fname:<30} ❌MISSING")
        ok = False
        continue
    if oct(path.stat().st_mode)[-3:] != "600":
        print(f"{fname:<30} ⚠️PERMS  (need chmod 600)")
        ok = False
        continue
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"{fname:<30} ❌BAD_JSON")
        ok = False
        continue
    names = {c["name"] for c in data}
    missing = spec["must_have"] - names
    if missing:
        print(f"{fname:<30} ❌MISS_AUTH (missing: {missing})")
        ok = False
        continue
    uid_c = next((c for c in data if c["name"] == spec["uid_cookie"]), None)
    tail = uid_tail(uid_c["value"]) if uid_c else "??"
    exp = min(c.get("expires", 0) for c in data if c["name"] in spec["must_have"])
    days = days_until(int(exp))
    print(f"{fname:<30} ✅OK     {len(data):<10} *{tail:<8} {days} days")

print("-" * 80)
print("✅ All good" if ok else "❌ Issues found above")
sys.exit(0 if ok else 1)
