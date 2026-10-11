"""Users + roles (viewer < operator < admin), signed session tokens, encryption at rest for secrets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Callable

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request

from . import db
from .config import STATE, settings

log = logging.getLogger("software-manager.security")
ROLES = {"viewer": 1, "operator": 2, "admin": 3}
SESSION_SEC = int(settings.sm_session_hours * 3600)


def _secret_key() -> bytes:
    if settings.sm_secret_key:
        return hashlib.sha256(settings.sm_secret_key.encode()).digest()
    f = STATE / "secret.key"
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
        f.chmod(0o600)
    return f.read_bytes()


_KEY = _secret_key()
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_KEY + b"enc").digest()))


def encrypt(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode() if s else ""


def decrypt(s: str) -> str:
    return _fernet.decrypt(s.encode()).decode() if s else ""


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 310000)
    return "pbkdf2$310000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


def sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_KEY, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def unsign(token: str) -> dict | None:
    try:
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_KEY, body.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def bootstrap_admin() -> None:
    if db.one("SELECT 1 AS x FROM users LIMIT 1"):
        return
    pw = settings.sm_admin_password
    if not pw:
        f = STATE / "initial-admin-password"
        if not f.exists():
            f.write_text(secrets.token_urlsafe(12))
            f.chmod(0o600)
        pw = f.read_text().strip()
        log.warning("created user '%s'; initial password is in %s (change it in Settings)", settings.sm_admin_user, f)
    db.ex("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)",
          settings.sm_admin_user, hash_pw(pw), "admin", time.time())


_fails: dict[str, list[float]] = {}


def login(username: str, password: str, ip: str) -> dict:
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 60]
    if len(recent) >= 5:
        raise HTTPException(429, "too many failed logins; wait a minute")
    u = db.one("SELECT * FROM users WHERE username=? AND disabled=0", username)
    if not u or not check_pw(password, u["pw_hash"]):
        _fails[ip] = recent + [now]
        db.audit(username or "?", "login_failed", ip=ip)
        raise HTTPException(401, "invalid username or password")
    _fails.pop(ip, None)
    db.audit(username, "login", ip=ip)
    tok = sign({"u": username, "r": u["role"], "exp": now + SESSION_SEC, "k": "session"})
    return {"token": tok, "username": username, "role": u["role"], "expires_in": SESSION_SEC}


class User(dict):
    @property
    def name(self) -> str:
        return self["u"]

    @property
    def role(self) -> str:
        return self["r"]


def current_user(request: Request) -> User:
    tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    data = unsign(tok) if tok else None
    if not data or data.get("k") != "session":
        raise HTTPException(401, "login required")
    u = db.one("SELECT role, disabled FROM users WHERE username=?", data["u"])
    if not u or u["disabled"]:
        raise HTTPException(401, "user disabled")
    data["r"] = u["role"]
    return User(data)


def require(role: str) -> Callable[..., User]:
    def dep(user: User = Depends(current_user)) -> User:
        if ROLES.get(user.role, 0) < ROLES[role]:
            raise HTTPException(403, f"{role} role required")
        return user
    return dep


def file_token(version: str, name: str, kind: str, ttl: int = 3600) -> str:
    return sign({"k": "file", "v": version, "n": name, "f": kind, "exp": time.time() + ttl})


def check_file_token(tok: str, version: str, name: str, kind: str) -> bool:
    d = unsign(tok)
    return bool(d and d.get("k") == "file" and d.get("v") == version and d.get("n") == name and d.get("f") == kind)


SECRET_KEY_RE = ("PASSWORD", "PASSWD", "_PASS", "TOKEN", "SECRET")


def is_secret_key(k: str) -> bool:
    ku = k.upper()
    return any(ku.endswith(s) or s in ku for s in SECRET_KEY_RE)
