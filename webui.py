#!/usr/bin/env python3
"""
Simple web UI for melo-mitm claimer.
Runs on port 8080 inside the container.
"""

import json
import os
import re
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

TOKEN_FILE = os.environ.get("TOKEN_FILE", "/data/token.json")
CLAIM_SCRIPT = "/app/claim_daily.py"
LOG_FILE = os.environ.get("LOG_FILE", "/data/claimer.log")
STATS_FILE = os.environ.get("STATS_FILE", "/data/stats.json")

ALBUMS_JSON_URL = "https://svt-hof.s3-eu-west-1.amazonaws.com/albums.json"
ALBUMS_CACHE_FILE = "/data/albums.json"
USERCARDS_URL = "https://eqhdw40qtf.execute-api.eu-west-1.amazonaws.com/production/usercardscount"
CARD_IMG_BASE = "https://svt-hof.s3.eu-west-1.amazonaws.com/{}_thumb_v10.jpg"
UA = "Melodifestivalen/android/10.4.0/60385"

# per-album card metadata (title, song, rarity, badges, image uris)
INFO_URL = "https://svt-hof.s3-eu-west-1.amazonaws.com/info.{}.json"
INFO_CACHE_DIR = os.environ.get("INFO_CACHE_DIR", "/data/info")

RANKDATA_URL = "https://d9949q48n87u.cloudfront.net/rankdata"
AMIGO_PROFILE_URL = "https://amigo.prod.uno.svt.se/amigo/v3/users/me/profile"
RANKDATA_CACHE_FILE = "/data/rankdata.json"
AMIGO_CACHE_FILE = "/data/amigo_profile.json"

_rankdata_cache = {"ts": 0, "data": None, "err": None}
_amigo_cache = {"ts": 0, "data": None, "err": None}
RARITY_ORDER = ["Mycket vanligt", "Vanligt", "Ovanligt", "Mycket ovanligt",
                "Sällsynt", "Oerhört sällsynt"]
_info_lock = threading.Lock()
_info: dict[int, dict] = {}
_info_state = {"prefetching": False, "err": None}

# collection cache: {ts, albums, owned, err}
_collection_lock = threading.Lock()
_collection = {"ts": 0, "albums": None, "owned": None, "err": None}

# Simple in-memory log buffer (last 200 lines)
log_buffer = []
log_lock = threading.Lock()


def log_entry(level: str, msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    entry = {"ts": ts, "level": level, "msg": msg}
    with log_lock:
        log_buffer.append(entry)
        if len(log_buffer) > 200:
            log_buffer.pop(0)


def read_token() -> dict:
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def write_token(data: dict):
    Path(TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(TOKEN_FILE, 0o600)


def run_claim(album: str, check: bool = False) -> dict:
    """Run claim_daily.py and return parsed output."""
    cmd = ["python3", CLAIM_SCRIPT, "--token-file", TOKEN_FILE, "--album", album]
    if check:
        cmd.append("--check")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


def run_claim_all(check: bool = False) -> dict:
    cmd = ["python3", CLAIM_SCRIPT, "--token-file", TOKEN_FILE, "--all"]
    if check:
        cmd.append("--check")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


# login state storage (in-memory, per session)
login_sessions = {}
login_lock = threading.Lock()

def run_login_request_code(email: str) -> dict:
    """Step 1: request the email code."""
    cmd = ["python3", CLAIM_SCRIPT, "--token-file", TOKEN_FILE, "--email", email, "--login-request-code"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            # parse CHALLENGE and DEVICE_ID from stdout
            challenge = None
            device_id = None
            for line in result.stdout.strip().split('\n'):
                if line.startswith('CHALLENGE:'):
                    challenge = line.split(':', 1)[1]
                elif line.startswith('DEVICE_ID:'):
                    device_id = line.split(':', 1)[1]
            if challenge and device_id:
                return {
                    "exit_code": 0,
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                    "challenge": challenge,
                    "device_id": device_id,
                }
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


def run_login_submit_code(email: str, code: str, challenge: str, device_id: str) -> dict:
    """Step 2: submit the code with challenge verifier."""
    cmd = ["python3", CLAIM_SCRIPT, "--token-file", TOKEN_FILE, "--email", email, 
           "--login-submit-code", "--code", code, "--challenge", challenge, "--device-id", device_id]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


def decode_jwt_payload(token: str) -> dict:
    import base64
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


# --------------------------- collection (albums + owned cards)

def load_albums(force: bool = False) -> list[dict]:
    """Album metadata (names, covers, card lists) from S3, cached 24h on disk."""
    try:
        if not force and os.path.exists(ALBUMS_CACHE_FILE):
            age = time.time() - os.path.getmtime(ALBUMS_CACHE_FILE)
            if age < 86400:
                with open(ALBUMS_CACHE_FILE) as f:
                    return json.load(f).get("albums", [])
    except Exception:
        pass
    try:
        with urllib.request.urlopen(ALBUMS_JSON_URL, timeout=20) as r:
            data = json.load(r)
        albums = data.get("albums", [])
        Path(ALBUMS_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(ALBUMS_CACHE_FILE, "w") as f:
            json.dump(data, f)
        return albums
    except Exception as e:
        log_entry("warn", f"albums.json fetch failed: {e}")
        try:
            with open(ALBUMS_CACHE_FILE) as f:
                return json.load(f).get("albums", [])
        except Exception:
            return []


def fetch_owned_cards() -> tuple[dict, str | None]:
    """Return ({code: count}, error). Uses the stored access token."""
    tok = read_token()
    access = tok.get("accessToken")
    if not access:
        return {}, "no token"
    req = urllib.request.Request(
        USERCARDS_URL,
        data=b"{}",
        headers={
            "Authorization": "Bearer " + access,
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return {}, f"HTTP {e.code}: {body}"
    except Exception as e:
        return {}, str(e)
    owned = {}
    for c in (data.get("data") or {}).get("cards") or []:
        if "i" in c:
            owned[c["i"]] = c.get("c", c.get("u", 0))
    return owned, None


def get_collection(force: bool = False) -> dict:
    with _collection_lock:
        fresh = (time.time() - _collection["ts"]) < 60
        if not force and fresh and _collection["albums"] is not None:
            albums, owned, err = _collection["albums"], _collection["owned"], _collection["err"]
        else:
            albums = load_albums(force=force)
            owned, err = fetch_owned_cards()
            _collection.update(ts=time.time(), albums=albums, owned=owned, err=err)

    out, tot_cards, tot_owned = [], 0, 0
    for a in albums:
        cards = a.get("cards") or {}
        have = sum(1 for code in cards if code in owned)
        tot_cards += len(cards)
        tot_owned += have
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "short": a.get("short"),
            "cover": a.get("cover"),
            "total": len(cards),
            "owned": have,
            "pct": round(100 * have / len(cards), 1) if cards else 0.0,
        })
    prefetch_info()
    return {
        "albums": out,
        "totals": {"cards": tot_cards, "owned": tot_owned,
                   "pct": round(100 * tot_owned / tot_cards, 1) if tot_cards else 0.0,
                   "unique_owned": len(owned)},
        "img": CARD_IMG_BASE,
        "err": err,
        "fetched": _collection["ts"],
    }


# --------------------------- card info (title, rarity, song)

def load_album_info(album_id, force: bool = False) -> dict:
    """Cards metadata for one album from S3 (info.<id>.json), cached on disk 24h."""
    aid = int(album_id)
    with _info_lock:
        if not force and aid in _info:
            return _info[aid]
    path = os.path.join(INFO_CACHE_DIR, f"info.{aid}.json")
    if not force:
        try:
            if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < 86400:
                with open(path) as f:
                    data = json.load(f)
                with _info_lock:
                    _info[aid] = data
                return data
        except Exception:
            pass
    try:
        with urllib.request.urlopen(INFO_URL.format(aid), timeout=25) as r:
            data = json.load(r)
        Path(INFO_CACHE_DIR).mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
        with _info_lock:
            _info[aid] = data
            _info_state["err"] = None
        return data
    except Exception as e:
        with _info_lock:
            _info_state["err"] = str(e)
        log_entry("warn", f"info.{aid}.json fetch failed: {e}")
        return {}


def prefetch_info() -> None:
    """Load all 25 info files in the background (once)."""
    with _info_lock:
        if _info_state["prefetching"]:
            return
        _info_state["prefetching"] = True

    def run():
        try:
            for aid in range(1, 26):
                load_album_info(aid)
        finally:
            with _info_lock:
                _info_state["prefetching"] = False
            log_entry("info", f"card info loaded for {len(_info)} albums")

    threading.Thread(target=run, daemon=True).start()


def card_song(meta: dict) -> str:
    for iv in meta.get("infoValues") or []:
        if iv.get("short") in ("bidragets-namn", "lattitel"):
            v = (iv.get("infoValue") or "").strip()
            if v:
                return v
    return ""


def card_extra(meta: dict, limit: int = 8) -> dict:
    out = {}
    for iv in meta.get("infoValues") or []:
        k = iv.get("short")
        v = (iv.get("infoValue") or "").strip()
        if not k or k == "info" or not v:
            continue
        out[k] = v
        if len(out) >= limit:
            break
    return out


def album_info_cards(album_id) -> dict:
    return (load_album_info(album_id) or {}).get("cards") or {}


def rarity_key(name: str) -> int:
    try:
        return RARITY_ORDER.index(name)
    except ValueError:
        return len(RARITY_ORDER)


def load_rankdata(force: bool = False) -> dict:
    """Rank system data (levels, daily actions, rewards) cached 24h."""
    with _collection_lock:  # reuse lock
        fresh = (time.time() - _rankdata_cache["ts"]) < 86400
        if not force and fresh and _rankdata_cache["data"] is not None:
            return _rankdata_cache["data"]
    try:
        if not force and os.path.exists(RANKDATA_CACHE_FILE):
            age = time.time() - os.path.getmtime(RANKDATA_CACHE_FILE)
            if age < 86400:
                with open(RANKDATA_CACHE_FILE) as f:
                    data = json.load(f)
                with _collection_lock:
                    _rankdata_cache.update(ts=time.time(), data=data, err=None)
                return data
    except Exception:
        pass
    try:
        tok = read_token()
        access = tok.get("accessToken")
        if not access:
            return {"err": "no token"}
        req = urllib.request.Request(RANKDATA_URL, headers={"Authorization": f"Bearer {access}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        Path(RANKDATA_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(RANKDATA_CACHE_FILE, "w") as f:
            json.dump(data, f)
        with _collection_lock:
            _rankdata_cache.update(ts=time.time(), data=data, err=None)
        return data
    except Exception as e:
        with _collection_lock:
            _rankdata_cache["err"] = str(e)
        log_entry("warn", f"rankdata fetch failed: {e}")
        try:
            with open(RANKDATA_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            return {"err": str(e)}


def load_amigo_profile(force: bool = False) -> dict:
    """User profile (username, displayName, avatar) cached 24h."""
    with _collection_lock:
        fresh = (time.time() - _amigo_cache["ts"]) < 86400
        if not force and fresh and _amigo_cache["data"] is not None:
            return _amigo_cache["data"]
    try:
        if not force and os.path.exists(AMIGO_CACHE_FILE):
            age = time.time() - os.path.getmtime(AMIGO_CACHE_FILE)
            if age < 86400:
                with open(AMIGO_CACHE_FILE) as f:
                    data = json.load(f)
                with _collection_lock:
                    _amigo_cache.update(ts=time.time(), data=data, err=None)
                return data
    except Exception:
        pass
    try:
        tok = read_token()
        access = tok.get("accessToken")
        if not access:
            return {"err": "no token"}
        req = urllib.request.Request(AMIGO_PROFILE_URL, headers={"Authorization": f"Bearer {access}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        Path(AMIGO_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(AMIGO_CACHE_FILE, "w") as f:
            json.dump(data, f)
        with _collection_lock:
            _amigo_cache.update(ts=time.time(), data=data, err=None)
        return data
    except Exception as e:
        with _collection_lock:
            _amigo_cache["err"] = str(e)
        log_entry("warn", f"amigo profile fetch failed: {e}")
        try:
            with open(AMIGO_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            return {"err": str(e)}


@app.route("/api/rankdata")
def api_rankdata():
    force = request.args.get("force") == "1"
    return jsonify(load_rankdata(force=force))


@app.route("/api/amigo/profile")
def api_amigo_profile():
    force = request.args.get("force") == "1"
    return jsonify(load_amigo_profile(force=force))


# --------------------------- Routes

TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")


@app.route("/")
def index():
    try:
        with open(TEMPLATE_FILE) as f:
            html = f.read()
    except Exception:
        html = "<h1>template.html missing</h1>"
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/token")
def api_token():
    tok = read_token()
    access = tok.get("accessToken", "")
    if not access:
        return jsonify({"has_token": False})
    payload = decode_jwt_payload(access)
    return jsonify({
        "has_token": True,
        "sub": payload.get("sub"),
        "exp": payload.get("exp"),
        "email": tok.get("email"),
    })


@app.route("/api/token/raw")
def api_token_raw():
    tok = read_token()
    return jsonify({"accessToken": tok.get("accessToken", "")})


@app.route("/api/claim", methods=["POST"])
def api_claim():
    data = request.get_json() or {}
    album = data.get("album", "23")
    check = data.get("check", False)
    log_entry("info", f"API claim album={album} check={check}")
    res = run_claim(album, check)
    log_entry("info", f"claim exit={res['exit_code']}")
    return jsonify(res)


@app.route("/api/claim-all", methods=["POST"])
def api_claim_all():
    data = request.get_json() or {}
    check = data.get("check", False)
    log_entry("info", f"API claim-all check={check}")
    res = run_claim_all(check)
    log_entry("info", f"claim-all exit={res['exit_code']}")
    return jsonify(res)


@app.route("/api/login/request-code", methods=["POST"])
def api_login_request_code():
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"exit_code": 1, "stdout": "", "stderr": "email required"}), 400
    log_entry("info", f"API login request-code email={email}")
    res = run_login_request_code(email)
    if res.get("exit_code") == 0 and res.get("challenge") and res.get("device_id"):
        # store in session
        session_id = str(random.randint(100000, 999999))
        with login_lock:
            login_sessions[session_id] = {
                "email": email,
                "challenge": res["challenge"],
                "device_id": res["device_id"],
                "created": time.time(),
            }
        res["session_id"] = session_id
    log_entry("info", f"login request-code exit={res['exit_code']}")
    return jsonify(res)


@app.route("/api/login/submit-code", methods=["POST"])
def api_login_submit_code():
    data = request.get_json() or {}
    session_id = data.get("session_id", "").strip()
    code = data.get("code", "").strip()
    if not session_id or not code:
        return jsonify({"exit_code": 1, "stdout": "", "stderr": "session_id and code required"}), 400
    with login_lock:
        session = login_sessions.get(session_id)
    if not session:
        return jsonify({"exit_code": 1, "stdout": "", "stderr": "invalid or expired session"}), 400
    email = session["email"]
    challenge = session["challenge"]
    device_id = session["device_id"]
    log_entry("info", f"API login submit-code email={email}")
    res = run_login_submit_code(email, code, challenge, device_id)
    if res.get("exit_code") == 0:
        # clean up session
        with login_lock:
            login_sessions.pop(session_id, None)
    log_entry("info", f"login submit-code exit={res['exit_code']}")
    return jsonify(res)


@app.route("/api/login", methods=["POST"])
def api_login():
    # legacy endpoint - redirects to two-step
    return jsonify({"exit_code": 1, "stdout": "", "stderr": "use /api/login/request-code and /api/login/submit-code"}), 400


_FILE_LOG_RE = re.compile(r"^\[([^\]]+)\] \[([A-Za-z]+)\]\s*(.*)$")


def read_file_log(limit: int = 400) -> list[dict]:
    """Lines written by the entrypoint claim cycle: [ts] [LEVEL] message."""
    try:
        with open(LOG_FILE, errors="replace") as f:
            lines = f.readlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in lines:
        ln = ln.rstrip("\n")
        if not ln.strip():
            continue
        m = _FILE_LOG_RE.match(ln)
        if m:
            out.append({"ts": m.group(1), "level": m.group(2).lower(), "msg": m.group(3)})
        else:
            out.append({"ts": datetime.now(timezone.utc).isoformat(), "level": "info", "msg": ln})
    return out


def _log_ts(e: dict):
    try:
        return datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


@app.route("/api/log")
def api_log():
    with log_lock:
        mem = list(log_buffer)
    merged = mem + read_file_log()
    merged.sort(key=_log_ts)
    return jsonify({"logs": merged[-400:]})


@app.route("/api/stats")
def api_stats():
    try:
        with open(STATS_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"total_claims": 0, "total_cards": 0,
                        "unique_codes": [], "per_album": {}, "history": []})


@app.route("/api/collection")
def api_collection():
    force = request.args.get("force") == "1"
    return jsonify(get_collection(force=force))


@app.route("/api/collection/<int:album_id>")
def api_collection_album(album_id):
    force = request.args.get("force") == "1"
    sort = request.args.get("sort", "num")
    col = get_collection(force=force)
    owned, img = _collection.get("owned") or {}, CARD_IMG_BASE
    for a in col["albums"]:
        if a["id"] == album_id:
            meta = next((x for x in (_collection.get("albums") or []) if x.get("id") == album_id), {})
            info_cards = album_info_cards(album_id)
            cards, by_rarity = [], {}
            for code in (meta.get("cards") or {}):
                m = info_cards.get(code) or {}
                images = m.get("images") or {}
                thumb = (images.get("thumb") or {}).get("uri")
                big = (images.get("big") or {}).get("uri")
                rarity = m.get("rarityName") or ""
                have = owned.get(code, 0)
                info_text = ""
                for iv in m.get("infoValues") or []:
                    if iv.get("short") == "info" and iv.get("infoValue"):
                        info_text = iv["infoValue"].strip()
                        break
                cards.append({
                    "code": code,
                    "have": have,
                    "title": m.get("title") or "",
                    "song": card_song(m),
                    "rarity": rarity,
                    "rkey": rarity_key(rarity),
                    "series": m.get("seriesName") or "",
                    "num": m.get("cardNumber"),
                    "badges": (m.get("badges") or "").strip(),
                    "variant": (m.get("variant") or "").strip(),
                    "info": info_text[:500],
                    "extra": card_extra(m),
                    "img": thumb or img.format(code),
                    "imgBig": big or img.format(code).replace("_thumb_", "_big_"),
                })
                slot = by_rarity.setdefault(rarity or "?", {"total": 0, "owned": 0})
                slot["total"] += 1
                if have:
                    slot["owned"] += 1
            _sort_cards(cards, sort)
            return jsonify({"album": a, "cards": cards, "byRarity": by_rarity,
                            "infoReady": bool(info_cards), "img": img})
    return jsonify({"error": "unknown album"}), 404


def _sort_cards(cards: list, sort: str) -> None:
    if sort == "name":
        cards.sort(key=lambda c: (c["title"].lower() or "\uffff", c["num"] or 9999))
    elif sort == "rarity":
        cards.sort(key=lambda c: (c["rkey"], c["num"] or 9999, c["code"]))
    elif sort == "owned":
        cards.sort(key=lambda c: (-c["have"], c["rkey"], c["num"] or 9999))
    else:  # num = album order
        cards.sort(key=lambda c: (c["num"] or 9999, c["code"]))


@app.route("/api/cards")
def api_cards():
    """Compact metadata for every card in every album (powers search/rarity/duplicates)."""
    force = request.args.get("force") == "1"
    col = get_collection(force=force)
    owned = _collection.get("owned") or {}
    raw = _collection.get("albums") or []

    # make sure info files are available (disk cache usually serves them instantly)
    unloaded = [a["id"] for a in raw if a["id"] not in _info]
    if unloaded:
        for aid in unloaded[:5]:
            load_album_info(aid)
        if len(unloaded) > 5:
            prefetch_info()

    cards, out_albums, by_rarity = {}, [], {}
    ready = 0
    for a in raw:
        aid = a.get("id")
        album_codes = a.get("cards") or {}
        info_cards = album_info_cards(aid)
        if info_cards:
            ready += 1
        for code in album_codes:
            m = info_cards.get(code) or {}
            rarity = m.get("rarityName") or ""
            have = owned.get(code, 0)
            cards[code] = {
                "a": aid,
                "t": m.get("title") or "",
                "s": card_song(m),
                "r": rarity,
                "rkey": rarity_key(rarity),
                "n": m.get("cardNumber"),
                "b": (m.get("badges") or "").strip(),
                "v": ((m.get("images") or {}).get("thumb") or {}).get("version") or "10",
                "h": have,
            }
            slot = by_rarity.setdefault(rarity or "?", {"total": 0, "owned": 0})
            slot["total"] += 1
            if have:
                slot["owned"] += 1
        total = len(album_codes)
        owned_n = sum(1 for c in album_codes if c in owned)
        out_albums.append({
            "id": aid, "name": a.get("name"), "short": a.get("short"),
            "total": total, "owned": owned_n,
            "dups": sum(1 for c in album_codes if owned.get(c, 0) > 1),
            "pct": round(100 * owned_n / total, 1) if total else 0.0,
        })

    missing = sum(1 for c in cards.values() if c["h"] == 0)
    dups = sum(1 for c in cards.values() if c["h"] > 1)
    return jsonify({
        "albums": out_albums,
        "cards": cards,
        "byRarity": by_rarity,
        "rarityOrder": [r for r in RARITY_ORDER if r in by_rarity],
        "totals": {"codes": len(cards), "owned": len(cards) - missing,
                   "missing": missing, "dups": dups, "infoReady": ready,
                   "infoTotal": len(raw)},
        "err": _info_state.get("err"),
        "fetched": int(time.time()),
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force a token refresh now (same logic as entrypoint.sh)."""
    tok = read_token()
    refresh = tok.get("refreshToken")
    if not refresh:
        return jsonify({"ok": False, "error": "no refreshToken in token file"}), 400
    req = urllib.request.Request(
        "https://auth.prod.uno.svt.se/authentication/v5/token/refresh",
        data=json.dumps({"refreshToken": refresh}).encode(),
        headers={
            "x-device-id": tok.get("deviceId", ""),
            "x-uno-client": "melodifestivalen-android",
            "User-Agent": "Melodifestivalen/android/10.4.0/60385",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        log_entry("error", f"manual token refresh failed: HTTP {e.code} {body}")
        return jsonify({"ok": False, "error": f"HTTP {e.code}: {body}"}), 502
    except Exception as e:
        log_entry("error", f"manual token refresh failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502
    tok["accessToken"] = resp["accessToken"]
    if "refreshToken" in resp:
        tok["refreshToken"] = resp["refreshToken"]
    tok["savedAt"] = int(time.time())
    write_token(tok)
    log_entry("info", "manual token refresh succeeded")
    return jsonify({"ok": True, "exp": decode_jwt_payload(tok["accessToken"]).get("exp")})


if __name__ == "__main__":
    # seed log
    log_entry("info", "web UI started")
    app.run(host="0.0.0.0", port=8765, threaded=True)