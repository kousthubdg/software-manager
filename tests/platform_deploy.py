"""Deploy a whole release from the release path on one node through the Software Manager API.

env:
  SM_URL           manager URL (https://<host>:<port>)
  SM_PW_FILE       file with the admin password (SM_USER, default admin)
  RELEASE_FOLDER   release path folder to upload (e.g. V1.0.0)
  NODE_NAME        node (as in Nodes) that gets every pod
  DEP_NAME         deployment name to create (default: branch)
  OVERRIDES        JSON {pod: {service_port: N}} (optional; or OVERRIDES_FILE with that JSON)
  ALARM_BASE_PATH  default <data_root>/alarms
  DROP_DEPLOYMENTS comma list of deployments to delete first (with data)
  DROP_RELEASES    comma list of release versions to delete first
  DROP_NODES       comma list of node names to delete first
  ACTION           deploy (default) | status
"""
import json
import os
import sys
import time

import httpx

E = os.environ
c = httpx.Client(base_url=E["SM_URL"], timeout=120, verify=False)
pw = open(E["SM_PW_FILE"]).read().strip()
c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": E.get("SM_USER", "admin"), "password": pw}
                                                ).raise_for_status().json()["token"]


def lst(k):
    return [x.strip() for x in E.get(k, "").split(",") if x.strip()]


def wait_job(jid, limit=3600):
    t0 = time.time()
    last = None
    while time.time() - t0 < limit:
        j = c.get(f"/api/jobs/{jid}").json()
        step = next((s for s in reversed(j.get("steps", [])) if s.get("status") != "pending"), None)
        name = step and f"{step.get('kind')} {step.get('pod', '')}"
        cur = (j["status"], name, step and step.get("status"))
        if cur != last:
            print(f"  job #{jid} {j['status']} {j['progress']['done']}/{j['progress']['total']} {name or ''} "
                  f"{step and step.get('status')}", flush=True)
            last = cur
        if j["status"] in ("succeeded", "failed", "cancelled"):
            if j["status"] != "succeeded":
                for s in j.get("steps", []):
                    if s.get("status") == "failed":
                        print("  FAILED STEP:", s.get("kind"), s.get("pod"), str(s.get("output") or "")[:1500])
            return j
        time.sleep(5)
    raise SystemExit(f"job {jid} timed out")


def status(dep_name):
    for n in c.get("/api/nodes").json():
        c.post(f"/api/nodes/{n['id']}/refresh")
    d = next((x for x in c.get("/api/deployments").json() if x["name"] == dep_name), None)
    if not d:
        print("no deployment", dep_name)
        return
    d = c.get(f"/api/deployments/{d['id']}").json()
    print(f"deployment {d['name']} {d.get('version')} state={d.get('state')}")
    pods = d.get("pods") or []
    rows = sorted(pods.items()) if isinstance(pods, dict) else sorted((p.get("pod") or p.get("name"), p) for p in pods)
    for pod, st in rows:
        print(f"  {pod!s:20} {st.get('state')!s:10} {st.get('health')!s:10} {st.get('error') or ''}")


dep_name = E.get("DEP_NAME", "branch")
if E.get("ACTION") == "status":
    status(dep_name)
    sys.exit(0)

deps = {d["name"]: d for d in c.get("/api/deployments").json()}
for name in lst("DROP_DEPLOYMENTS"):
    if name in deps:
        print("delete deployment", name)
        r = c.post(f"/api/deployments/{deps[name]['id']}/delete",
                   json={"confirm_name": name, "remove_data": True, "confirm_data": "DELETE DATA"})
        print(" ", r.status_code, r.text[:300])
        if r.status_code == 200:
            wait_job(r.json()["job"])
for v in lst("DROP_RELEASES"):
    r = c.delete(f"/api/releases/{v}")
    print("delete release", v, r.status_code, r.text[:200])
nodes = {n["name"]: n for n in c.get("/api/nodes").json()}
for n in lst("DROP_NODES"):
    if n in nodes:
        r = c.delete(f"/api/nodes/{nodes[n]['id']}")
        print("delete node", n, r.status_code, r.text[:200])

folder = E["RELEASE_FOLDER"]
src = c.get("/api/release-source").json()
item = next((x for x in src["releases"] if x["folder"] == folder), None)
if not item:
    raise SystemExit(f"{folder} not in release path {src['root']}: {[x['folder'] for x in src['releases']]}")
ver = item.get("version")
if item.get("status") != "downloaded":
    r = c.post(f"/api/release-source/{folder}/import", json={"download": True})
    print("upload", folder, r.status_code, r.text[:300])
    r.raise_for_status()
    ver = r.json()["version"]
    t0 = time.time()
    while (st := c.get(f"/api/releases/{ver}").json())["status"] not in ("downloaded", "scan_failed", "download_failed"):
        if time.time() - t0 > 1800:
            raise SystemExit("release store timed out")
        time.sleep(5)
    print("release", ver, st["status"], (st.get("progress") or {}).get("error") or "")
    if st["status"] != "downloaded":
        raise SystemExit(1)

rel = c.get(f"/api/releases/{ver}").json()
pods = [i["name"] for i in rel["manifest"]["installers"]]
node = next((n for n in c.get("/api/nodes").json() if n["name"] == E["NODE_NAME"]), None)
if not node:
    raise SystemExit(f"node {E['NODE_NAME']} not registered")
settings = c.get("/api/settings").json()
abp = E.get("ALARM_BASE_PATH") or f"{settings['data_root'].rstrip('/')}/alarms"
ovr = open(E["OVERRIDES_FILE"]).read() if E.get("OVERRIDES_FILE") else E.get("OVERRIDES") or "{}"
dmap = {"nodes": [{"node_id": node["id"], "pods": pods}], "overrides": json.loads(ovr)}
v = c.post("/api/deployments/validate", json={"version": ver, "map": dmap, "alarm_base_path": abp}).json()
print("validate", json.dumps(v)[:1500])
if not v.get("ok"):
    raise SystemExit(1)
r = c.post("/api/deployments", json={"name": dep_name, "version": ver, "map": dmap, "alarm_base_path": abp})
print("create", r.status_code, r.text[:300])
r.raise_for_status()
j = wait_job(r.json()["job"])
print("install job", j["status"])
status(dep_name)
sys.exit(0 if j["status"] == "succeeded" else 1)
