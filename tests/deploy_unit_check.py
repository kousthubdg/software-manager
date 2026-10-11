"""In-process checks of deploy.py / agents.py paths that T6 does not reach (agent calls mocked).

  cd software-manager/src && PYTHONPATH=. SM_STATE_DIR=<tmp>/state SM_RELEASE_STORE=<tmp>/rel SM_BACKUP_DIR=<tmp>/bk \
    SM_BUNDLE_DIR=<tmp>/b SM_IMPORT_DIR=<tmp>/i AC_DIR=<tmp>/ac SM_HTTPS=false SM_DATA_ROOT=<tmp>/data \
    $PY ../tests/deploy_unit_check.py
Use a throwaway <tmp> folder and delete it afterwards."""
from __future__ import annotations

import io
import json
import time

from fastapi import HTTPException

from app import agents, db, deploy, releases
from app.config import BACKUPS, settings
from app.security import encrypt

fails: list[str] = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    if not cond:
        fails.append(what)


# --- data_root / run_ids fall back to env when the setting is cleared
db.kv_set("data_root", None)
check(deploy.data_root() == settings.sm_data_root.rstrip("/"), f"data_root falls back ({deploy.data_root()})")
db.kv_set("data_root", "")
check(deploy.data_root() == settings.sm_data_root.rstrip("/"), "data_root '' falls back")
db.kv_set("run_uid", None)
check(deploy.run_ids()[0] == settings.sm_run_uid, "run_uid falls back")

# --- map shape errors instead of 500
for bad in ({"nodes": "x"}, {"nodes": [{"node_id": "abc", "pods": ["a"]}]}, {"nodes": [{"node_id": 1, "pods": "a"}]},
            {"nodes": [], "overrides": {"a": {"service_port": "http"}}}, []):
    v = deploy.validate("9.9.9", bad, "/x")
    check(not v["ok"] and v["errors"], f"validate rejects {bad!r}: {v['errors']}")

# --- node + deployment fixtures
nid = db.ex("INSERT INTO nodes(name,ip,port,token_enc,added_at,status) VALUES(?,?,?,?,?,?)",
            "n1", "10.255.255.1", 1, encrypt("tok"), time.time(), "online")
did = db.ex("INSERT INTO deployments(name,software_name,version,alarm_base_path,map_json,state,created_by,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?)", "dep1", "SW", "9.9.9", "/opt/x/alarms",
            json.dumps({"nodes": [{"node_id": nid, "pods": ["thumbnail"]}]}), "running", "t", time.time(), time.time())
dep = db.one("SELECT * FROM deployments WHERE id=?", did)

# --- unknown release -> HTTP 404, not KeyError
for fn, args in ((deploy.wf_update, (dep, "8.8.8", "t")), (deploy.wf_install, (dep, "t")),
                 (deploy.wf_restore, (dep, "x", "t"))):
    try:
        fn(*args)
        check(False, f"{fn.__name__}: unknown release raises")
    except HTTPException as exc:
        check(exc.status_code in (404, 409), f"{fn.__name__}: unknown release -> HTTP {exc.status_code} {exc.detail}")
    except Exception as exc:
        check(False, f"{fn.__name__}: unknown release -> {type(exc).__name__} {exc}")

# --- pod_spec inside a job: JobError, not HTTPException / KeyError
try:
    deploy.pod_spec(dep, "8.8.8", "thumbnail")
    check(False, "pod_spec unknown release raises")
except deploy.JobError as exc:
    check(True, f"pod_spec unknown release -> JobError ({exc})")
except Exception as exc:
    check(False, f"pod_spec unknown release -> {type(exc).__name__}")

# --- lock first: a 409 leaves the deployment state alone
deploy.lock(did, 999999)
try:
    deploy.create_job(did, "install", [{"kind": "finalize"}], {}, "t", dep_state="installing")
    check(False, "busy deployment refused")
except HTTPException as exc:
    st = db.one("SELECT state FROM deployments WHERE id=?", did)["state"]
    check(exc.status_code == 409 and st == "running", f"409 keeps state ({exc.status_code}, {st})")
deploy.unlock(did, 999999)

# --- startup resets deployments stuck in installing / updating / deleting
db.ex("UPDATE deployments SET state='updating' WHERE id=?", did)
deploy.mark_interrupted()
st = db.one("SELECT state FROM deployments WHERE id=?", did)["state"]
check(st == "interrupted", f"mark_interrupted resets updating -> {st}")
db.ex("UPDATE deployments SET state='running' WHERE id=?", did)

# --- backup: failed / empty dump leaves no dated folder; good dumps rotate by SM_BACKUP_KEEP
calls: list = []
real_call, real_stream = agents.call, agents.stream


def fake_stream(n, path, dest, timeout=3600):
    payload = fake_stream.payload
    if isinstance(payload, Exception):
        dest.write(b"partial")
        raise payload
    dest.write(payload)
    return len(payload)


agents.stream = fake_stream
job = {"id": 0, "deployment_id": did, "params_json": "{}"}
step = {"kind": "backup", "pod": "postgres-database", "node_id": nid}
for payload in (b"x" * 10, agents.AgentError("boom")):
    fake_stream.payload = payload
    try:
        deploy.exec_step(job, step)
        check(False, "bad backup fails")
    except Exception:
        pass
    left = list((BACKUPS / "dep1").iterdir()) if (BACKUPS / "dep1").exists() else []
    check(not left, f"failed backup ({payload!r}) leaves nothing: {left}")
settings.sm_backup_keep = 2
fake_stream.payload = b"y" * 500
for _ in range(3):
    deploy.exec_step(job, step)
    time.sleep(1.1)
kept = sorted(p.name for p in (BACKUPS / "dep1").iterdir())
check(len(kept) == 2 and all((BACKUPS / "dep1" / k / "meta.json").is_file() for k in kept), f"keep=2 -> {kept}")

# --- compensation: rollback only pods the agent really has on the new release
log: list = []


def fake_call(n, method, path, json=None, timeout=60, token=None, raw=False):
    log.append((method, path))
    if path.endswith("/status"):
        return {"release": "2.0.0" if "/a/" in path else "1.0.0"}
    if path.endswith("/rollback"):
        return {"state": "running", "health": "healthy"}
    return {}


agents.call = fake_call
jid = db.ex("INSERT INTO jobs(deployment_id,kind,status,params_json,steps_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?)", did, "update", "running", "{}", "[]", "t", time.time())
steps = [
    {"id": 1, "kind": "prepare", "pod": "a", "node_id": nid, "version": "2.0.0", "compensate": "rollback", "status": "ok"},
    {"id": 2, "kind": "start", "pod": "a", "node_id": nid, "version": "2.0.0", "compensate": "rollback", "status": "ok"},
    {"id": 3, "kind": "prepare", "pod": "b", "node_id": nid, "version": "2.0.0", "compensate": "rollback",
     "status": "failed"},
]
deploy._compensate(jid, steps)
rb = [p for m, p in log if p.endswith("/rollback")]
check(rb == ["/api/pods/a/rollback"], f"only pod a rolled back (b never switched): {rb}")

# --- self-update helper polling fails fast on a permanent agent error
def perm_call(n, method, path, json=None, timeout=60, token=None, raw=False):
    if "/helpers/" in path:
        raise agents.AgentError("helper not found [HTTP 404]", 404)
    return {"helper": "sm-updater-x-1"}


agents.call = perm_call
t0 = time.time()
try:
    deploy.exec_step({"id": jid, "deployment_id": None, "params_json": "{}"},
                     {"id": 9, "kind": "replace_image", "node_id": nid, "container": "sm-agent", "image": "r/sm-agent:2",
                      "helper": "sm-updater-x-1"})
    check(False, "permanent helper error fails")
except agents.AgentError as exc:
    check(time.time() - t0 < 30, f"permanent helper error fails fast ({time.time() - t0:.0f}s: {exc})")
agents.call, agents.stream = real_call, real_stream

# --- agents: no plain-http fallback unless enabled
check(agents._schemes() == ("https",), "agent calls are https only by default")
settings.sm_agent_http_fallback = True
check(agents._schemes() == ("https", "http"), "SM_AGENT_HTTP_FALLBACK=true enables http")
settings.sm_agent_http_fallback = False
settings.sm_agent_ca_file = "/some/ca.pem"
check(agents._verify() == "/some/ca.pem", "SM_AGENT_CA_FILE used for verification")
settings.sm_agent_ca_file = ""

# --- self-update health URLs come from node / settings, not fixed ports or 127.0.0.1
real_create = deploy.create_job
captured: dict = {}
deploy.create_job = lambda dep_id, kind, steps, params, user, dep_state=None: captured.setdefault("s", steps) and 1
deploy.wf_self_update("sm-agent", "reg/sm-agent:2", "t")
check(captured["s"][0]["health_url"] == "https://10.255.255.1:1/health", f"agent health url {captured['s'][0]['health_url']}")
deploy.create_job = real_create

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
raise SystemExit(1 if fails else 0)
