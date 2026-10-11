"""Software Manager API (branch-local): releases, nodes, deployments, jobs, logs, settings, self-update + React UI."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import agents, central, db, deploy, health, manifest, registry, releases, support
from .config import BACKUPS, STATIC, settings
from .security import (ROLES, User, bootstrap_admin, check_file_token, check_pw, current_user, encrypt, hash_pw,
                       login, require)

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("software-manager")
_URL_TOKEN_RE = re.compile(r"([?&](?:token|t)=)[^&\s\"]+")


class _MaskUrlTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_URL_TOKEN_RE.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_MaskUrlTokens())
VERSION = "1.0.0"
app = FastAPI(title="Software Manager", version=VERSION)
viewer, operator, admin = require("viewer"), require("operator"), require("admin")


@app.on_event("startup")
def _startup() -> None:
    bootstrap_admin()
    deploy.mark_interrupted()
    for r in db.q("SELECT version, status FROM releases WHERE status IN ('scanning','downloading')"):
        db.ex("UPDATE releases SET status=? WHERE version=?",
              "uploaded" if r["status"] == "scanning" else "download_failed", r["version"])
    releases.cleanup_partials()
    for x in releases.import_local():
        log.info("release import %s: %s", x["version"], x["result"])
    health.start_thread()
    central.start_thread()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


@app.get("/health")
def health_ep() -> dict:
    return {"service": "software-manager", "status": "ok", "version": VERSION, "branch": settings.branch_name}


# ------------------------------------------------------------------------------------------------ auth

class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def api_login(body: LoginBody, request: Request) -> dict:
    return login(body.username, body.password, _ip(request))


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"username": user.name, "role": user.role, "branch": settings.branch_name, "version": VERSION}


class PwBody(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


@app.post("/api/auth/password")
def change_pw(body: PwBody, user: User = Depends(current_user)) -> dict:
    u = db.one("SELECT * FROM users WHERE username=?", user.name)
    if not u or not check_pw(body.old_password, u["pw_hash"]):
        raise HTTPException(400, "current password is wrong")
    db.ex("UPDATE users SET pw_hash=? WHERE username=?", hash_pw(body.new_password), user.name)
    db.audit(user.name, "password_change")
    return {"ok": True}


class UserBody(BaseModel):
    username: str = ""
    password: str = ""
    role: str = "viewer"
    disabled: bool = False


@app.get("/api/users")
def users(_: User = Depends(admin)) -> list[dict]:
    return db.q("SELECT username, role, disabled, created_at FROM users ORDER BY username")


@app.post("/api/users")
def add_user(body: UserBody, user: User = Depends(admin)) -> dict:
    if body.role not in ROLES or not body.username or len(body.password) < 8:
        raise HTTPException(422, "username, role (viewer/operator/admin) and a password of 8+ characters are required")
    if db.one("SELECT 1 AS x FROM users WHERE username=?", body.username):
        raise HTTPException(409, "user exists")
    db.ex("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)", body.username, hash_pw(body.password),
          body.role, time.time())
    db.audit(user.name, "user_add", body.username, {"role": body.role})
    return {"ok": True}


@app.put("/api/users/{username}")
def edit_user(username: str, body: UserBody, user: User = Depends(admin)) -> dict:
    if not db.one("SELECT 1 AS x FROM users WHERE username=?", username):
        raise HTTPException(404, "no such user")
    if body.role not in ROLES:
        raise HTTPException(422, "bad role")
    if username == user.name and (body.role != "admin" or body.disabled):
        raise HTTPException(422, "you cannot demote or disable yourself")
    if body.password and len(body.password) < 8:
        raise HTTPException(422, "password must be 8+ characters")
    db.ex("UPDATE users SET role=?, disabled=? WHERE username=?", body.role, int(body.disabled), username)
    if body.password:
        db.ex("UPDATE users SET pw_hash=? WHERE username=?", hash_pw(body.password), username)
    db.audit(user.name, "user_edit", username, {"role": body.role, "disabled": body.disabled,
                                                "password_reset": bool(body.password)})
    return {"ok": True}


@app.delete("/api/users/{username}")
def del_user(username: str, user: User = Depends(admin)) -> dict:
    if username == user.name:
        raise HTTPException(422, "you cannot delete yourself")
    db.ex("DELETE FROM users WHERE username=?", username)
    db.audit(user.name, "user_delete", username)
    return {"ok": True}


# -------------------------------------------------------------------------------------------- releases

def _rel_summary(r: dict) -> dict:
    scan = json.loads(r.get("scan_json") or "{}")
    return {k: r[k] for k in ("version", "software_name", "description", "manifest_sha256", "uploaded_by",
                              "uploaded_at", "status", "updated_at")} | {
        "progress": json.loads(r.get("progress_json") or "{}"), "scan_ok": scan.get("ok"),
        "used_by": [d["name"] for d in db.q("SELECT name FROM deployments WHERE version=? OR prev_version=?",
                                            r["version"], r["version"])],
    }


@app.get("/api/releases")
def list_releases(_: User = Depends(viewer)) -> list[dict]:
    return [_rel_summary(r) for r in db.q("SELECT * FROM releases ORDER BY uploaded_at DESC")]


@app.get("/api/releases/{version}")
def get_release(version: str, _: User = Depends(viewer)) -> dict:
    r = db.one("SELECT * FROM releases WHERE version=?", version)
    if not r:
        raise HTTPException(404, "release not found")
    m, _e, _w = manifest.parse(r["manifest_text"])
    return {**_rel_summary(r), "manifest_text": r["manifest_text"], "scan": json.loads(r["scan_json"] or "{}"),
            "resolved": json.loads(r["resolved_json"] or "{}"), "manifest": m.to_dict() if m else None}


class ManifestBody(BaseModel):
    text: str


@app.post("/api/releases/upload")
async def upload_release(request: Request, user: User = Depends(operator), file: Optional[UploadFile] = File(None)) -> dict:
    if file is not None:
        raw = await file.read(2_000_001)
    else:
        try:
            raw = ManifestBody(**(await request.json())).text.encode()
        except Exception:
            raise HTTPException(422, "send a manifest file (multipart 'file') or JSON {text}")
    if len(raw) > 2_000_000:
        raise HTTPException(413, "manifest too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "manifest must be UTF-8 text")
    res = await run_in_threadpool(releases.upload, text, user.name, True)
    if not res["ok"]:
        return JSONResponse(res, status_code=422)
    return res


@app.get("/api/release-source")
def release_source(_: User = Depends(viewer)) -> dict:
    return releases.source_list()


class ImportBody(BaseModel):
    download: bool = True


@app.post("/api/release-source/{folder:path}/import")
def release_source_import(folder: str, body: ImportBody, user: User = Depends(operator)) -> dict:
    try:
        res = releases.import_source(folder, user.name, body.download)
    except releases.ScanError as exc:
        raise HTTPException(422, str(exc))
    db.audit(user.name, "release_import", folder, {"download": body.download, "version": res.get("version")})
    return res


class BundleBody(BaseModel):
    prereq: str
    versions: list[str] = []


@app.get("/api/bundles")
def bundles_list(_: User = Depends(viewer)) -> list[dict]:
    return releases.bundle_list()


@app.post("/api/bundles")
def bundles_build(body: BundleBody, user: User = Depends(operator)) -> dict:
    try:
        return {"ok": True, "name": releases.bundle_start(body.prereq, body.versions, user.name)}
    except releases.ScanError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/bundles/{name:path}")
def bundles_download(name: str, user: User = Depends(operator)) -> FileResponse:
    try:
        p = releases.bundle_path(name)
    except releases.ScanError as exc:
        raise HTTPException(404, str(exc))
    db.audit(user.name, "bundle_download", name)
    return FileResponse(p, filename=p.name, media_type="application/x-tar")


@app.delete("/api/bundles/{name:path}")
def bundles_delete(name: str, user: User = Depends(admin)) -> dict:
    try:
        releases.bundle_delete(name)
    except releases.ScanError as exc:
        raise HTTPException(404, str(exc))
    db.audit(user.name, "bundle_delete", name)
    return {"ok": True}


@app.post("/api/releases/import-local")
def releases_import_local(user: User = Depends(admin)) -> dict:
    res = releases.import_local()
    db.audit(user.name, "release_import_local", "", {"result": res})
    return {"ok": True, "result": res}


# ------------------------------------------------------------------ release catalog (office -> branches)

def _catalog_auth(request: Request) -> None:
    if not central.check_catalog_key(request.headers.get(central.KEY_HEADER)):
        db.audit("branch", "catalog_denied", request.url.path, ip=_ip(request))
        raise HTTPException(401, "branch key missing or wrong")


@app.get("/api/catalog")
def catalog_list(request: Request) -> list[dict]:
    _catalog_auth(request)
    return central.catalog()


@app.post("/api/catalog/key")
def catalog_key_new(user: User = Depends(admin)) -> dict:
    key = central.new_catalog_key()
    db.audit(user.name, "catalog_key_new")
    return {"key": key}


@app.delete("/api/catalog/key")
def catalog_key_off(user: User = Depends(admin)) -> dict:
    db.kv_set("catalog_key_sha256", None)
    db.audit(user.name, "catalog_key_off")
    return {"ok": True}


@app.get("/api/catalog/{version}")
def catalog_release(version: str, request: Request) -> dict:
    _catalog_auth(request)
    try:
        return central.catalog_release(version)
    except KeyError:
        raise HTTPException(404, "release not found")


@app.get("/api/catalog/{version}/files")
def catalog_files(version: str, request: Request) -> list[dict]:
    _catalog_auth(request)
    try:
        central.catalog_release(version)
    except KeyError:
        raise HTTPException(404, "release not found")
    return central.catalog_files(version)


@app.get("/api/catalog/{version}/file")
def catalog_file(version: str, path: str, request: Request) -> FileResponse:
    _catalog_auth(request)
    try:
        central.catalog_release(version)
        p = central.catalog_file(version, path)
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


@app.get("/api/central")
def central_status(_: User = Depends(viewer)) -> dict:
    return central.status()


class CentralBody(BaseModel):
    url: Optional[str] = None
    key: Optional[str] = None
    ca_pem: Optional[str] = None


@app.put("/api/central")
def central_set(body: CentralBody, user: User = Depends(admin)) -> dict:
    try:
        central.set_central(body.url, body.key, body.ca_pem)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.audit(user.name, "central_settings", body.url or "", {"key": body.key is not None, "ca": body.ca_pem is not None})
    return {"ok": True}


@app.post("/api/central/check")
def central_check(_: User = Depends(operator)) -> dict:
    central.check_once()
    return central.status()


@app.post("/api/central/{version}/download")
def central_download(version: str, user: User = Depends(operator)) -> dict:
    try:
        started = central.start_download(version, user.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"ok": True, "started": started}


@app.post("/api/releases/{version}/scan")
def rescan(version: str, user: User = Depends(operator)) -> dict:
    r = db.one("SELECT status FROM releases WHERE version=?", version)
    if not r:
        raise HTTPException(404, "release not found")
    if r["status"] in ("downloaded", "downloading"):
        raise HTTPException(409, f"release is {r['status']}")
    if not releases.start_bg(releases.scan, version):
        raise HTTPException(409, "already running")
    db.audit(user.name, "release_rescan", version)
    return {"ok": True}


@app.post("/api/releases/{version}/download")
def download_release(version: str, user: User = Depends(operator)) -> dict:
    r = db.one("SELECT status, scan_json FROM releases WHERE version=?", version)
    if not r:
        raise HTTPException(404, "release not found")
    if r["status"] not in ("scanned", "download_failed") or not json.loads(r["scan_json"] or "{}").get("ok"):
        raise HTTPException(409, f"release must pass the scan first (status {r['status']})")
    if not releases.start_bg(releases.download, version):
        raise HTTPException(409, "already running")
    db.audit(user.name, "release_download_start", version)
    return {"ok": True}


@app.delete("/api/releases/{version}")
def delete_release(version: str, user: User = Depends(admin)) -> dict:
    used = db.q("SELECT name FROM deployments WHERE version=? OR prev_version=?", version, version)
    if used:
        raise HTTPException(409, f"release is used by deployment(s) {[d['name'] for d in used]} (current or rollback)")
    try:
        releases.delete(version)
    except KeyError:
        raise HTTPException(404, "release not found")
    except releases.ScanError as exc:
        raise HTTPException(409, str(exc))
    db.audit(user.name, "release_delete", version)
    return {"ok": True}


@app.get("/api/releases/{version}/diff")
def release_diff(version: str, frm: str, _: User = Depends(viewer)) -> dict:
    try:
        a, ma = releases.load(frm)
        b, mb = releases.load(version)
    except KeyError as exc:
        raise HTTPException(404, f"release {exc} not found")
    except releases.ScanError as exc:
        raise HTTPException(422, str(exc))
    da = {k: v.get("digest", "") for k, v in json.loads(a["resolved_json"] or "{}").items()}
    dbb = {k: v.get("digest", "") for k, v in json.loads(b["resolved_json"] or "{}").items()}
    return manifest.diff(ma, mb, da, dbb)


@app.get("/api/agent-files/{version}/{name}/{kind}")
@app.get("/api/releases/{version}/files/{name}/{kind}")
def agent_file(version: str, name: str, kind: str, t: str = "") -> FileResponse:
    if kind not in ("scripts", "image", "datafile") or not check_file_token(t, version, name, kind):
        raise HTTPException(403, "invalid or expired file token")
    try:
        p = releases.file_path(version, name, kind)
    except FileNotFoundError:
        raise HTTPException(404, "file not in release store")
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


@app.get("/api/agent-backups/{rel:path}")
def agent_backup(rel: str, t: str = "") -> FileResponse:
    if not check_file_token(t, "backup", rel, "backup"):
        raise HTTPException(403, "invalid or expired file token")
    p = (BACKUPS / rel).resolve()
    if BACKUPS.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "backup not found")
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


# ----------------------------------------------------------------------------------------------- nodes

def _node_out(n: dict) -> dict:
    out = {k: v for k, v in n.items() if k not in ("token_enc", "info_json", "pods_json")}
    out["info"] = json.loads(n.get("info_json") or "{}")
    out["pods"] = json.loads(n.get("pods_json") or "[]")
    return out


@app.get("/api/nodes")
def list_nodes(_: User = Depends(viewer)) -> list[dict]:
    return [_node_out(n) for n in db.q("SELECT * FROM nodes ORDER BY id")]


class NodeBody(BaseModel):
    name: str
    ip: str
    port: int = Field(default_factory=lambda: settings.sm_agent_port)
    token: str = ""


def _node(nid: int) -> dict:
    try:
        return agents.node(nid)
    except agents.AgentError:
        raise HTTPException(404, "node not found")


def _pod_name(pod: str) -> str:
    if not manifest.NAME_RE.match(pod):
        raise HTTPException(422, "bad pod name")
    return pod


@app.post("/api/nodes")
def add_node(body: NodeBody, user: User = Depends(admin)) -> dict:
    if not body.token:
        raise HTTPException(422, "node token is required (printed by prereq.sh / in the agent's data/node-token)")
    probe = {"id": 0, "name": body.name, "ip": body.ip, "port": body.port, "token_enc": encrypt(body.token)}
    try:
        info = agents.call(probe, "GET", "/api/info", timeout=30)
    except agents.AgentError as exc:
        raise HTTPException(422, f"cannot reach agent: {exc}")
    try:
        nid = db.ex("INSERT INTO nodes(name,ip,port,token_enc,added_at,info_json,status,last_seen) VALUES(?,?,?,?,?,?,?,?)",
                    body.name, body.ip, body.port, encrypt(body.token), time.time(), json.dumps(info), "online",
                    time.time())
    except Exception:
        raise HTTPException(409, "a node with this IP and port already exists")
    db.audit(user.name, "node_add", f"{body.name} {body.ip}:{body.port}")
    return {"ok": True, "id": nid, "info": info}


@app.put("/api/nodes/{nid}")
def edit_node(nid: int, body: NodeBody, user: User = Depends(admin)) -> dict:
    n = _node(nid)
    tok = body.token or None
    probe = {**n, "ip": body.ip, "port": body.port, "token_enc": encrypt(tok) if tok else n["token_enc"]}
    try:
        agents.call(probe, "GET", "/api/info", timeout=30)
    except agents.AgentError as exc:
        raise HTTPException(422, f"cannot reach agent: {exc}")
    try:
        db.ex("UPDATE nodes SET name=?, ip=?, port=?, token_enc=? WHERE id=?", body.name, body.ip, body.port,
              probe["token_enc"], nid)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "a node with this IP and port already exists")
    db.audit(user.name, "node_edit", str(nid), {"ip": body.ip, "port": body.port, "token_changed": bool(tok)})
    return {"ok": True}


@app.delete("/api/nodes/{nid}")
def del_node(nid: int, user: User = Depends(admin)) -> dict:
    for d in db.q("SELECT name, map_json FROM deployments"):
        if nid in deploy.placement(json.loads(d["map_json"])).values():
            raise HTTPException(409, f"node is used by deployment '{d['name']}'")
    db.ex("DELETE FROM nodes WHERE id=?", nid)
    db.audit(user.name, "node_delete", str(nid))
    return {"ok": True}


@app.post("/api/nodes/{nid}/refresh")
def refresh_node(nid: int, _: User = Depends(viewer)) -> dict:
    health.poll_node(_node(nid), True)
    return _node_out(_node(nid))


def _agent(nid: int, method: str, path: str, **kw: Any) -> Any:
    n = _node(nid)
    try:
        return agents.call(n, method, path, **kw)
    except agents.AgentError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/nodes/{nid}/pods/{pod}/logs")
def pod_logs(nid: int, pod: str, tail: int = 500, _: User = Depends(viewer)) -> Response:
    out = _agent(nid, "GET", f"/api/pods/{_pod_name(pod)}/logs?tail={tail}", timeout=60)
    return Response(support.mask(out if isinstance(out, str) else json.dumps(out)), media_type="text/plain")


@app.get("/api/nodes/{nid}/pods/{pod}/inspect")
def pod_inspect(nid: int, pod: str, _: User = Depends(viewer)) -> Any:
    return support.mask_obj(_agent(nid, "GET", f"/api/pods/{_pod_name(pod)}/inspect"))


@app.get("/api/nodes/{nid}/pods/{pod}/values")
def pod_values(nid: int, pod: str, _: User = Depends(operator)) -> dict:
    from .security import is_secret_key
    data = _agent(nid, "GET", f"/api/pods/{_pod_name(pod)}/values")
    if not isinstance(data, dict):
        raise HTTPException(502, "unexpected reply from agent")
    data["values"] = {k: ("***" if is_secret_key(k) and not k.endswith("_FILE") else v)
                      for k, v in data.get("values", {}).items()}
    return data


class ValuesBody(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


@app.put("/api/nodes/{nid}/pods/{pod}/values")
def put_pod_values(nid: int, pod: str, body: ValuesBody, user: User = Depends(admin)) -> dict:
    res = _agent(nid, "PUT", f"/api/pods/{_pod_name(pod)}/values", json=body.model_dump())
    db.audit(user.name, "values_edit", f"{nid}/{pod}", {"keys": list(body.values), "secrets": list(body.secrets)})
    return res


@app.post("/api/nodes/{nid}/prune")
def prune_node(nid: int, user: User = Depends(operator)) -> dict:
    res = _agent(nid, "POST", "/api/prune", timeout=600)
    db.audit(user.name, "node_prune", str(nid), res)
    return res


# ----------------------------------------------------------------------------------------- deployments

def _dep(did: int) -> dict:
    d = db.one("SELECT * FROM deployments WHERE id=?", did)
    if not d:
        raise HTTPException(404, "deployment not found")
    return d


def _dep_out(d: dict) -> dict:
    mp = json.loads(d["map_json"])
    pl = deploy.placement(mp)
    nodes = {n["id"]: n for n in db.q("SELECT * FROM nodes")}
    pods = []
    try:
        _r, m = releases.load(d["version"])
        names = [i.name for i in deploy.pods_of(m)]
        resolved = json.loads(_r["resolved_json"] or "{}")
    except (KeyError, releases.ScanError):
        names, resolved = list(pl), {}
    for name in names:
        nid = pl.get(name)
        n = nodes.get(nid) or {}
        st = next((p for p in json.loads(n.get("pods_json") or "[]") if p["name"] == name), {})
        pods.append({"name": name, "node_id": nid, "node": n.get("name"), "node_ip": n.get("ip"),
                     "port": deploy.pod_port(name, resolved, mp), "status": st})
    return {**{k: v for k, v in d.items() if k != "map_json"}, "map": mp, "pods": pods, "busy_job": deploy.busy(d["id"])}


@app.get("/api/deployments")
def list_deps(_: User = Depends(viewer)) -> list[dict]:
    return [_dep_out(d) for d in db.q("SELECT * FROM deployments ORDER BY id")]


@app.get("/api/deployments/{did}")
def get_dep(did: int, _: User = Depends(viewer)) -> dict:
    return _dep_out(_dep(did))


class MapBody(BaseModel):
    version: str = ""
    name: str = ""
    alarm_base_path: str = ""
    map: dict = Field(default_factory=dict)
    id: Optional[int] = None


@app.post("/api/deployments/validate")
def validate_dep(body: MapBody, _: User = Depends(viewer)) -> dict:
    abp = body.alarm_base_path
    if body.id and not abp:
        abp = _dep(body.id)["alarm_base_path"]
    return deploy.validate(body.version, body.map, abp, exclude_dep=body.id)


@app.post("/api/deployments")
def create_dep(body: MapBody, user: User = Depends(operator)) -> dict:
    if not body.name or not manifest.NAME_RE.match(body.name):
        raise HTTPException(422, "deployment name: lowercase letters, digits, '-', '_' or '.'")
    if db.one("SELECT 1 AS x FROM deployments WHERE name=?", body.name):
        raise HTTPException(409, "deployment name exists")
    v = deploy.validate(body.version, body.map, body.alarm_base_path)
    if not v["ok"]:
        raise HTTPException(422, "; ".join(v["errors"]))
    _r, m = releases.load(body.version)
    now = time.time()
    did = db.ex("INSERT INTO deployments(name,software_name,version,alarm_base_path,map_json,state,created_by,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", body.name, m.software_name, body.version,
                body.alarm_base_path.rstrip("/"), json.dumps(body.map), "new", user.name, now, now)
    jid = deploy.wf_install(_dep(did), user.name)
    return {"ok": True, "id": did, "job": jid, "warnings": v["warnings"]}


@app.post("/api/deployments/{did}/install")
def reinstall(did: int, user: User = Depends(operator)) -> dict:
    return {"job": deploy.wf_install(_dep(did), user.name)}


class ActionBody(BaseModel):
    action: str
    pods: list[str] = Field(default_factory=list)


@app.post("/api/deployments/{did}/action")
def dep_action(did: int, body: ActionBody, user: User = Depends(operator)) -> dict:
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(422, "action must be start, stop or restart")
    return {"job": deploy.wf_action(_dep(did), body.action, user.name, body.pods or None)}


class PodActionBody(BaseModel):
    action: str


@app.post("/api/deployments/{did}/pods/{pod}/action")
def pod_action(did: int, pod: str, body: PodActionBody, user: User = Depends(operator)) -> dict:
    d = _dep(did)
    nid = deploy.placement(json.loads(d["map_json"])).get(pod)
    if not nid:
        raise HTTPException(404, "pod not in deployment")
    if body.action in ("start", "stop", "restart"):
        return {"job": deploy.wf_action(d, body.action, user.name, [pod])}
    if body.action == "rollback":
        return {"job": deploy.create_job(did, "pod_rollback", [{"kind": "rollback", "pod": pod, "node_id": nid}],
                                         {"pods": [pod]}, user.name)}
    raise HTTPException(422, "action must be start, stop, restart or rollback")


class UpdateBody(BaseModel):
    version: str
    map: Optional[dict] = None


@app.post("/api/deployments/{did}/update")
def dep_update(did: int, body: UpdateBody, user: User = Depends(operator)) -> dict:
    d = _dep(did)
    if body.version == d["version"]:
        raise HTTPException(422, "already on this version")
    try:
        return {"job": deploy.wf_update(d, body.version, user.name, body.map)}
    except KeyError as exc:
        raise HTTPException(404, f"release {exc} not found")


@app.post("/api/deployments/{did}/rollback")
def dep_rollback(did: int, user: User = Depends(operator)) -> dict:
    d = _dep(did)
    if not d["prev_version"]:
        raise HTTPException(409, "no previous version recorded")
    return {"job": deploy.wf_update(d, d["prev_version"], user.name, kind="rollback")}


class ApplyMapBody(BaseModel):
    map: dict


@app.put("/api/deployments/{did}/map")
def dep_map(did: int, body: ApplyMapBody, user: User = Depends(operator)) -> dict:
    return {"job": deploy.wf_apply_map(_dep(did), body.map, user.name)}


@app.get("/api/deployments/{did}/wiring")
def dep_wiring(did: int, _: User = Depends(viewer)) -> dict:
    d = _dep(did)
    r, _m = releases.load(d["version"])
    resolved = json.loads(r["resolved_json"] or "{}")
    mp = json.loads(d["map_json"])
    return {p: deploy.managed_env(p, resolved, mp) for p in deploy.placement(mp)}


class DeleteBody(BaseModel):
    confirm_name: str
    remove_data: bool = False
    confirm_data: str = ""


@app.post("/api/deployments/{did}/delete")
def dep_delete(did: int, body: DeleteBody, user: User = Depends(admin)) -> dict:
    d = _dep(did)
    if body.confirm_name != d["name"]:
        raise HTTPException(422, "type the deployment name to confirm")
    if body.remove_data and body.confirm_data != "DELETE DATA":
        raise HTTPException(422, "type DELETE DATA to also delete pod data (database, recordings, configs)")
    return {"job": deploy.wf_delete(d, user.name, body.remove_data)}


@app.get("/api/deployments/{did}/backups")
def dep_backups(did: int, _: User = Depends(viewer)) -> list[dict]:
    d = _dep(did)
    base = BACKUPS / d["name"]
    out = []
    if base.is_dir():
        for p in sorted(base.iterdir(), reverse=True):
            meta = p / "meta.json"
            if meta.exists():
                out.append({"id": p.name, **json.loads(meta.read_text())})
    return out


@app.post("/api/deployments/{did}/backup")
def dep_backup(did: int, user: User = Depends(operator)) -> dict:
    d = _dep(did)
    nid = deploy.placement(json.loads(d["map_json"])).get("postgres-database")
    if not nid:
        raise HTTPException(409, "deployment has no postgres-database pod")
    return {"job": deploy.create_job(did, "backup", [{"kind": "backup", "pod": "postgres-database", "node_id": nid}],
                                     {}, user.name)}


class RestoreBody(BaseModel):
    backup: str
    confirm_name: str


@app.post("/api/deployments/{did}/restore")
def dep_restore(did: int, body: RestoreBody, user: User = Depends(admin)) -> dict:
    d = _dep(did)
    if body.confirm_name != d["name"]:
        raise HTTPException(422, "type the deployment name to confirm the restore (current data is replaced)")
    if "/" in body.backup or ".." in body.backup:
        raise HTTPException(422, "bad backup id")
    return {"job": deploy.wf_restore(d, body.backup, user.name)}


# ------------------------------------------------------------------------------------------------ jobs

def _job_out(j: dict, full: bool = False) -> dict:
    out = {k: v for k, v in j.items() if k not in ("steps_json", "params_json", "log_text")}
    steps = json.loads(j["steps_json"])
    out["params"] = {k: v for k, v in json.loads(j["params_json"]).items() if k != "map"}
    out["progress"] = {"done": sum(1 for s in steps if s["status"] == "ok"), "total": len(steps)}
    if full:
        names = {n["id"]: n["name"] for n in db.q("SELECT id, name FROM nodes")}
        for s in steps:
            if s.get("node_id"):
                s["node_name"] = names.get(s["node_id"], f"node {s['node_id']}")
        out["steps"] = steps
        out["log"] = j["log_text"]
    return out


@app.get("/api/jobs")
def list_jobs(deployment_id: Optional[int] = None, limit: int = 100, _: User = Depends(viewer)) -> list[dict]:
    if deployment_id:
        rows = db.q("SELECT * FROM jobs WHERE deployment_id=? ORDER BY id DESC LIMIT ?", deployment_id, limit)
    else:
        rows = db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", limit)
    return [_job_out(j) for j in rows]


@app.get("/api/jobs/{jid}")
def get_job(jid: int, _: User = Depends(viewer)) -> dict:
    j = db.one("SELECT * FROM jobs WHERE id=?", jid)
    if not j:
        raise HTTPException(404, "job not found")
    return _job_out(j, True)


@app.post("/api/jobs/{jid}/resume")
def resume_job(jid: int, user: User = Depends(operator)) -> dict:
    deploy.resume(jid, user.name)
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ logs

def _logger(path: str, params: dict, timeout: float = 60) -> httpx.Response:
    tok = support.logger_token()
    if not tok:
        raise HTTPException(503, "logger token not configured (Settings > Logger)")
    try:
        r = httpx.get(f"{support.logger_url()}{path}",
                      params={k: v for k, v in params.items() if v not in (None, "") and k != "token"},
                      headers={"X-Logger-Token": tok}, timeout=timeout, verify=False)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"logger unreachable: {exc}")
    if r.status_code >= 400:
        raise HTTPException(502 if r.status_code in (401, 403) else r.status_code, f"logger: {r.text[:300]}")
    return r


@app.get("/api/logs")
def logs(request: Request, _: User = Depends(viewer)) -> Response:
    r = _logger("/api/logs", dict(request.query_params))
    return Response(r.content, media_type="application/json")


@app.get("/api/logs/tail")
def logs_tail(request: Request, _: User = Depends(viewer)) -> Response:
    r = _logger("/api/logs/tail", dict(request.query_params), timeout=20)
    return Response(r.content, media_type="application/json")


@app.get("/api/logs/services")
def logs_services(_: User = Depends(viewer)) -> Response:
    return Response(_logger("/api/services", {}).content, media_type="application/json")


@app.get("/api/logs/download")
def logs_download(request: Request, user: User = Depends(operator)) -> Response:
    params = {k: v for k, v in request.query_params.items() if k != "token"}
    r = _logger("/api/logs/download", params, timeout=600)
    db.audit(user.name, "logs_download", json.dumps(params))
    return Response(r.content, media_type=r.headers.get("content-type", "application/octet-stream"),
                    headers={"Content-Disposition": r.headers.get("content-disposition", "attachment")})


@app.get("/api/support-bundle")
def support_bundle(hours: int = 24, user: User = Depends(operator)) -> Response:
    data, name = support.build(max(1, min(hours, 24 * 14)))
    db.audit(user.name, "support_bundle", name)
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# -------------------------------------------------------------------------------------- settings & misc

SETTING_KEYS = {"data_root": str, "run_uid": str, "run_gid": str, "logger_url": str, "camera_count": int,
                "detect_fps_per_camera": float, "trivy_block_critical": bool, "manager_node_id": int,
                "mqtt_alert_user": str, "logger_url_for_agents": str, "fileserver_base_url": str,
                "fileserver_user": str}


@app.get("/api/settings")
def get_settings(_: User = Depends(viewer)) -> dict:
    out = {k: db.kv_get(k) for k in SETTING_KEYS}
    out.setdefault("data_root", None)
    out["data_root"] = deploy.data_root()
    out["run_uid"], out["run_gid"] = deploy.run_ids()
    out["logger_url"] = support.logger_url()
    out["logger_token_set"] = bool(support.logger_token())
    out["mqtt_alert_password_set"] = bool(db.kv_get("mqtt_alert_password_enc"))
    out["fileserver_password_set"] = bool(db.kv_get("fileserver_password_enc"))
    out["branch"] = settings.branch_name
    out["https"] = settings.sm_https
    return out


class SettingsBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    logger_token: Optional[str] = None
    mqtt_alert_password: Optional[str] = None
    fileserver_password: Optional[str] = None


@app.put("/api/settings")
def put_settings(body: SettingsBody, user: User = Depends(admin)) -> dict:
    for k, v in body.values.items():
        if k not in SETTING_KEYS:
            raise HTTPException(422, f"unknown setting {k}")
        try:
            if SETTING_KEYS[k] is bool and isinstance(v, str):
                if v.strip().lower() not in ("true", "false", "1", "0", "yes", "no", ""):
                    raise ValueError(v)
                v = v.strip().lower() in ("true", "1", "yes")
            val = SETTING_KEYS[k](v) if v not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(422, f"bad value for {k}")
        if k == "data_root" and val and (not str(val).startswith("/") or str(val).rstrip("/") in ("", "/")):
            raise HTTPException(422, "data_root must be an absolute path (not /)")
        db.kv_set(k, val)
    if body.logger_token is not None:
        db.kv_set("logger_token_enc", encrypt(body.logger_token))
    if body.mqtt_alert_password is not None:
        db.kv_set("mqtt_alert_password_enc", encrypt(body.mqtt_alert_password))
    if body.fileserver_password is not None:
        db.kv_set("fileserver_password_enc", encrypt(body.fileserver_password))
    db.audit(user.name, "settings_edit", "", {"keys": list(body.values), "logger_token": body.logger_token is not None})
    return {"ok": True}


@app.get("/api/registries")
def list_registries(_: User = Depends(viewer)) -> list[dict]:
    return [{"host": r["host"], "username": r["username"], "password_set": bool(r["password_enc"]),
             "ca": bool(r["ca_pem"]), "insecure": bool(r["insecure"])} for r in db.q("SELECT * FROM registries")]


class RegistryBody(BaseModel):
    host: str
    username: str = ""
    password: Optional[str] = None
    ca_pem: str = ""
    insecure: bool = False


@app.put("/api/registries")
def put_registry(body: RegistryBody, user: User = Depends(admin)) -> dict:
    if not body.host.strip() or "/" in body.host:
        raise HTTPException(422, "registry host (host[:port]) is required")
    old = db.one("SELECT * FROM registries WHERE host=?", body.host) or {}
    pw_enc = encrypt(body.password) if body.password is not None else old.get("password_enc", "")
    ca = body.ca_pem or old.get("ca_pem", "")
    db.ex("INSERT INTO registries(host,username,password_enc,ca_pem,insecure) VALUES(?,?,?,?,?) ON CONFLICT(host) DO "
          "UPDATE SET username=excluded.username, password_enc=excluded.password_enc, ca_pem=excluded.ca_pem,"
          " insecure=excluded.insecure", body.host, body.username, pw_enc, ca, int(body.insecure))
    db.audit(user.name, "registry_edit", body.host, {"username": body.username, "insecure": body.insecure})
    return {"ok": True}


@app.post("/api/registries/{host}/test")
def test_registry(host: str, repo: str, tag: str, _: User = Depends(operator)) -> dict:
    try:
        return {"ok": True, **registry.manifest_digest(host, repo, tag)}
    except registry.RegistryError as exc:
        return {"ok": False, "error": str(exc)}


@app.delete("/api/registries/{host}")
def del_registry(host: str, user: User = Depends(admin)) -> dict:
    db.ex("DELETE FROM registries WHERE host=?", host)
    db.audit(user.name, "registry_delete", host)
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = 500, q: str = "", _: User = Depends(admin)) -> list[dict]:
    if q:
        return db.q("SELECT * FROM audit WHERE username LIKE ? OR action LIKE ? OR target LIKE ? ORDER BY id DESC LIMIT ?",
                    f"%{q}%", f"%{q}%", f"%{q}%", limit)
    return db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", limit)


@app.get("/api/alerts")
def alerts(active: bool = True, _: User = Depends(viewer)) -> list[dict]:
    if active:
        return db.q("SELECT * FROM alerts WHERE active=1 ORDER BY last_ts DESC")
    return db.q("SELECT * FROM alerts ORDER BY last_ts DESC LIMIT 500")


@app.get("/api/overview")
def overview(_: User = Depends(viewer)) -> dict:
    nodes = db.q("SELECT status FROM nodes")
    deps = db.q("SELECT state FROM deployments")
    return {
        "branch": settings.branch_name, "version": VERSION,
        "nodes": {"total": len(nodes), "online": sum(1 for n in nodes if n["status"] == "online")},
        "deployments": {"total": len(deps), "running": sum(1 for d in deps if d["state"] == "running")},
        "releases": len(db.q("SELECT version FROM releases")),
        "alerts": len(db.q("SELECT key FROM alerts WHERE active=1")),
        "running_jobs": len(db.q("SELECT id FROM jobs WHERE status IN ('running','queued')")),
    }


class SelfUpdateBody(BaseModel):
    component: str
    image: str
    node_ids: list[int] = []


@app.post("/api/self-update")
def self_update(body: SelfUpdateBody, user: User = Depends(admin)) -> dict:
    if body.component not in ("sm-agent", "sm-logger", "software-manager"):
        raise HTTPException(422, "component must be sm-agent, sm-logger or software-manager")
    if ":" not in body.image.rsplit("/", 1)[-1] or "@" in body.image:
        raise HTTPException(422, "image must be registry/repo:tag")
    return {"job": deploy.wf_self_update(body.component, body.image, user.name, body.node_ids)}


# ------------------------------------------------------------------------------------------------- SPA

if (STATIC / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str) -> Response:
    if path.startswith("api/"):
        raise HTTPException(404, "not found")
    f = (STATIC / path).resolve()
    if path and STATIC.resolve() in f.parents and f.is_file():
        return FileResponse(f)
    index = STATIC / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return Response("Software Manager API is running; UI not built (src/frontend).", media_type="text/plain")
