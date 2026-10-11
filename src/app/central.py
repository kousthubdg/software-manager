"""Release catalog: an office Software Manager publishes its stored releases; branch Software Managers fetch them.

Office side: GET /api/catalog... (branch key in header X-Catalog-Key; the key is generated in Settings and only its
sha256 is kept). Serves the same files that a prerequisites package carries: release.json + the store folder.

Branch side (settings central_url + central key): a poller asks the office every SM_CENTRAL_POLL_SEC for its
catalog. New versions are downloaded (SM_CENTRAL_AUTO_DOWNLOAD, or on request) into SM_IMPORT_DIR/<version>/
{release.json, files/} and registered by releases.import_local() -> status downloaded, shown in every dropdown.
Installing stays a manual action on the Deployment page.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from . import db, manifest, releases
from .config import IMPORTS, RELEASES, settings
from .security import decrypt, encrypt

log = logging.getLogger("software-manager.central")
KEY_HEADER = "X-Catalog-Key"
_state: dict[str, Any] = {"last_check": None, "error": "", "available": [], "progress": {}}
_lock = threading.Lock()
_dl_lock = threading.Lock()
_wake = threading.Event()


# ---------------------------------------------------------------------------------------------- office side

def new_catalog_key() -> str:
    key = secrets.token_urlsafe(32)
    db.kv_set("catalog_key_sha256", hashlib.sha256(key.encode()).hexdigest())
    return key


def catalog_enabled() -> bool:
    return bool(db.kv_get("catalog_key_sha256"))


def check_catalog_key(key: Optional[str]) -> bool:
    want = db.kv_get("catalog_key_sha256")
    if not want or not key:
        return False
    return hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), want)


def _meta(r: dict) -> dict:
    return {k: r[k] for k in ("version", "software_name", "description", "manifest_text", "manifest_sha256",
                              "uploaded_by", "uploaded_at", "scan_json", "resolved_json")}


def catalog() -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM releases WHERE status='downloaded' ORDER BY uploaded_at DESC"):
        base = RELEASES / r["version"]
        if not base.is_dir():
            continue
        files = catalog_files(r["version"])
        out.append({"version": r["version"], "software_name": r["software_name"], "description": r["description"],
                    "manifest_sha256": r["manifest_sha256"], "uploaded_at": r["uploaded_at"],
                    "size": sum(f["size"] for f in files), "files": len(files)})
    return out


def catalog_release(version: str) -> dict:
    r = db.one("SELECT * FROM releases WHERE version=? AND status='downloaded'", version) \
        if releases.valid_version(version) else None
    if not r or not (RELEASES / version).is_dir():
        raise KeyError(version)
    return _meta(r)


def catalog_files(version: str) -> list[dict]:
    base = RELEASES / version
    return [{"path": p.relative_to(base).as_posix(), "size": p.stat().st_size}
            for p in sorted(base.rglob("*")) if p.is_file()]


def catalog_file(version: str, rel: str) -> Path:
    if not releases.valid_version(version) or not rel or rel.startswith("/") or "\\" in rel:
        raise FileNotFoundError(rel)
    base = (RELEASES / version).resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        raise FileNotFoundError(rel)
    return p


# ---------------------------------------------------------------------------------------------- branch side

def central_url() -> str:
    return (db.kv_get("central_url") or settings.sm_central_url or "").rstrip("/")


def central_key() -> str:
    enc = db.kv_get("central_key_enc")
    if enc:
        try:
            return decrypt(enc)
        except Exception:
            log.warning("central key cannot be decrypted (secret key changed?)")
            return ""
    if settings.sm_central_key_file:
        try:
            return Path(settings.sm_central_key_file).read_text().strip()
        except OSError:
            return ""
    return settings.sm_central_key


def set_central(url: Optional[str], key: Optional[str], ca_pem: Optional[str]) -> None:
    if url is not None:
        url = url.strip().rstrip("/")
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("office address must start with https:// or http://")
        db.kv_set("central_url", url or None)
    if key is not None:
        db.kv_set("central_key_enc", encrypt(key.strip()) if key.strip() else None)
    if ca_pem is not None:
        db.kv_set("central_ca_pem", ca_pem.strip() or None)
    wake()


def _verify() -> Any:
    pem = db.kv_get("central_ca_pem")
    if not pem:
        return False
    import ssl
    ctx = ssl.create_default_context(cadata=pem)
    ctx.check_hostname = False
    return ctx


def _client(timeout: float = 30) -> httpx.Client:
    return httpx.Client(base_url=central_url(), verify=_verify(), timeout=timeout,
                        headers={KEY_HEADER: central_key()}, follow_redirects=False)


def status() -> dict:
    with _lock:
        st = json.loads(json.dumps(_state))
    st.update(url=central_url(), key_set=bool(central_key()), ca_set=bool(db.kv_get("central_ca_pem")),
              poll_sec=settings.sm_central_poll_sec, auto_download=settings.sm_central_auto_download,
              catalog_enabled=catalog_enabled())
    return st


def _local(version: str) -> Optional[dict]:
    return db.one("SELECT status, manifest_sha256, software_name FROM releases WHERE version=?", version)


def check_once() -> None:
    url = central_url()
    if not url or not central_key():
        with _lock:
            _state.update(error="", available=[])
        return
    try:
        with _client() as c:
            r = c.get("/api/catalog")
            if r.status_code in (401, 403):
                raise RuntimeError("the office refused the branch key")
            r.raise_for_status()
            items = r.json()
        if not isinstance(items, list):
            raise RuntimeError("unexpected catalog reply")
    except Exception as exc:
        with _lock:
            _state.update(last_check=time.time(), error=f"{url}: {exc}")
        log.warning("release catalog check failed: %s", exc)
        return
    avail = []
    for it in items:
        v = it.get("version") if isinstance(it, dict) else None
        if not isinstance(v, str) or not manifest.VER_RE.match(v):
            continue
        loc = _local(v)
        if not loc:
            state = "available"
        elif loc["manifest_sha256"] == it.get("manifest_sha256") and loc["status"] == "downloaded":
            state = "downloaded"
        elif loc["manifest_sha256"] == it.get("manifest_sha256"):
            state = loc["status"]
        else:
            state = "conflict"
        avail.append({**{k: it.get(k) for k in ("version", "software_name", "description", "manifest_sha256",
                                                 "uploaded_at", "size")}, "state": state})
    with _lock:
        _state.update(last_check=time.time(), error="", available=avail)
    if settings.sm_central_auto_download:
        for a in avail:
            if a["state"] == "available":
                start_download(a["version"], "auto")


def start_download(version: str, user: str) -> bool:
    if not manifest.VER_RE.match(version):
        raise ValueError("bad version")
    loc = _local(version)
    if loc:
        raise ValueError(f"version {version} already exists here ({loc['status']})")
    with _lock:
        if version in _state["progress"] and _state["progress"][version].get("state") == "downloading":
            return False
        _state["progress"][version] = {"state": "queued", "done": 0, "total": 0, "current": "", "error": ""}
    db.audit(user, "central_download", version, {"from": central_url()})
    threading.Thread(target=_download, args=(version,), daemon=True, name=f"central-{version}").start()
    return True


def _prog(version: str, **kw: Any) -> None:
    with _lock:
        _state["progress"].setdefault(version, {}).update(kw)


def _download(version: str) -> None:
    with _dl_lock:
        tmp = IMPORTS / f".{version}.central"
        try:
            _prog(version, state="downloading", error="")
            shutil.rmtree(tmp, ignore_errors=True)
            (tmp / "files").mkdir(parents=True)
            with _client(timeout=60) as c:
                r = c.get(f"/api/catalog/{version}")
                r.raise_for_status()
                meta = r.json()
                if meta.get("version") != version:
                    raise RuntimeError("catalog returned another version")
                m, errors, _ = manifest.parse(meta["manifest_text"])
                if not m or m.sha256 != meta["manifest_sha256"]:
                    raise RuntimeError(f"manifest from the office is invalid: {'; '.join(errors)[:300]}")
                files = c.get(f"/api/catalog/{version}/files").raise_for_status().json()
                total = sum(int(f["size"]) for f in files)
                free = shutil.disk_usage(IMPORTS).free
                if free < total * 2 + settings.sm_min_free_gb * 1e9:
                    raise RuntimeError(f"not enough disk: need {total * 2 / 1e9:.1f} GB + "
                                       f"{settings.sm_min_free_gb} GB reserve, free {free / 1e9:.1f} GB")
                _prog(version, total=total, done=0)
                done = 0
                for f in files:
                    rel = str(f["path"])
                    dest = (tmp / "files" / rel).resolve()
                    if (tmp / "files").resolve() not in dest.parents:
                        raise RuntimeError(f"bad file path {rel!r} from the office")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _prog(version, current=rel)
                    with c.stream("GET", f"/api/catalog/{version}/file", params={"path": rel}) as resp:
                        resp.raise_for_status()
                        with open(dest, "wb") as out:
                            for chunk in resp.iter_bytes(1 << 20):
                                out.write(chunk)
                                done += len(chunk)
                                _prog(version, done=done)
                    if dest.stat().st_size != int(f["size"]):
                        raise RuntimeError(f"{rel}: size {dest.stat().st_size} != {f['size']}")
            _verify_files(tmp / "files", json.loads(meta.get("resolved_json") or "{}"))
            (tmp / "release.json").write_text(json.dumps(meta))
            final = IMPORTS / version
            if final.exists():
                raise RuntimeError(f"{final} already exists (import pending?)")
            tmp.replace(final)
            res = [x for x in releases.import_local() if x["version"] in (version, final.name)]
            result = res[0]["result"] if res else "not imported"
            if result not in ("imported", "already here"):
                shutil.rmtree(final, ignore_errors=True)
                raise RuntimeError(f"import: {result}")
            _prog(version, state="downloaded", current="")
            db.audit("system", "central_download", version, {"ok": True})
        except Exception as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            _prog(version, state="failed", error=str(exc)[:500])
            db.audit("system", "central_download", version, {"ok": False, "error": str(exc)[:500]})
            log.warning("release %s from the office failed: %s", version, exc)


def _verify_files(base: Path, resolved: dict) -> None:
    """md5 of image / scripts tars against what the office recorded when it stored the release."""
    for name, res in (resolved or {}).items():
        if not isinstance(res, dict) or not manifest.NAME_RE.match(str(name)):
            continue
        for fname, key in (("image.tar", "image_tar_md5"), ("scripts.tar", "scripts_md5")):
            want = res.get(key)
            p = base / name / fname
            if want and (not p.is_file() or releases.md5_file(p) != want):
                raise RuntimeError(f"{name}/{fname}: missing or md5 mismatch")
        if res.get("datafile_md5") and res.get("datafile_name"):
            p = base / name / "datafile" / str(res["datafile_name"])
            if not p.is_file() or releases.md5_file(p) != res["datafile_md5"]:
                raise RuntimeError(f"{name}/datafile: md5 mismatch")


def wake() -> None:
    _wake.set()


def _loop() -> None:
    time.sleep(5)
    while True:
        try:
            check_once()
        except Exception:
            log.exception("release catalog poll")
        _wake.wait(max(30.0, settings.sm_central_poll_sec))
        _wake.clear()


def cleanup() -> None:
    for p in IMPORTS.glob(".*.central"):
        shutil.rmtree(p, ignore_errors=True)


def start_thread() -> None:
    cleanup()
    threading.Thread(target=_loop, daemon=True, name="central-poller").start()
