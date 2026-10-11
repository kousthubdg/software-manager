"""T6: install / start / stop / delete from a stored release (image tars served by the manager, no registry pull).

Run through tests/install-flow.sh (starts a manager + an agent from source). Everything comes from env:
  SM_URL, SM_PW                manager under test and its admin password
  NODE_IP, AGENT_PORT, AGENT_TOKEN   the test agent (added as a node)
  AGENT_LOG                    agent log file (checked for tar loads / tracebacks)
  REGISTRY, REG_USER, REG_PASSWORD_FILE, REG_CA   registry the release images are in
  RELEASE_FOLDER               release path folder to import (e.g. V1.0.0)
  SM_RELEASE_STORE             manager release store (optional: checks image.tar, restores image tags at the end)
  DATA_ROOT                    data root shared by manager + agent (pod folders live under it)
  T6_PODS                      candidate pods, comma separated (default thumbnail,scheduler); pods whose
                               container already exists on this Docker host are skipped
  T6_MAX_PODS (2), T6_PORT_BASE (13004)   how many pods, first service port to try
  T6_CREATED                   file to record the containers this test creates (launcher cleans up from it)
"""
from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import time

import httpx

E = os.environ
c = httpx.Client(base_url=E["SM_URL"], timeout=120, verify=False)
DEP_NAME = "t6-install-flow"
failures: list[str] = []


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def check(cond, what: str) -> bool:
    log(("PASS " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)
    return bool(cond)


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


def containers_all() -> set[str]:
    return set(sh("docker", "ps", "-a", "--format", "{{.Names}}").stdout.split())


def cstate(name: str) -> tuple[str, str, str]:
    """(status, health, started_at) of a container; ('missing', '', '') when absent."""
    p = sh("docker", "inspect", "-f", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|"
           "{{.State.StartedAt}}", name)
    if p.returncode != 0:
        return "missing", "", ""
    s, h, t = p.stdout.strip().split("|")
    return s, h, t


def wait_healthy(names: list[str], limit: float = 300) -> dict:
    end = time.time() + limit
    st = {}
    while time.time() < end:
        st = {n: cstate(n) for n in names}
        if all(s == "running" and h in ("", "healthy") for s, h, _ in st.values()):
            break
        time.sleep(3)
    return st


def image_id(ref: str) -> str:
    p = sh("docker", "image", "inspect", "-f", "{{.Id}}", ref)
    return p.stdout.strip() if p.returncode == 0 else ""


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def wait_release(v: str, limit: float = 3600) -> dict:
    end, last = time.time() + limit, ""
    while time.time() < end:
        r = c.get(f"/api/releases/{v}").json()
        prog = r.get("progress") or {}
        now = f"{r['status']} {prog.get('phase', '')} {prog.get('done', '')}/{prog.get('total', '')} {prog.get('current', '')}"
        if now != last:
            log("  release", v, now)
            last = now
        if r["status"] in ("downloaded", "download_failed", "scan_failed"):
            return r
        time.sleep(3)
    raise SystemExit(f"release {v} timeout")


def wait_job(jid: int, limit: float = 1800) -> dict:
    end = time.time() + limit
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("succeeded", "failed", "interrupted"):
            for s in j["steps"]:
                if s["status"] not in ("ok", "pending"):
                    log("   step", s["id"], s["kind"], s.get("pod", ""), s["status"], (s.get("output") or "")[:400])
            return j
        time.sleep(3)
    raise SystemExit(f"job {jid} timeout")


def job_ok(resp: httpx.Response, what: str) -> dict:
    if not check(resp.status_code == 200, f"{what}: request accepted ({resp.status_code} {resp.text[:200]})"):
        return {"status": "not-started", "steps": []}
    j = wait_job(resp.json()["job"])
    check(j["status"] == "succeeded", f"{what}: job #{j['id']} {j['status']}")
    return j


def dep_state(dep_id: int) -> dict:
    c.post(f"/api/nodes/{node_id}/refresh")
    return c.get(f"/api/deployments/{dep_id}").json()


def agent_pods() -> dict:
    r = httpx.get(f"https://127.0.0.1:{E['AGENT_PORT']}/api/pods", verify=False, timeout=30,
                  headers={"Authorization": f"Bearer {E['AGENT_TOKEN']}"})
    return {p["name"]: p for p in r.json()["pods"]}


def registry_put(password: str) -> None:
    ca = open(E["REG_CA"]).read() if E.get("REG_CA") else ""
    c.put("/api/registries", json={"host": E["REGISTRY"], "username": E["REG_USER"], "password": password,
                                   "ca_pem": ca}).raise_for_status()


# ---------------------------------------------------------------------------------------------- setup
tok = c.post("/api/auth/login", json={"username": "admin", "password": E["SM_PW"]}).raise_for_status().json()["token"]
c.headers["Authorization"] = f"Bearer {tok}"
reg_pw = open(E["REG_PASSWORD_FILE"]).read().strip()
registry_put(reg_pw)
log("registry configured:", E["REGISTRY"])

r = c.post("/api/nodes", json={"name": "t6-node", "ip": E["NODE_IP"], "port": int(E["AGENT_PORT"]),
                               "token": E["AGENT_TOKEN"]})
if not check(r.status_code == 200, f"add test agent as node ({r.text[:200]})"):
    sys.exit(1)
node_id = r.json()["id"]

r = c.post(f"/api/release-source/{E['RELEASE_FOLDER']}/import", json={"download": True})
if not check(r.status_code == 200, f"import {E['RELEASE_FOLDER']} with download ({r.text[:300]})"):
    sys.exit(1)
version = r.json()["version"]
rel = wait_release(version)
if not check(rel["status"] == "downloaded", f"release {version} downloaded ({(rel.get('progress') or {}).get('error', '')})"):
    sys.exit(1)

# ---------------------------------------------------------------------------------------------- choose pods
existing = containers_all()
cands = [p.strip() for p in E.get("T6_PODS", "thumbnail,scheduler").split(",") if p.strip()]
release_pods = {i["name"]: i for i in rel["manifest"]["installers"]}
pods = [p for p in cands if p in release_pods and p not in existing][: int(E.get("T6_MAX_PODS", "2"))]
log("skipped (container exists or not in release):", [p for p in cands if p not in pods])
if not check(bool(pods), f"found pods to install that are not on this host: {pods}"):
    sys.exit(1)
with open(E.get("T6_CREATED", os.devnull), "w") as fh:
    fh.write("\n".join(pods) + "\n")
ports: dict[str, int] = {}
port = int(E.get("T6_PORT_BASE", "13004"))
for p in pods:
    while not port_free(port) or port in ports.values():
        port += 1
    ports[p] = port
    port += 1
log("pods:", ports)

refs = {}
for p in pods:
    i = release_pods[p]
    refs[p] = f"{i['registry_server'] + '/' if i.get('registry_server') else ''}{i['repo']}:{i['image_ver']}"
    res = rel["resolved"].get(p, {})
    check(res.get("image_source") == "tar" and res.get("image_tar_md5"),
          f"{p}: release stores the image as a tar (image_source={res.get('image_source')})")
    if E.get("SM_RELEASE_STORE"):
        tar = os.path.join(E["SM_RELEASE_STORE"], version, p, "image.tar")
        check(os.path.isfile(tar) and os.path.getsize(tar) > 1e6, f"{p}: image.tar in the release store")

bad = httpx.get(f"{E['SM_URL']}/api/agent-files/{version}/{pods[0]}/image?t=bad", timeout=30)
check(bad.status_code == 403, f"agent-files refuses a bad token ({bad.status_code})")

# remove the local tags so the install has to load the image from the manager's tar
untagged: dict[str, str] = {}


@atexit.register
def restore_tags() -> None:
    """Put back every image tag this test removed (the delete step also removes them)."""
    for p, iid in untagged.items():
        ref = refs[p]
        if image_id(ref):
            continue
        if sh("docker", "tag", iid, ref).returncode != 0 and E.get("SM_RELEASE_STORE"):
            ld = sh("docker", "load", "-i", os.path.join(E["SM_RELEASE_STORE"], version, p, "image.tar"))
            loaded = [x.split(":", 1)[1].strip() for x in ld.stdout.splitlines() if x.startswith("Loaded image")]
            if loaded:
                sh("docker", "tag", loaded[0], ref)
        log(f"  restored {ref}: {'ok' if image_id(ref) else 'FAILED'}")



for p, ref in refs.items():
    iid = image_id(ref)
    if not iid:
        continue
    users = sh("docker", "ps", "-a", "--filter", f"ancestor={ref}", "--format", "{{.Names}}").stdout.split()
    if users:
        log(f"  {ref} is used by {users}; leaving it (load from tar cannot be proven for {p})")
        continue
    rm = sh("docker", "image", "rm", ref)
    if rm.returncode == 0:
        untagged[p] = iid
        log(f"  removed local tag {ref}")
    else:
        log(f"  could not remove {ref}: {rm.stderr.strip()[:200]}")

# ---------------------------------------------------------------------------------------------- validation
base_map = {"nodes": [{"node_id": node_id, "pods": pods}],
            "overrides": {p: {"service_port": ports[p]} for p in pods}}
abp = f"{E['DATA_ROOT'].rstrip('/')}/alarms"
v = c.post("/api/deployments/validate", json={"version": version, "map": base_map, "alarm_base_path": abp}).json()
check(not v["ok"] and any("not placed" in e for e in v["errors"]), "map without 'partial' is refused (unplaced pods)")
MAP = {**base_map, "partial": True}
v = c.post("/api/deployments/validate", json={"version": version, "map": MAP, "alarm_base_path": abp}).json()
check(v["ok"] and any("partial map" in w for w in v["warnings"]), f"partial map accepted with a warning {v['errors']}")

# ---------------------------------------------------------------------------------------------- install
registry_put("wrong-" + str(int(time.time())))
log("registry login set to a wrong password (install must not need the registry)")
r = c.post("/api/deployments", json={"name": DEP_NAME, "version": version, "map": MAP, "alarm_base_path": abp})
j = job_ok(r, "install")
dep = r.json().get("id") if r.status_code == 200 else None
registry_put(reg_pw)
if j["status"] != "succeeded" or not dep:
    for p in pods:
        lg = sh("docker", "logs", "--tail", "30", p)
        log(f"---- docker logs {p}\n{(lg.stdout + lg.stderr)[-3000:]}")
    print("\nRESULT:", f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
prep = {s["pod"]: s.get("output", "") for s in j["steps"] if s["kind"] == "prepare"}
for p in pods:
    if p in untagged:
        check("(image: tar)" in prep.get(p, ""), f"{p}: image loaded from the manager's tar ({prep.get(p, '')[-40:]})")
    check(bool(image_id(refs[p])), f"{p}: docker image inspect {refs[p]}")
    left = [f for f in os.listdir(os.path.join(E["DATA_ROOT"], p, "images"))] \
        if os.path.isdir(os.path.join(E["DATA_ROOT"], p, "images")) else []
    check(not left, f"{p}: downloaded image tar removed after load {left}")
st = wait_healthy(pods)
check(all(s == "running" and h in ("", "healthy") for s, h, _ in st.values()), f"containers running + healthy {st}")
d = dep_state(dep)
check(d["state"] == "running", f"deployment state running ({d['state']})")
mpods = {x["name"]: x for x in d["pods"] if x["name"] in pods}
check(all((mpods[p]["status"] or {}).get("state") == "running" for p in pods), "manager sees the pods running")
check(all(mpods[p]["port"] == ports[p] for p in pods), "manager shows the overridden ports")
for p in pods:
    vals = c.get(f"/api/nodes/{node_id}/pods/{p}/values").json().get("values", {})
    check(vals.get("SERVICE_PORT") == str(ports[p]), f"{p}: values.env SERVICE_PORT={vals.get('SERVICE_PORT')}")
    hp = sh("curl", "-sf", "-m", "5", f"http://127.0.0.1:{ports[p]}/health")
    check(hp.returncode == 0, f"{p}: /health on port {ports[p]} answers")

# ---------------------------------------------------------------------------------------------- restart one pod
p0 = pods[0]
before = cstate(p0)[2]
job_ok(c.post(f"/api/deployments/{dep}/pods/{p0}/action", json={"action": "restart"}), f"restart {p0}")
s, h, after = wait_healthy([p0])[p0]
check(s == "running" and h in ("", "healthy") and after != before, f"{p0} restarted ({before} -> {after}, {s}/{h})")

# ---------------------------------------------------------------------------------------------- stop / start
job_ok(c.post(f"/api/deployments/{dep}/action", json={"action": "stop"}), "stop deployment")
st = {p: cstate(p) for p in pods}
check(all(s == "exited" for s, _, _ in st.values()), f"stop -> containers exited {st}")
d = dep_state(dep)
check(d["state"] == "stopped", "deployment state stopped")
mst = {x["name"]: (x["status"] or {}) for x in d["pods"] if x["name"] in pods}
check(all(s.get("state") == "exited" and s.get("health") is None and s.get("desired") == "stopped"
          for s in mst.values()),
      f"manager sees the pods exited, desired stopped, no stale health "
      f"{ {p: (s.get('state'), s.get('health'), s.get('desired')) for p, s in mst.items()} }")
time.sleep(int(E.get("T6_HEAL_WAIT", "15")))
st = {p: cstate(p) for p in pods}
check(all(s == "exited" for s, _, _ in st.values()), f"agent self-heal leaves stopped pods stopped {st}")

job_ok(c.post(f"/api/deployments/{dep}/action", json={"action": "start"}), "start deployment")
st = wait_healthy(pods)
check(all(s == "running" and h in ("", "healthy") for s, h, _ in st.values()), f"start -> running + healthy {st}")
check(dep_state(dep)["state"] == "running", "deployment state running after start")

# ---------------------------------------------------------------------------------------------- reinstall stopped
job_ok(c.post(f"/api/deployments/{dep}/action", json={"action": "stop"}), "stop before reinstall")
j = job_ok(c.post(f"/api/deployments/{dep}/install"), "reinstall stopped deployment")
prep = {s["pod"]: s.get("output", "") for s in j["steps"] if s["kind"] == "prepare"}
check(all("(image: local)" in prep.get(p, "") for p in pods), "reinstall reuses the loaded image (no new load)")
st = wait_healthy(pods)
check(all(s == "running" and h in ("", "healthy") for s, h, _ in st.values()), f"reinstall -> running + healthy {st}")
check(dep_state(dep)["state"] == "running", "deployment state running after reinstall")

# ---------------------------------------------------------------------------------------------- agent log
if E.get("AGENT_LOG") and os.path.isfile(E["AGENT_LOG"]):
    text = open(E["AGENT_LOG"], errors="replace").read()
    for p in untagged:
        check(f"{p}: loaded {refs[p]} from image tar" in text, f"agent log: {p} loaded from image tar")
    check("Traceback" not in text, "agent log has no tracebacks")
    check("/prepare HTTP/1.1\" 200" in text, "agent log shows successful prepare calls")

# ---------------------------------------------------------------------------------------------- delete
j = job_ok(c.post(f"/api/deployments/{dep}/delete", json={"confirm_name": DEP_NAME, "remove_data": True,
                                                         "confirm_data": "DELETE DATA"}), "delete deployment (+data)")
left = containers_all() & set(pods)
check(not left, f"delete -> containers gone {sorted(left)}")
check(not any(os.path.exists(os.path.join(E["DATA_ROOT"], p)) for p in pods), "delete -> pod data removed")
check(c.get(f"/api/deployments/{dep}").status_code == 404, "deployment record removed")
check(not (set(agent_pods()) & set(pods)), "agent no longer tracks the pods")

print("\nRESULT:", "ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
