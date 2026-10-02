#!/usr/bin/env python3
"""
claim_daily.py — claim the Melodifestivalen daily card ("Hämta kort") via API.

Discovered by capturing the app's traffic through mitmproxy (see FINDINGS.md).

Flow
----
1. UNO auth (SVT account):
   POST https://auth.prod.uno.svt.se/authentication/v5/login/email          -> "email sent"
   (user reads the 6-digit code from their mailbox)
   POST https://auth.prod.uno.svt.se/authentication/v5/login/email/confirm  -> accessToken + refreshToken
   The confirm call needs a PKCE pair: challenge = base64url(sha256(verifier)),
   challengeMethod "S256". The app uses this to tie the emailed code to the client.

2. Card claim:
   POST https://eqhdw40qtf.execute-api.eu-west-1.amazonaws.com/production/get
   body: {"album": "<id>"}   Authorization: Bearer <accessToken>
   -> {"data":{"cards":["CODE:0", ...],"next":<epoch>,"version":{...},"streak":{"bonus":N}}}

   `cards` are the cards offered in the current window. `next` is the epoch
   timestamp when the next claim window opens (top of the hour, server side).
   Claiming also bumps the account rank/score by 3 (GET_CARD action), which the
   app receives over its AppSync WebSocket subscription `onRankUpdated`.

Usage
-----
  ./claim_daily.py                       # claim, reuse cached token if valid
  ./claim_daily.py --login              # force a fresh email-code login
  ./claim_daily.py --email you@mail.se --album 23
  ./claim_daily.py --token-file tok.json --check   # only show state, claim nothing
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import random
import string
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UNAUTH = "https://auth.prod.uno.svt.se/authentication/v5"
CARDS = "https://eqhdw40qtf.execute-api.eu-west-1.amazonaws.com/production/get"
UA = "Melodifestivalen/android/10.4.0/60385"
CLIENT = "melodifestivalen-android"

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.json")
STATS_FILE = os.environ.get("STATS_FILE", "/data/stats.json")


# ---------------------------------------------------------------- token utils

def b64url(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def new_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256.

    The app's verifier is short (looks like 8 chars).
    """
    verifier = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def decode_jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def token_valid(token: str, skew: int = 120) -> bool:
    exp = decode_jwt_payload(token).get("exp")
    return bool(exp) and exp - skew > time.time()


def load_token_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return {}


def save_token_file(path: str, data: dict) -> None:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, 0o600)


# ------------------------------------------------------------------- http

def request(method: str, url: str, headers: dict, body=None, timeout: int = 20):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept-Encoding": "gzip", "Connection": "Keep-Alive", **headers}
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip_decompress(raw)
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400].decode("utf-8", "replace")}


def gzip_decompress(raw: bytes) -> bytes:
    import gzip
    return gzip.decompress(raw)


# ------------------------------------------------------------------- auth

import tempfile

def _pkce_temp_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "melo_pkce"
    d.mkdir(exist_ok=True)
    return d


def _pkce_key(challenge: str, device_id: str) -> str:
    return f"{urllib.parse.quote(challenge, safe='')}_{device_id}"

def _save_pkce(challenge: str, device_id: str, verifier: str):
    p = _pkce_temp_dir() / f"{_pkce_key(challenge, device_id)}.json"
    p.write_text(json.dumps({"verifier": verifier}))


def _load_pkce(challenge: str, device_id: str) -> str | None:
    p = _pkce_temp_dir() / f"{_pkce_key(challenge, device_id)}.json"
    if p.exists():
        try:
            return json.loads(p.read_text()).get("verifier")
        except Exception:
            return None
    return None


def _delete_pkce(challenge: str, device_id: str):
    p = _pkce_temp_dir() / f"{_pkce_key(challenge, device_id)}.json"
    if p.exists():
        p.unlink()


def login_request_code(email: str) -> dict:
    """Step 1: request email code, return challenge for later verification."""
    device_id = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    verifier, challenge = new_pkce()
    _save_pkce(challenge, device_id, verifier)
    common = {
        "x-device-id": device_id,
        "x-uno-client": CLIENT,
        "User-Agent": UA,
        "Content-Type": "application/json",
    }

    print(f"[*] requesting login code for {email} ...", file=sys.stderr)
    status, body = request(
        "POST", f"{UNAUTH}/login/email", common,
        {"email": email, "challenge": urllib.parse.quote(challenge, safe=""), "challengeMethod": "S256"},
    )
    if status != 200:
        _delete_pkce(challenge, device_id)
        return {"success": False, "error": f"login/email failed {status}: {body}"}
    print("[+] code sent", file=sys.stderr)
    return {"success": True, "challenge": challenge, "device_id": device_id}


def login_submit_code(email: str, code: str, challenge: str, device_id: str) -> dict:
    """Step 2: submit code with verifier, return token data."""
    verifier = _load_pkce(challenge, device_id)
    if not verifier:
        return {"success": False, "error": "PKCE verifier not found (expired or invalid challenge/device_id)"}
    common = {
        "x-device-id": device_id,
        "x-uno-client": CLIENT,
        "User-Agent": UA,
        "Content-Type": "application/json",
    }
    status, body = request(
        "POST", f"{UNAUTH}/login/email/confirm", common,
        {"email": email, "code": code, "challengeVerifier": verifier},
    )
    if status != 200 or "accessToken" not in body:
        return {"success": False, "error": f"login/email/confirm failed {status}: {body}"}
    _delete_pkce(challenge, device_id)
    print("[+] logged in", file=sys.stderr)
    return {
        "success": True,
        "email": email,
        "accessToken": body["accessToken"],
        "refreshToken": body.get("refreshToken"),
        "deviceId": device_id,
        "savedAt": int(time.time()),
    }


def login(email: str) -> dict:
    """Interactive login (for CLI use)."""
    res = login_request_code(email)
    if not res["success"]:
        sys.exit(f"[!] {res['error']}")
    code = input("    code: ").strip()
    if not code:
        sys.exit("[!] no code given")
    res = login_submit_code(email, code, res["challenge"], res["verifier"], res["device_id"])
    if not res["success"]:
        sys.exit(f"[!] {res['error']}")
    return {k: v for k, v in res.items() if k != "success"}


def get_access_token(args) -> str:
    if args.token:
        return args.token.strip()

    # Handle two-step login for web UI
    if args.login_request_code:
        if not args.email:
            sys.exit("[!] --email required with --login-request-code")
        res = login_request_code(args.email)
        if not res["success"]:
            sys.exit(f"[!] {res['error']}")
        print(f"CHALLENGE:{res['challenge']}")
        print(f"DEVICE_ID:{res['device_id']}")
        sys.exit(0)

    if args.login_submit_code:
        if not all([args.email, args.code, args.challenge, args.device_id]):
            sys.exit("[!] --email, --code, --challenge, and --device-id required with --login-submit-code")
        res = login_submit_code(args.email, args.code, args.challenge, args.device_id)
        if not res["success"]:
            sys.exit(f"[!] {res['error']}")
        save_token_file(args.token_file, {k: v for k, v in res.items() if k != "success"})
        print(res["accessToken"])
        sys.exit(0)

    if not args.login:
        cached = load_token_file(args.token_file)
        tok = cached.get("accessToken")
        if tok and token_valid(tok):
            print(f"[*] using cached token (user {decode_jwt_payload(tok).get('sub')})")
            return tok
        if not sys.stdin.isatty():
            sys.exit("[!] token expired/absent - login via web UI (do not auto-relogin)")
        if cached.get("email"):
            print("[*] cached token expired/absent -> logging in again")
            args.email = args.email or cached["email"]
    email = args.email or input("email: ").strip()
    data = login(email)
    save_token_file(args.token_file, data)
    return data["accessToken"]


# ------------------------------------------------------------------- claim

def _fmt_cards(cards) -> str:
    if cards is None:
        return "      (none - window already used)"
    if not cards:
        return "      (none)"
    return "\n".join(f"      {c}" for c in cards)


# -------------------------------------------------------------------- stats

def load_stats() -> dict:
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"total_claims": 0, "total_cards": 0, "unique_codes": [],
                "per_album": {}, "history": []}


def save_stats(stats: dict):
    try:
        Path(STATS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass


def record_stats(album: str, cards: list[str] | None):
    """Record a claim result into the stats file."""
    if not cards:
        return
    stats = load_stats()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats["total_claims"] += 1
    stats["total_cards"] += len(cards)
    seen = set(stats.get("unique_codes", []))
    new_codes = []
    for c in cards:
        code = c.split(":")[0]
        if code not in seen:
            seen.add(code)
            new_codes.append(code)
    stats["unique_codes"] = sorted(seen)
    pa = stats.setdefault("per_album", {}).setdefault(str(album),
        {"claims": 0, "cards": 0, "last": None})
    pa["claims"] += 1
    pa["cards"] += len(cards)
    pa["last"] = now
    hist = stats.setdefault("history", [])
    hist.append({"ts": now, "album": str(album), "n": len(cards),
                 "new": new_codes})
    stats["history"] = hist[-500:]
    save_stats(stats)


def claim(token: str, album: str, dry_run: bool = False) -> dict:
    headers = {
        "cache-control": "no-cache, no-store, must-revalidate",
        "cache": "no-store",
        "pragma": "no-cache",
        "authorization": f"Bearer {token}",
        "expires": "0",
        "User-Agent": UA,
        "Content-Type": "application/json",
    }
    body = {"album": album}
    print(f"[*] POST {CARDS}  body={body}")
    status, resp = request("POST", CARDS, headers, body)
    print(f"[{'-' * 60}]")

    if status == 429:
        print(f"[!] 429 TOO_MANY_REQUESTS - claim already used for this hour window.")
        print(f"[i] Server enforces one claim per window. Wait for the next window and retry.")
        sys.exit(2)
    if status in (401, 403):
        print(f"[!] HTTP {status}: token rejected. Re-run with --login to get a fresh one.")
        print(f"    {json.dumps(resp)[:400]}")
        sys.exit(1)
    if status != 200:
        print(f"[!] HTTP {status}: {json.dumps(resp)[:600]}")
        sys.exit(1)

    data = resp.get("data") or {}
    cards = data.get("cards")
    nxt = data.get("next")
    print(f"[+] cards:  {_fmt_cards(cards)}")
    print(f"[+] streak: {data.get('streak')}")
    print(f"[+] version: {data.get('version')}")

    if nxt:
        when = time.strftime("%H:%M:%S", time.gmtime(int(nxt)))
        wait = int(nxt) - int(time.time())
        if wait > 0:
            print(f"[i] next claim window: {when} UTC (in {wait // 3600}h{(wait % 3600) // 60}m)")
        else:
            print(f"[i] next claim window: {when} UTC (open now)")
    if cards is None:
        print("[!] no cards in response - you already claimed this window.")
        if not dry_run:
            sys.exit(2)
    else:
        record_stats(album, cards)
    return resp


def main() -> None:
    ap = argparse.ArgumentParser(description="claim the Melodifestivalen daily card(s)")
    ap.add_argument("--email", help="SVT account email")
    ap.add_argument("--login", action="store_true", help="force fresh login (interactive, not for container)")
    ap.add_argument("--login-request-code", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--login-submit-code", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--code", help=argparse.SUPPRESS)
    ap.add_argument("--challenge", help=argparse.SUPPRESS)
    ap.add_argument("--device-id", help=argparse.SUPPRESS)
    ap.add_argument("--token", help="use this access token instead of the cache")
    ap.add_argument("--token-file", default="/data/token.json", help="path to token.json (default /data/token.json)")
    ap.add_argument("--album", default="23", help="album id to claim from (default 23)")
    ap.add_argument("--all", action="store_true", help="claim all albums 1..25 sequentially")
    ap.add_argument("--check", action="store_true", help="show state without printing a claim as done")
    ap.add_argument("--delay", type=float, default=0.5, help="delay between album claims (seconds)")
    args = ap.parse_args()

    token = get_access_token(args)

    if args.all:
        albums = [str(i) for i in range(1, 26)]
        print(f"[*] claiming all {len(albums)} albums (1..25) with {args.delay}s delay...")
        total_cards = 0
        for alb in albums:
            try:
                resp = claim(token, alb, dry_run=args.check)
                data = resp.get("data", {})
                cards = data.get("cards")
                if cards:
                    total_cards += len(cards)
                if not args.check:
                    time.sleep(args.delay)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if code == 2:
                    print(f"[i] album {alb}: window already used, skipping")
                    continue
                raise
        print(f"\n[+] done. total cards claimed this run: {total_cards}")
        return

    claim(token, args.album, dry_run=args.check)


if __name__ == "__main__":
    main()
