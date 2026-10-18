"""Deployments: map validation, cross-node wiring, and fixed workflows run as resumable jobs.

Workflows (deterministic step lists, persisted after every step, one job per deployment at a time):
  install  check nodes -> configure agents -> prepare every pod -> start in manifest order (wait healthy)
  start / stop / restart   manifest order / reverse order
  update   diff releases -> pg_dump backup -> prepare+start changed/added pods -> down removed pods -> prune;
           any failure rolls the already-updated pods back (agent keeps the previous compose + image)
  rollback update to prev_version
  apply_map move pods between nodes, then restart pods whose wired addresses changed
  delete   down in reverse order (+ images, + data only with a second confirmation)
  restore  stop dependents -> pg_restore a backup -> start dependents
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

from . import agents, db, manifest, registry, releases, support
from .config import BACKUPS, public_url, settings
from .security import file_token

log = logging.getLogger("software-manager.deploy")
TOKEN_RE = re.compile(r"\{([a-z0-9][a-z0-9_.-]*)(?::(\d+))?\}")
PORT_TOKEN_RE = re.compile(r"\{port:([a-z0-9][a-z0-9_.-]*)\}")
DB_POD = "postgres-database"
_locks: dict[int, int] = {}
_locks_guard = threading.Lock()


class JobError(RuntimeError):
    """A job step cannot run (bad deployment / release data); fails the step with this message."""


def _names(csv: str) -> set[str]:
    return {x.strip() for x in csv.split(",") if x.strip()}


def heavy_pods() -> set[str]:
    return _names(settings.sm_heavy_pods)


def analytics_pods() -> set[str]:
    return _names(settings.sm_analytics_pods)


def data_root() -> str:
    return str(db.kv_get("data_root") or settings.sm_data_root).rstrip("/")


def run_ids() -> tuple[str, str]:
    return (str(db.kv_get("run_uid") or settings.sm_run_uid), str(db.kv_get("run_gid") or settings.sm_run_gid))


def _load(version: str, what: str = "release") -> tuple[dict, manifest.Manifest]:
    """releases.load for API workflows: unknown / unreadable release -> HTTP error instead of KeyError / 500."""
    try:
        return releases.load(version)
    except KeyError:
        raise HTTPException(404, f"{what} {version} not found in the release store")
    except releases.ScanError as exc:
        raise HTTPException(409, f"{what} {version} cannot be read: {exc}")


def placement(map_doc: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in map_doc.get("nodes", []):
        for pod in row.get("pods", []):
            out.setdefault(pod, int(row["node_id"]))
    return out


def map_errors(map_doc: Any) -> list[str]:
    """Shape check of a deployment map (before anything indexes into it)."""
    if not isinstance(map_doc, dict):
        return ["map must be an object"]
    errs: list[str] = []
    rows = map_doc.get("nodes", [])
    if not isinstance(rows, list):
        return ["map.nodes must be a list"]
    for row in rows:
        if not isinstance(row, dict):
            errs.append("map.nodes entries must be objects")
            continue
        try:
            int(row.get("node_id"))
        except (TypeError, ValueError):
            errs.append(f"bad node_id {row.get('node_id')!r}")
        pods = row.get("pods", [])
        if not isinstance(pods, list) or not all(isinstance(p, str) for p in pods):
            errs.append(f"pods of node {row.get('node_id')!r} must be a list of names")
    ovs = map_doc.get("overrides") or {}
    if not isinstance(ovs, dict):
        return errs + ["map.overrides must be an object"]
    for pod, ov in ovs.items():
        if not isinstance(ov, dict):
            errs.append(f"overrides of '{pod}' must be an object")
            continue
        sp = ov.get("service_port")
        if sp not in (None, ""):
            try:
                ok = 1 <= int(sp) <= 65535
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errs.append(f"overrides of '{pod}': service_port {sp!r} is not a port number")
    return errs


def pods_of(m: manifest.Manifest) -> list[manifest.Installer]:
    return [i for i in m.installers if not i.self_update]


def pod_port(pod: str, resolved: dict, map_doc: dict) -> Optional[int]:
    ov = (map_doc.get("overrides") or {}).get(pod) or {}
    p = ov.get("service_port") or (resolved.get(pod) or {}).get("port")
    return int(p) if p else None


def validate(version: str, map_doc: dict, alarm_base_path: str, exclude_dep: Optional[int] = None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    bad = map_errors(map_doc)
    if bad:
        return {"ok": False, "errors": bad, "warnings": []}
    try:
        r, m = releases.load(version)
    except KeyError:
        return {"ok": False, "errors": [f"release {version} not found"], "warnings": []}
    except releases.ScanError as exc:
        return {"ok": False, "errors": [f"release {version} cannot be read: {exc}"], "warnings": []}
    if r["status"] != "downloaded":
        errors.append(f"release {version} is not downloaded (status {r['status']})")
    resolved = json.loads(r["resolved_json"] or "{}")
    nodes = {n["id"]: n for n in db.q("SELECT * FROM nodes")}
    names = [i.name for i in pods_of(m)]
    counts: dict[str, int] = {}
    for row in map_doc.get("nodes", []):
        nid = int(row.get("node_id", 0))
        if nid not in nodes:
            errors.append(f"node {nid} is not registered")
            continue
        if nodes[nid]["status"] != "online":
            warnings.append(f"node {nodes[nid]['name']} ({nodes[nid]['ip']}) is {nodes[nid]['status']}")
        for pod in row.get("pods", []):
            counts[pod] = counts.get(pod, 0) + 1
            if pod not in names:
                errors.append(f"pod '{pod}' is not in release {version}")
    partial = bool(map_doc.get("partial"))
    skipped = [n for n in names if counts.get(n, 0) == 0]
    if partial and skipped:
        warnings.append(f"partial map: not deployed: {', '.join(skipped)}")
    if partial and not any(counts.values()):
        errors.append("partial map places no pods")
    for n in names:
        if counts.get(n, 0) == 0:
            if not partial:
                errors.append(f"pod '{n}' is not placed on any node")
        elif counts[n] > 1:
            errors.append(f"pod '{n}' is placed on {counts[n]} nodes (v1: exactly one instance per branch)")
    ports: dict[tuple[int, int], str] = {}
    pl = placement(map_doc)
    for pod, nid in pl.items():
        p = pod_port(pod, resolved, map_doc)
        if p is None:
            continue
        if (nid, p) in ports:
            errors.append(f"port {p} clash on {nodes.get(nid, {}).get('name', nid)}: {ports[(nid, p)]} and {pod}")
        ports[(nid, p)] = pod
    for d in db.q("SELECT * FROM deployments WHERE id<>?", exclude_dep or -1):
        dres = json.loads((db.one("SELECT resolved_json FROM releases WHERE version=?", d["version"]) or
                           {"resolved_json": "{}"})["resolved_json"])
        dmap = json.loads(d["map_json"])
        for pod, nid in placement(dmap).items():
            p = pod_port(pod, dres, dmap)
            if p and (nid, p) in ports:
                errors.append(f"port {p} on node {nid} is used by deployment '{d['name']}' ({pod})")
            if pod in pl:
                errors.append(f"pod '{pod}' already belongs to deployment '{d['name']}' (container names are unique per node)")
    root = data_root()
    abp = alarm_base_path.rstrip("/")
    if not abp.startswith("/"):
        errors.append("Alarm Base Path must be an absolute path")
    elif not (abp + "/").startswith(root + "/"):
        errors.append(f"Alarm Base Path must be under the data root {root} (agents only mount that path)")
    for pod, ov in (map_doc.get("overrides") or {}).items():
        if ov.get("gpu"):
            nid = pl.get(pod)
            info = json.loads(nodes.get(nid, {}).get("info_json") or "{}") if nid else {}
            if not info.get("gpu_nvidia"):
                errors.append(f"pod '{pod}' needs a GPU but node {nid} reports none")
    cams = int(db.kv_get("camera_count", 0) or 0)
    fps = float(db.kv_get("detect_fps_per_camera", 5) or 5)
    budget = settings.sm_capacity_budget
    analytics = analytics_pods()
    for nid in set(pl.values()):
        here = [p for p, n in pl.items() if n == nid and p in analytics]
        if here and cams and nid in nodes:
            info = json.loads(nodes[nid].get("info_json") or "{}")
            cap = float((info.get("bench") or {}).get("est_detect_fps") or 0)
            need = cams * fps
            if cap and need > cap * budget:
                warnings.append(f"capacity: node {nodes[nid]['name']} needs ~{need:.0f} detect fps for {cams} cameras "
                                f"x {fps} fps but measured capacity is ~{cap:.0f} fps "
                                f"({budget:.0%} budget {cap * budget:.0f})")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def managed_env(pod: str, resolved: dict, map_doc: dict) -> dict[str, str]:
    nodes = {n["id"]: n for n in db.q("SELECT id, ip FROM nodes")}
    pl = placement(map_doc)
    out: dict[str, str] = {}
    for key, tmpl in ((resolved.get(pod) or {}).get("wire") or {}).items():
        missing = False

        def port_rep(mo: re.Match) -> str:
            nonlocal missing
            p = pod_port(mo.group(1), resolved, map_doc) if mo.group(1) in pl else None
            if p is None:
                missing = True
                return mo.group(0)
            return str(p)

        def rep(mo: re.Match) -> str:
            nonlocal missing
            mod, dport = mo.group(1), mo.group(2)
            if mod not in pl or pl[mod] not in nodes:
                missing = True
                return mo.group(0)
            ip = nodes[pl[mod]]["ip"]
            if not dport:
                return ip
            return f"{ip}:{pod_port(mod, resolved, map_doc) or dport}"
        val = TOKEN_RE.sub(rep, PORT_TOKEN_RE.sub(port_rep, tmpl))
        if not missing:
            out[key] = val
    return out


def pod_spec(dep: dict, version: str, pod: str, map_doc: Optional[dict] = None) -> tuple[dict, dict]:
    """Runs inside job steps: problems raise JobError (step fails with the message), never HTTPException."""
    if not dep:
        raise JobError("deployment no longer exists")
    try:
        r, m = releases.load(version)
    except KeyError:
        raise JobError(f"release {version} is not in the release store")
    except releases.ScanError as exc:
        raise JobError(f"release {version} cannot be read: {exc}")
    resolved = json.loads(r["resolved_json"] or "{}")
    map_doc = map_doc or json.loads(dep["map_json"])
    inst = m.get(pod)
    if inst is None or inst.self_update:
        raise JobError(f"pod {pod} is not a deployable service of release {version}")
    res = resolved.get(pod)
    if not res or not res.get("scripts_md5"):
        raise JobError(f"release {version} has no stored check result for {pod} (check / download it again)")
    nid = placement(map_doc).get(pod)
    if nid is None:
        raise JobError(f"pod {pod} is not placed on a node in the deployment map")
    node = agents.node(nid)
    ov = (map_doc.get("overrides") or {}).get(pod) or {}
    uid, gid = run_ids()
    order = [i.name for i in pods_of(m)].index(pod)
    base = public_url()

    def url(kind: str) -> str:
        return f"{base}/api/agent-files/{version}/{pod}/{kind}?t={file_token(version, pod, kind)}"
    src = res.get("image_source") or inst.image_source
    spec = {
        "name": pod, "order": order, "release": version, "image_source": src, "digest": res.get("digest", ""),
        "image_id": res.get("image_id", ""),
        "image_tar_url": url("image") if src == "tar" else "",
        "image_tar_md5": res.get("image_tar_md5", ""),
        "registry_auth": registry.docker_auth(inst.registry_server) if inst.registry_server and src != "tar" else None,
        "scripts_url": url("scripts"), "scripts_md5": res["scripts_md5"], "scripts_ver": inst.scripts_ver,
        "datafile_url": url("datafile") if inst.has_datafile else "", "datafile_md5": res.get("datafile_md5", ""),
        "datafile_ver": inst.datafile_ver, "datafile_name": res.get("datafile_name", ""),
        "render": {
            "container_name": pod, "registry_server": inst.registry_server, "image": inst.repo,
            "version": inst.image_ver, "data_file_path": f"{data_root()}/{pod}/", "run_uid": uid, "run_gid": gid,
            "alarm_base_path": dep["alarm_base_path"], "node_ip": node["ip"],
            "service_port": str(pod_port(pod, resolved, map_doc) or ""), "mem_limit": str(ov.get("mem_limit") or ""),
            "cpus": str(ov.get("cpus") or ""),
        },
        "managed_env": managed_env(pod, resolved, map_doc),
    }
    return spec, node


# ------------------------------------------------------------------------------------------------ jobs

def _save_job(job_id: int, **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    db.ex(f"UPDATE jobs SET {sets} WHERE id=?", *[json.dumps(v) if k.endswith("_json") else v for k, v in cols.items()],
          job_id)


def _persist_step(job_id: int, step: dict) -> None:
    j = db.one("SELECT steps_json FROM jobs WHERE id=?", job_id)
    steps = [step if s["id"] == step["id"] else s for s in json.loads(j["steps_json"])]
    _save_job(job_id, steps_json=steps)


def _log(job_id: int, line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    db.ex("UPDATE jobs SET log_text = substr(log_text || ?, -200000) WHERE id=?", f"{stamp}Z {line}\n", job_id)


def lock(dep_id: int, job_id: int) -> None:
    with _locks_guard:
        if dep_id in _locks and _locks[dep_id] != job_id:
            raise HTTPException(409, f"deployment is busy with job #{_locks[dep_id]}")
        _locks[dep_id] = job_id


def unlock(dep_id: int, job_id: int) -> None:
    with _locks_guard:
        if _locks.get(dep_id) == job_id:
            _locks.pop(dep_id, None)


def busy(dep_id: int) -> Optional[int]:
    with _locks_guard:
        return _locks.get(dep_id)


def create_job(dep_id: Optional[int], kind: str, steps: list[dict], params: dict, user: str,
               dep_state: Optional[str] = None) -> int:
    """dep_state is set on the deployment only once the deployment lock is held (a 409 leaves it unchanged)."""
    versions = {s["version"] for s in steps if s.get("version")} | {_dep(dep_id).get("version") or ""}
    for v in sorted(versions - {""}):
        r = db.one("SELECT reupload_json FROM releases WHERE version=?", v)
        if r and r["reupload_json"]:
            raise HTTPException(409, f"release {v} is being re-uploaded; try again when it has finished")
    for i, s in enumerate(steps):
        s.setdefault("id", i)
        s.setdefault("status", "pending")
    jid = db.ex("INSERT INTO jobs(deployment_id,kind,status,params_json,steps_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)", dep_id, kind, "queued", json.dumps(params), json.dumps(steps), user, time.time())
    if dep_id is not None:
        try:
            lock(dep_id, jid)
        except HTTPException:
            db.ex("DELETE FROM jobs WHERE id=?", jid)
            raise
        if dep_state:
            db.ex("UPDATE deployments SET state=?, updated_at=? WHERE id=?", dep_state, time.time(), dep_id)
    threading.Thread(target=run_job, args=(jid,), daemon=True, name=f"job-{jid}").start()
    db.audit(user, f"job_{kind}", f"deployment:{dep_id}", {"job": jid, **{k: v for k, v in params.items() if k != 'map'}})
    return jid


def _dep(dep_id: Optional[int]) -> dict:
    d = db.one("SELECT * FROM deployments WHERE id=?", dep_id) if dep_id else None
    return d or {}


def _wait_for(pod: str) -> float:
    return settings.sm_heavy_wait_sec if pod in heavy_pods() else settings.sm_pod_wait_sec


def exec_step(job: dict, step: dict) -> str:
    k = step["kind"]
    dep = _dep(job["deployment_id"])
    params = json.loads(job["params_json"])
    if k == "check_node":
        n = agents.node(step["node_id"])
        info = agents.call(n, "GET", "/api/info", timeout=30)
        if not isinstance(info, dict):
            raise agents.AgentError(f"{n['name']}: unexpected /api/info reply")
        db.ex("UPDATE nodes SET info_json=?, last_seen=?, status='online' WHERE id=?", json.dumps(info), time.time(), n["id"])
        msgs = []
        if info.get("disk_free_gb", 0) < settings.sm_min_free_gb:
            raise agents.AgentError(f"{n['name']}: only {info.get('disk_free_gb', 0)} GB free "
                                    f"(minimum {settings.sm_min_free_gb} GB)")
        skew = abs(info.get("time", time.time()) - time.time())
        if skew > settings.sm_clock_skew_sec:
            msgs.append(f"clock skew {skew:.1f}s (check NTP)")
        if not info.get("live_restore"):
            msgs.append("docker live-restore is off (run prereq.sh)")
        return f"{n['name']}: docker {info.get('docker_version')}, {info.get('disk_free_gb')} GB free " + "; ".join(msgs)
    if k == "config_agent":
        n = agents.node(step["node_id"])
        body = {"logger_url": db.kv_get("logger_url_for_agents", "") or support.logger_url().replace(
            "127.0.0.1", settings.sys_ip).replace("localhost", settings.sys_ip),
            "logger_token": support.logger_token(), "manager_url": public_url(), "branch": settings.branch_name}
        agents.call(n, "POST", "/api/config", json=body)
        return "agent configured"
    if k == "prepare":
        spec, n = pod_spec(dep, step["version"], step["pod"], params.get("map"))
        res = agents.call(n, "POST", f"/api/pods/{step['pod']}/prepare", json=spec, timeout=1800)
        return f"prepared on {n['name']}: {res.get('compose')} (image: {res.get('image_from', '?')})"
    if k == "start":
        spec, n = pod_spec(dep, step["version"], step["pod"], params.get("map"))
        res = agents.call(n, "POST", f"/api/pods/{step['pod']}/start",
                          json={"managed_env": spec["managed_env"], "wait_sec": _wait_for(step["pod"])},
                          timeout=_wait_for(step["pod"]) + 120)
        if not isinstance(res, dict) or not res.get("ok"):
            raise agents.AgentError(f"{step['pod']} did not become healthy on {n['name']}: state={res.get('state')} "
                                    f"health={res.get('health')} {res.get('health_output', '')}")
        return f"{step['pod']} healthy on {n['name']}"
    n = agents.node(step["node_id"]) if step.get("node_id") else None
    if k == "stop":
        agents.call(n, "POST", f"/api/pods/{step['pod']}/stop", timeout=120)
        return f"{step['pod']} stopped on {n['name']}"
    if k == "restart":
        res = agents.call(n, "POST", f"/api/pods/{step['pod']}/restart", json={"wait_sec": _wait_for(step["pod"])},
                          timeout=_wait_for(step["pod"]) + 120)
        if not res.get("ok"):
            raise agents.AgentError(f"{step['pod']} unhealthy after restart: {res.get('health_output', '')}")
        return f"{step['pod']} restarted"
    if k == "down":
        agents.call(n, "POST", f"/api/pods/{step['pod']}/down",
                    json={"remove_images": step.get("remove_images", False), "remove_data": step.get("remove_data", False)},
                    timeout=300)
        return f"{step['pod']} removed from {n['name']}" + (" (data deleted)" if step.get("remove_data") else "")
    if k == "rollback":
        if step.get("if_release"):
            # compensation: only undo a pod the agent really has on the new release (prepare may have failed
            # before the agent switched it; rolling back then would go to an even older version)
            st = agents.call(n, "GET", f"/api/pods/{step['pod']}/status", timeout=30)
            if not isinstance(st, dict) or st.get("release") != step["if_release"]:
                return f"{step['pod']} not on {step['if_release']} on {n['name']}; nothing to roll back"
        res = agents.call(n, "POST", f"/api/pods/{step['pod']}/rollback", json={"wait_sec": _wait_for(step["pod"])},
                          timeout=_wait_for(step["pod"]) + 120)
        return f"{step['pod']} rolled back ({res.get('state')}/{res.get('health')})"
    if k == "backup":
        if not dep:
            raise JobError("deployment no longer exists")
        base = BACKUPS / dep["name"]
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        part = base / f".{stamp}.partial"
        for stale in base.glob(".*.partial") if base.is_dir() else []:
            shutil.rmtree(stale, ignore_errors=True)
        part.mkdir(parents=True, exist_ok=True)
        try:
            with open(part / f"{step['pod']}.dump", "wb") as fh:
                size = agents.stream(n, f"/api/pods/{step['pod']}/backup", fh)
            if size < 100:
                raise agents.AgentError(f"backup of {step['pod']} is empty ({size} bytes)")
            (part / "meta.json").write_text(json.dumps({"version": dep["version"], "pod": step["pod"], "size": size}))
            dest_dir = base / stamp
            shutil.rmtree(dest_dir, ignore_errors=True)
            part.rename(dest_dir)
        finally:
            shutil.rmtree(part, ignore_errors=True)
        f = dest_dir / f"{step['pod']}.dump"
        keep = max(1, settings.sm_backup_keep)
        olds = sorted(p for p in base.iterdir() if p.is_dir() and (p / "meta.json").is_file())
        for old in olds[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
        return f"pg_dump {size / 1024:.1f} KB -> {f}" if size < 1e6 else f"pg_dump {size / 1e6:.1f} MB -> {f}"
    if k == "restore":
        f = Path(step["file"])
        res = agents.call(n, "POST", f"/api/pods/{step['pod']}/restore-from", json={"url": _backup_url(f)}, timeout=3600)
        return f"restored {f.name}: {res.get('output', '')[-300:]}"
    if k == "prune":
        res = agents.call(n, "POST", "/api/prune", timeout=600)
        return f"pruned {len(res.get('removed', []))} old images"
    if k == "replace_image":
        helper, st = step.get("helper"), None
        if not helper:
            res = agents.call(n, "POST", f"/api/containers/{step['container']}/replace-image",
                              json={"image": step["image"], "health_url": step.get("health_url", ""),
                                    "registry_auth": registry.docker_auth(step["image"].split("/")[0])}, timeout=900)
            helper = step["helper"] = res.get("helper", "")
            _persist_step(job["id"], step)
        if not helper:
            raise agents.AgentError(f"{n['name']} did not start a self-update helper")
        end, last_err = time.time() + settings.sm_self_update_wait_sec, ""
        while time.time() < end:
            time.sleep(5)
            try:
                st = agents.call(n, "GET", f"/api/helpers/{helper}", timeout=15)
            except agents.AgentError as exc:
                if not exc.transient:
                    raise
                last_err = str(exc)
                continue  # sm-agent itself is being replaced
            if isinstance(st, dict) and st.get("status") == "exited":
                break
        if not isinstance(st, dict) or st.get("status") != "exited":
            raise agents.AgentError(f"self-update helper on {n['name']} did not finish"
                                    + (f" (last error: {last_err})" if last_err else ""))
        agents.call(n, "GET", f"/api/helpers/{helper}?remove=true", timeout=15)
        if st.get("exit_code") != 0:
            raise agents.AgentError(f"{step['container']} kept the old image on {n['name']} "
                                    f"(exit {st.get('exit_code')}): {st.get('logs', '')[-400:]}")
        return f"{step['container']} -> {step['image']} on {n['name']}"
    if k == "finalize":
        if not dep:
            return "deployment already removed"
        if step.get("delete"):
            db.ex("DELETE FROM deployments WHERE id=?", dep["id"])
            return "deployment removed"
        sets = step.get("set", {})
        if sets:
            cols = ", ".join(f"{c}=?" for c in sets)
            db.ex(f"UPDATE deployments SET {cols}, updated_at=? WHERE id=?", *sets.values(), time.time(), dep["id"])
        return f"deployment updated: {', '.join(f'{a}={b}' for a, b in sets.items() if a != 'map_json')}"
    raise RuntimeError(f"unknown step kind {k}")


def _backup_url(f: Path) -> str:
    rel = f.resolve().relative_to(BACKUPS.resolve()).as_posix()
    return f"{public_url()}/api/agent-backups/{rel}?t={file_token('backup', rel, 'backup')}"


def run_job(job_id: int, resume: bool = False) -> None:
    job = db.one("SELECT * FROM jobs WHERE id=?", job_id)
    if not job:
        return
    steps = json.loads(job["steps_json"])
    dep_id = job["deployment_id"]
    _save_job(job_id, status="running", started_at=job["started_at"] or time.time())
    _log(job_id, f"{'resumed' if resume else 'started'} {job['kind']}")
    failed: Optional[dict] = None
    try:
        for s in steps:
            if s["status"] in ("ok", "skipped"):
                continue
            s.update(status="running", started=time.time(), output="")
            _save_job(job_id, steps_json=steps)
            _log(job_id, f"step {s['id']} {s['kind']} {s.get('pod', '')} ...")
            try:
                out = exec_step(db.one("SELECT * FROM jobs WHERE id=?", job_id), s)
                s.update(status="ok", output=out, finished=time.time())
                _log(job_id, f"  ok: {out}")
            except Exception as exc:
                s.update(status="failed", output=str(exc), finished=time.time())
                _log(job_id, f"  FAILED: {exc}")
                log.warning("job %s step %s failed: %s", job_id, s["id"], exc)
                failed = s
            _save_job(job_id, steps_json=steps)
            if failed:
                break
        if failed and job["kind"] in ("update", "rollback", "apply_map", "reupload_apply"):
            _compensate(job_id, steps)
            _save_job(job_id, status="failed", finished_at=time.time(), steps_json=steps)
            if dep_id:
                db.ex("UPDATE deployments SET state='running', updated_at=? WHERE id=?", time.time(), dep_id)
        elif failed:
            _save_job(job_id, status="failed", finished_at=time.time())
            if dep_id and db.one("SELECT 1 AS x FROM deployments WHERE id=?", dep_id):
                db.ex("UPDATE deployments SET state='partial', updated_at=? WHERE id=?", time.time(), dep_id)
        else:
            _save_job(job_id, status="succeeded", finished_at=time.time())
        _log(job_id, f"finished: {'FAILED' if failed else 'succeeded'}")
    except Exception:
        failed = failed or {"internal": True}
        _log(job_id, "internal error: " + traceback.format_exc()[-2000:])
        _save_job(job_id, status="failed", finished_at=time.time())
        if dep_id:
            db.ex("UPDATE deployments SET state='partial', updated_at=? WHERE id=? AND state IN "
                  "('installing','updating','deleting','interrupted')", time.time(), dep_id)
    finally:
        if dep_id:
            unlock(dep_id, job_id)
        db.audit("system", f"job_{job['kind']}_{'failed' if failed else 'done'}", f"job:{job_id}")


def _compensate(job_id: int, steps: list[dict]) -> None:
    _log(job_id, "rolling back already-updated pods")
    done = [s for s in steps if s["status"] == "ok" and s.get("compensate")]
    failed_prep = [s for s in steps if s["status"] == "failed" and s["kind"] in ("prepare", "start") and s.get("compensate")]
    seen: set[str] = set()
    for s in list(reversed(done)) + failed_prep:
        if s["pod"] in seen:
            continue
        seen.add(s["pod"])
        comp = {"kind": s["compensate"], "pod": s["pod"], "node_id": s["node_id"], "id": f"c{s['id']}"}
        if comp["kind"] == "rollback" and s.get("version"):
            comp["if_release"] = s["version"]
        try:
            comp.update(status="ok", output=exec_step(db.one("SELECT * FROM jobs WHERE id=?", job_id), comp))
        except Exception as exc:
            comp.update(status="failed", output=str(exc))
        _log(job_id, f"  compensate {comp['kind']} {comp['pod']}: {comp['status']} {comp['output']}")
        steps.append(comp)


def mark_interrupted() -> None:
    for j in db.q("SELECT id, kind FROM jobs WHERE status IN ('running','queued')"):
        _save_job(j["id"], status="interrupted", finished_at=time.time())
        if j["kind"] == "self_update_software-manager":
            _log(j["id"], "manager restarted by its own self-update; checking the result")
            resume(j["id"], "system")
        else:
            _log(j["id"], "manager restarted while this job was running; resume or roll back")
    # deployments whose job died with the manager: no job will ever finish them, so let the user retry / resume
    db.ex("UPDATE deployments SET state='interrupted', updated_at=? WHERE state IN ('installing','updating','deleting')",
          time.time())


def resume(job_id: int, user: str) -> None:
    job = db.one("SELECT * FROM jobs WHERE id=?", job_id)
    if not job or job["status"] not in ("failed", "interrupted"):
        raise HTTPException(409, "only failed or interrupted jobs can be resumed")
    steps = json.loads(job["steps_json"])
    if any(str(s["id"]).startswith("c") for s in steps):
        raise HTTPException(409, "this job was rolled back; start a new update instead")
    if job["deployment_id"]:
        lock(job["deployment_id"], job_id)
    for s in steps:
        if s["status"] in ("failed", "running"):
            s["status"] = "pending"
    _save_job(job_id, steps_json=steps, status="queued")
    db.audit(user, "job_resume", f"job:{job_id}")
    threading.Thread(target=run_job, args=(job_id, True), daemon=True).start()


# ------------------------------------------------------------------------------------------- workflows

def _node_steps(map_doc: dict) -> list[dict]:
    nids = sorted({int(r["node_id"]) for r in map_doc.get("nodes", []) if r.get("pods")})
    return [{"kind": "check_node", "node_id": n} for n in nids] + [{"kind": "config_agent", "node_id": n} for n in nids]


def _ordered(version: str, map_doc: dict) -> list[tuple[str, int]]:
    _, m = _load(version)
    pl = placement(map_doc)
    return [(i.name, pl[i.name]) for i in pods_of(m) if i.name in pl]


def _installed(r: dict) -> str:
    """What a deployment is installed from: kept so a later re-upload of the same version can be compared."""
    return json.dumps({"manifest_text": r["manifest_text"], "resolved_json": r["resolved_json"] or "{}"})


def pending_changes(dep: dict) -> Optional[dict]:
    """Pods that differ between what the deployment was installed from and its (re-uploaded) release now."""
    snap = json.loads(dep.get("installed_json") or "{}")
    r = db.one("SELECT * FROM releases WHERE version=?", dep["version"])
    if not snap or not r or r["status"] != "downloaded" or r["reupload_json"]:
        return None
    old_m, _e, _w = manifest.parse(snap["manifest_text"])
    new_m, _e, _w = manifest.parse(r["manifest_text"])
    if not old_m or not new_m:
        return None
    d = manifest.diff(old_m, new_m, releases.pod_fingerprints(json.loads(snap["resolved_json"] or "{}")),
                      releases.pod_fingerprints(json.loads(r["resolved_json"] or "{}")))
    deployed = set(placement(json.loads(dep["map_json"])))
    pods = {i.name for i in pods_of(new_m)} | {i.name for i in pods_of(old_m)}
    out = {k: [p for p in d[k] if p in pods and (k == "added" or p in deployed)] for k in ("changed", "added", "removed")}
    if not any(out.values()):
        return None
    return {**out, "revision": r["revision"], "uploaded_at": r["uploaded_at"], "uploaded_by": r["uploaded_by"]}


def wf_install(dep: dict, user: str) -> int:
    mp = json.loads(dep["map_json"])
    r, _m = _load(dep["version"])
    order = _ordered(dep["version"], mp)
    steps = _node_steps(mp)
    steps += [{"kind": "prepare", "pod": p, "node_id": n, "version": dep["version"]} for p, n in order]
    steps += [{"kind": "start", "pod": p, "node_id": n, "version": dep["version"]} for p, n in order]
    steps.append({"kind": "finalize", "set": {"state": "running", "installed_json": _installed(r)}})
    return create_job(dep["id"], "install", steps, {}, user, dep_state="installing")


def wf_action(dep: dict, action: str, user: str, pods: Optional[list[str]] = None) -> int:
    mp = json.loads(dep["map_json"])
    order = [(p, n) for p, n in _ordered(dep["version"], mp) if not pods or p in pods]
    steps: list[dict] = []
    if action in ("stop", "restart"):
        steps += [{"kind": "stop", "pod": p, "node_id": n} for p, n in reversed(order)]
    if action in ("start", "restart"):
        steps += [{"kind": "start", "pod": p, "node_id": n, "version": dep["version"]} for p, n in order]
    if not pods:
        steps.append({"kind": "finalize", "set": {"state": "stopped" if action == "stop" else "running"}})
    return create_job(dep["id"], action if not pods else f"pod_{action}", steps, {"pods": pods or []}, user)


def wf_update(dep: dict, to_version: str, user: str, new_map: Optional[dict] = None, kind: str = "update") -> int:
    """to_version == current version applies a re-upload of that version (only the pods whose content changed).
    The image tag is the same then, so a failed pod cannot be rolled back automatically."""
    same = to_version == dep["version"]
    old_r, old_m = _load(dep["version"], "current release")
    old_resolved = old_r["resolved_json"] or "{}"
    snap = json.loads(dep.get("installed_json") or "{}")
    if snap:
        m, _e, _w = manifest.parse(snap["manifest_text"])
        if m:
            old_m, old_resolved = m, snap["resolved_json"] or "{}"
    new_r, new_m = _load(to_version)
    if new_r["status"] != "downloaded":
        raise HTTPException(422, f"release {to_version} is not downloaded")
    if new_m.software_name != old_m.software_name:
        raise HTTPException(422, "cannot update to a different software_name")
    mp = new_map or json.loads(dep["map_json"])
    if not new_map:
        keep = {i.name for i in pods_of(new_m)}
        mp = {**mp, "nodes": [{**row, "pods": [p for p in row.get("pods", []) if p in keep]}
                              for row in mp.get("nodes", [])]}
    v = validate(to_version, mp, dep["alarm_base_path"], exclude_dep=dep["id"])
    if not v["ok"]:
        raise HTTPException(422, "; ".join(v["errors"]))
    od = releases.pod_fingerprints(json.loads(old_resolved))
    nd = releases.pod_fingerprints(json.loads(new_r["resolved_json"] or "{}"))
    diff = manifest.diff(old_m, new_m, od, nd)
    if same and not (diff["changed"] or diff["added"] or diff["removed"]) and mp == json.loads(dep["map_json"]):
        raise HTTPException(422, f"nothing changed in release {to_version} since this deployment was installed")
    old_map = json.loads(dep["map_json"])
    old_pl, new_pl = placement(old_map), placement(mp)
    steps = _node_steps(mp)
    if DB_POD in old_pl and (diff["changed"] or diff["added"] or diff["removed"]):
        steps.append({"kind": "backup", "pod": DB_POD, "node_id": old_pl[DB_POD]})
    for p, n in _ordered(to_version, mp):
        if p in diff["changed"]:
            comp = None if same else "rollback"
            steps.append({"kind": "prepare", "pod": p, "node_id": n, "version": to_version, "compensate": comp})
            steps.append({"kind": "start", "pod": p, "node_id": n, "version": to_version, "compensate": comp})
        elif p in diff["added"]:
            steps.append({"kind": "prepare", "pod": p, "node_id": n, "version": to_version, "compensate": "down"})
            steps.append({"kind": "start", "pod": p, "node_id": n, "version": to_version, "compensate": "down"})
    for p in reversed([i.name for i in pods_of(old_m)]):
        if p in diff["removed"] and p in old_pl:
            steps.append({"kind": "down", "pod": p, "node_id": old_pl[p], "remove_images": True})
    for nid in sorted(set(new_pl.values())):
        steps.append({"kind": "prune", "node_id": nid})
    fin = {"version": to_version, "prev_version": dep["prev_version"] if same else dep["version"],
           "state": "running", "map_json": json.dumps(mp), "installed_json": _installed(new_r)}
    steps.append({"kind": "finalize", "set": fin})
    return create_job(dep["id"], "reupload_apply" if same else kind, steps,
                      {"from": dep["version"], "to": to_version, "diff": diff, "map": mp,
                       "revision": new_r["revision"]}, user, dep_state="updating")


def wf_apply_map(dep: dict, new_map: dict, user: str) -> int:
    v = validate(dep["version"], new_map, dep["alarm_base_path"], exclude_dep=dep["id"])
    if not v["ok"]:
        raise HTTPException(422, "; ".join(v["errors"]))
    r, m = _load(dep["version"])
    resolved = json.loads(r["resolved_json"] or "{}")
    old_map = json.loads(dep["map_json"])
    old_pl, new_pl = placement(old_map), placement(new_map)
    moved = [p for p in new_pl if old_pl.get(p) != new_pl[p]]
    steps = _node_steps(new_map)
    for p, n in _ordered(dep["version"], new_map):
        if p in moved:
            if p in old_pl:
                steps.append({"kind": "stop", "pod": p, "node_id": old_pl[p]})
            steps.append({"kind": "prepare", "pod": p, "node_id": n, "version": dep["version"]})
            steps.append({"kind": "start", "pod": p, "node_id": n, "version": dep["version"]})
    for p, n in _ordered(dep["version"], new_map):
        if p in moved:
            continue
        if managed_env(p, resolved, old_map) != managed_env(p, resolved, new_map):
            steps.append({"kind": "start", "pod": p, "node_id": n, "version": dep["version"]})
    for p in moved:
        if p in old_pl:
            steps.append({"kind": "down", "pod": p, "node_id": old_pl[p]})
    steps.append({"kind": "finalize", "set": {"map_json": json.dumps(new_map), "state": "running"}})
    return create_job(dep["id"], "apply_map", steps, {"moved": moved, "map": new_map}, user)


def wf_delete(dep: dict, user: str, remove_data: bool) -> int:
    mp = json.loads(dep["map_json"])
    steps = [{"kind": "down", "pod": p, "node_id": n, "remove_images": True, "remove_data": remove_data}
             for p, n in reversed(_ordered(dep["version"], mp))]
    steps.append({"kind": "finalize", "delete": True})
    return create_job(dep["id"], "delete", steps, {"remove_data": remove_data}, user, dep_state="deleting")


def wf_restore(dep: dict, backup_dir: str, user: str) -> int:
    mp = json.loads(dep["map_json"])
    order = _ordered(dep["version"], mp)
    pl = placement(mp)
    if DB_POD not in pl:
        raise HTTPException(409, f"deployment has no {DB_POD} pod")
    base = (BACKUPS / dep["name"]).resolve()
    f = (base / backup_dir / f"{DB_POD}.dump").resolve()
    if base not in f.parents or not f.is_file():
        raise HTTPException(404, "backup not found")
    keep = {DB_POD} | _names(settings.sm_restore_keep_running)
    others = [(p, n) for p, n in order if p not in keep]
    steps = [{"kind": "stop", "pod": p, "node_id": n} for p, n in reversed(others)]
    steps.append({"kind": "restore", "pod": DB_POD, "node_id": pl[DB_POD], "file": str(f)})
    steps += [{"kind": "start", "pod": p, "node_id": n, "version": dep["version"]} for p, n in others]
    return create_job(dep["id"], "restore", steps, {"backup": backup_dir}, user)


def wf_self_update(component: str, image: str, user: str, node_ids: list[int] | None = None) -> int:
    repo = image.rsplit(":", 1)[0].rsplit("/", 1)[-1]
    if repo != component:
        raise HTTPException(422, f"image {image} is not a {component} image (repository name must be {component})")
    nodes = [n for n in db.q("SELECT * FROM nodes ORDER BY id") if not node_ids or n["id"] in node_ids]
    steps: list[dict] = []
    if component == "sm-agent":
        if not nodes:
            raise HTTPException(422, "no nodes to update")
        for n in nodes:
            steps.append({"kind": "replace_image", "node_id": n["id"], "container": "sm-agent", "image": image,
                          "health_url": f"https://{n['ip']}:{n['port']}/health"})
    else:
        mgr = db.kv_get("manager_node_id")
        if not mgr:
            hit = next((n for n in nodes if n["ip"] == settings.sys_ip), None)
            mgr = hit["id"] if hit else None
        mnode = db.one("SELECT * FROM nodes WHERE id=?", int(mgr)) if mgr else None
        if not mnode:
            raise HTTPException(422, "set the Software Manager node in Settings first")
        if component == "software-manager":
            scheme, port = ("https" if settings.sm_https else "http"), settings.service_port
        else:
            u = urlparse(support.logger_url())
            scheme = u.scheme or "http"
            port = u.port or (443 if scheme == "https" else 80)
        steps.append({"kind": "replace_image", "node_id": mnode["id"], "container": component, "image": image,
                      "health_url": f"{scheme}://{mnode['ip']}:{port}/health"})
    return create_job(None, f"self_update_{component}", steps, {"image": image}, user)
