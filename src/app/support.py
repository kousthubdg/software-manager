"""Support bundle: one zip with everything needed to diagnose a branch, secrets masked."""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import agents, db
from .config import settings
from .security import decrypt, is_secret_key

MASK_RE = [
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)([\"']?\s*[=:]\s*[\"']?)([^\s,;\"']+)"),
     r"\1\2***"),
    (re.compile(r"(rtsp|rtsps|http|https)://([^:/@\s]+):([^@/\s]+)@"), r"\1://\2:***@"),
]


def mask(s: str) -> str:
    for rx, rep in MASK_RE:
        s = rx.sub(rep, s)
    return s


def mask_obj(obj: Any) -> Any:
    """mask() every string in a decoded JSON value; values under secret-looking keys become ***."""
    if isinstance(obj, dict):
        return {k: "***" if isinstance(k, str) and isinstance(v, str) and is_secret_key(k)
                and not k.upper().endswith("_FILE") else mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(x) for x in obj]
    return mask(obj) if isinstance(obj, str) else obj


def _j(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


def logger_url() -> str:
    return (db.kv_get("logger_url", "") or settings.sm_logger_url).rstrip("/")


def logger_token() -> str:
    if settings.logger_token:
        return settings.logger_token
    enc = db.kv_get("logger_token_enc", "")
    return decrypt(enc) if enc else ""


def build(hours: int = 24) -> tuple[bytes, str]:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", f"Yellow Metal Platform support bundle\nbranch: {settings.branch_name}\n"
                                 f"created: {stamp}\nlog window: last {hours} h\nsecrets are masked\n")
        z.writestr("manager/deployments.json", _j(db.q("SELECT * FROM deployments")))
        rels = db.q("SELECT version, software_name, status, manifest_sha256, uploaded_by, uploaded_at, scan_json,"
                    " resolved_json, manifest_text FROM releases")
        for r in rels:
            z.writestr(f"manager/releases/{r['version']}/manifest.yml", r.pop("manifest_text"))
            z.writestr(f"manager/releases/{r['version']}/release.json", _j(r))
        z.writestr("manager/jobs.json", _j(db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 200")))
        z.writestr("manager/audit.json", _j(db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 2000")))
        z.writestr("manager/alerts.json", _j(db.q("SELECT * FROM alerts ORDER BY last_ts DESC")))
        nodes = db.q("SELECT id, name, ip, port, status, last_seen, last_error, info_json, pods_json FROM nodes")
        z.writestr("manager/nodes.json", _j(nodes))
        for n in db.q("SELECT * FROM nodes"):
            base = f"nodes/{n['name']}_{n['ip']}"
            try:
                z.writestr(f"{base}/info.json", _j(agents.call(n, "GET", "/api/info", timeout=30)))
                pods = agents.call(n, "GET", "/api/pods", timeout=20)
                z.writestr(f"{base}/pods.json", _j(pods))
                z.writestr(f"{base}/docker-events.json",
                           _j(agents.call(n, "GET", f"/api/docker-events?since_sec={hours * 3600}", timeout=60)))
            except agents.AgentError as exc:
                z.writestr(f"{base}/ERROR.txt", str(exc))
                continue
            for p in (pods.get("pods") or []) if isinstance(pods, dict) else []:
                name = p["name"]
                for path, kind in ((f"/api/pods/{name}/inspect", "inspect.json"), (f"/api/pods/{name}/values", "values")):
                    try:
                        data = agents.call(n, "GET", path, timeout=30)
                    except agents.AgentError as exc:
                        z.writestr(f"{base}/{name}/{kind}.error.txt", str(exc))
                        continue
                    if kind == "values":
                        vals = {k: ("***" if is_secret_key(k) and not k.endswith("_FILE") else mask(v))
                                for k, v in data.get("values", {}).items()}
                        z.writestr(f"{base}/{name}/values.env",
                                   "".join(f"{k}={v}\n" for k, v in vals.items()))
                    else:
                        z.writestr(f"{base}/{name}/{kind}", mask(_j(data)))
                try:
                    z.writestr(f"{base}/{name}/container.log",
                               mask(agents.call(n, "GET", f"/api/pods/{name}/logs?tail=3000", timeout=60)))
                except agents.AgentError as exc:
                    z.writestr(f"{base}/{name}/container.log.error.txt", str(exc))
        tok = logger_token()
        if tok:
            frm = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            try:
                r = httpx.get(f"{logger_url()}/api/logs/download", params={"from": frm, "format": "zip"},
                              headers={"X-Logger-Token": tok}, timeout=300, verify=False)
                if r.status_code == 200:
                    with zipfile.ZipFile(io.BytesIO(r.content)) as lz:
                        for info in lz.infolist():
                            z.writestr(f"logs/{info.filename}", lz.read(info))
                else:
                    z.writestr("logs/ERROR.txt", f"logger HTTP {r.status_code}: {r.text[:500]}")
            except httpx.HTTPError as exc:
                z.writestr("logs/ERROR.txt", f"logger unreachable: {exc}")
        else:
            z.writestr("logs/ERROR.txt", "logger token not configured (Settings > Logger)")
    return buf.getvalue(), f"support_{settings.branch_name or 'branch'}_{stamp}.zip"
