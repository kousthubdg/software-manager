"""End-to-end against a real manager + two agents (WSL lab).

install 1.0.0 across two nodes -> wiring check -> stop/start -> self-heal -> update 1.0.1 (+backup)
-> rollback -> failing update 1.0.2 (auto rollback) -> delete (+data)

env: SM_URL, SM_PW, NODE_IP, TOKEN_A, TOKEN_B, PORT_A, PORT_B, REG_PW, REG_CA, SCRIPTS (dir with *_scripts.tar)
"""
import json
import os
import subprocess
import sys
import time

import httpx

E = os.environ
c = httpx.Client(base_url=E["SM_URL"], timeout=120, verify=False)
PODS_A = ["postgres-database", "mqtt"]
PODS_B = ["configuration", "alarm"]
PORTS = {"postgres-database": 55432, "mqtt": 11883, "configuration": 18001, "alarm": 18005}
failures = []


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def check(cond, what):
    log(("PASS " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def manifest(version, image_vers):
    lines = ["manifest_schema: 1", "software_name: YELLOW_METAL_E2E", f"software_version: {version}",
             "description: e2e subset", "installers:"]
    for name in PODS_A + PODS_B:
        lines += [f"  - name: {name}", f"    image: localhost:5000/{name}", f"    image_ver: {image_vers.get(name, '1.0.0')}",
                  "    datafile_path: none", "    scripts_ver: 1.0.0",
                  f"    scripts_path: {E['SCRIPTS']}/{name}_scripts.tar"]
    return "\n".join(lines) + "\n"


def wait_release(v, done=("downloaded", "download_failed", "scan_failed", "scanned"), limit=600):
    end = time.time() + limit
    while time.time() < end:
        r = c.get(f"/api/releases/{v}").json()
        if r["status"] in done:
            return r
        time.sleep(2)
    raise SystemExit(f"release {v} timeout")


def release(v, vers):
    r = c.post("/api/releases/upload", json={"text": manifest(v, vers)})
    assert r.status_code == 200, r.text
    rel = wait_release(v, ("scanned", "scan_failed"))
    if rel["status"] != "scanned":
        for i in rel["scan"]["installers"]:
            log(i["name"], {k: x.get("error") for k, x in i["checks"].items() if not x["ok"]})
        raise SystemExit(f"scan {v} failed")
    c.post(f"/api/releases/{v}/download").raise_for_status()
    rel = wait_release(v, ("downloaded", "download_failed"))
    check(rel["status"] == "downloaded", f"release {v} scanned + downloaded")


def wait_job(jid, limit=1200):
    end = time.time() + limit
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("succeeded", "failed", "interrupted"):
            if j["status"] != "succeeded":
                for s in j["steps"]:
                    if s["status"] not in ("ok", "pending"):
                        log("   step", s["id"], s["kind"], s.get("pod", ""), s["status"], (s.get("output") or "")[:300])
            return j
        time.sleep(3)
    raise SystemExit(f"job {jid} timeout")


def pods(dep_id):
    for n in c.get("/api/nodes").json():
        c.post(f"/api/nodes/{n['id']}/refresh")
    d = c.get(f"/api/deployments/{dep_id}").json()
    return {p["name"]: p["status"] for p in d["pods"]}, d


def all_running(dep_id):
    st, _ = pods(dep_id)
    return all(s.get("state") == "running" and s.get("health") in (None, "healthy") for s in st.values()), st


# ---------------------------------------------------------------------------------------------- setup
tok = c.post("/api/auth/login", json={"username": "admin", "password": E["SM_PW"]}).raise_for_status().json()["token"]
c.headers["Authorization"] = f"Bearer {tok}"
c.put("/api/registries", json={"host": "localhost:5000", "username": "ym", "password": E["REG_PW"],
                               "ca_pem": open(E["REG_CA"]).read()}).raise_for_status()
nodes = {}
for name, port, token in (("node-a", E["PORT_A"], E["TOKEN_A"]), ("node-b", E["PORT_B"], E["TOKEN_B"])):
    existing = [n for n in c.get("/api/nodes").json() if n["name"] == name]
    if existing:
        nodes[name] = existing[0]["id"]
        continue
    r = c.post("/api/nodes", json={"name": name, "ip": E["NODE_IP"], "port": int(port), "token": token})
    check(r.status_code == 200, f"add {name} ({r.text[:200] if r.status_code != 200 else 'ok'})")
    nodes[name] = r.json()["id"]

for v, vers in (("1.0.0", {}), ("1.0.1", {"configuration": "1.0.1"}), ("1.0.2", {"configuration": "1.0.1", "alarm": "1.0.2"})):
    if c.get(f"/api/releases/{v}").status_code == 404:
        release(v, vers)

MAP = {"nodes": [{"node_id": nodes["node-a"], "pods": PODS_A}, {"node_id": nodes["node-b"], "pods": PODS_B}],
       "overrides": {p: {"service_port": port} for p, port in PORTS.items()}}
ABP = "/opt/yellowmetal/alarms"

# ---------------------------------------------------------------------------------------------- install
bad = c.post("/api/deployments/validate", json={"version": "1.0.0", "map": {"nodes": [{"node_id": nodes["node-a"], "pods": PODS_A}]},
                                                 "alarm_base_path": ABP}).json()
check(not bad["ok"] and any("not placed" in e for e in bad["errors"]), "validation rejects unplaced pods")
bad = c.post("/api/deployments/validate", json={"version": "1.0.0", "map": MAP, "alarm_base_path": "/tmp/x"}).json()
check(not bad["ok"], "validation rejects alarm path outside data root")

r = c.post("/api/deployments", json={"name": "e2e", "version": "1.0.0", "map": MAP, "alarm_base_path": ABP})
check(r.status_code == 200, f"create deployment {r.text[:300] if r.status_code != 200 else ''}")
dep = r.json()["id"]
j = wait_job(r.json()["job"])
check(j["status"] == "succeeded", f"install 1.0.0 job #{j['id']}")
if j["status"] != "succeeded":
    raise SystemExit("install failed; stopping (fix, then delete the deployment or resume the job)")
ok, st = all_running(dep)
check(ok, f"all pods running/healthy after install {json.dumps({k: (v.get('state'), v.get('health')) for k, v in st.items()})}")

wiring = c.get(f"/api/deployments/{dep}/wiring").json()
log("wiring alarm:", wiring.get("alarm"))
check(wiring["alarm"].get("DB_PORT") == "55432" and wiring["alarm"].get("MQTT_PORT") == "11883",
      "alarm wired to postgres:55432 + mqtt:11883")
vals = c.get(f"/api/nodes/{nodes['node-b']}/pods/alarm/values").json()["values"]
check(vals.get("DB_PORT") == "55432" and vals.get("DB_IP") == E["NODE_IP"] and vals.get("SM_MANAGED", "1") is not None,
      "alarm values.env has managed DB_IP/DB_PORT")
check(vals.get("DB_PASSWORD") is None and vals.get("DB_PASSWORD_FILE") == "/run/secrets/DB_PASSWORD",
      "secret moved to /run/secrets (DB_PASSWORD_FILE)")

# ---------------------------------------------------------------------------------------------- stop/start
j = wait_job(c.post(f"/api/deployments/{dep}/action", json={"action": "stop"}).json()["job"])
st, d = pods(dep)
check(j["status"] == "succeeded" and all(s.get("state") == "exited" for s in st.values()) and d["state"] == "stopped",
      "stop deployment")
j = wait_job(c.post(f"/api/deployments/{dep}/action", json={"action": "start"}).json()["job"])
ok, st = all_running(dep)
check(j["status"] == "succeeded" and ok, "start deployment")

# ---------------------------------------------------------------------------------------------- self-heal
subprocess.run(["docker", "stop", "configuration"], capture_output=True)
log("stopped configuration by hand; waiting for agent reconcile")
healed = False
for _ in range(30):
    time.sleep(5)
    ok, st = all_running(dep)
    if st["configuration"].get("state") == "running":
        healed = True
        break
check(healed, "agent restarted a hand-stopped container (self-healing)")

# ---------------------------------------------------------------------------------------------- update + rollback
j = wait_job(c.post(f"/api/deployments/{dep}/update", json={"version": "1.0.1"}).json()["job"])
st, d = pods(dep)
check(j["status"] == "succeeded" and d["version"] == "1.0.1" and st["configuration"].get("version") == "1.0.1",
      f"update 1.0.0 -> 1.0.1 (changed: {j['params'].get('diff', {}).get('changed')})")
check(any(s["kind"] == "backup" and s["status"] == "ok" for s in j["steps"]), "pg_dump backup before update")
check(len(c.get(f"/api/deployments/{dep}/backups").json()) >= 1, "backup listed")
j = wait_job(c.post(f"/api/deployments/{dep}/rollback").json()["job"])
st, d = pods(dep)
check(j["status"] == "succeeded" and d["version"] == "1.0.0" and st["configuration"].get("version") == "1.0.0",
      "rollback 1.0.1 -> 1.0.0")
j = wait_job(c.post(f"/api/deployments/{dep}/update", json={"version": "1.0.1"}).json()["job"])
check(j["status"] == "succeeded", "update again to 1.0.1")

# ---------------------------------------------------------------------------------------------- failing update
j = wait_job(c.post(f"/api/deployments/{dep}/update", json={"version": "1.0.2"}).json()["job"])
st, d = pods(dep)
comp = [s for s in j["steps"] if str(s["id"]).startswith("c")]
check(j["status"] == "failed" and comp and d["version"] == "1.0.1", f"bad update 1.0.2 fails and stays on 1.0.1 (compensations {[(s['kind'], s['pod'], s['status']) for s in comp]})")
ok = False
for _ in range(24):
    ok, st = all_running(dep)
    if ok:
        break
    time.sleep(5)
check(ok and st["alarm"].get("version") == "1.0.0", f"alarm rolled back automatically and healthy ({st['alarm'].get('version')}, {st['alarm'].get('health')})")

# ---------------------------------------------------------------------------------------------- logs / bundle
time.sleep(8)
lg = c.get("/api/logs/services")
check(lg.status_code == 200 and any(lg.json().get("nodes", {}).values()), f"logger has service logs {lg.text[:200]}")
sb = c.get("/api/support-bundle?hours=1")
check(sb.status_code == 200 and sb.content[:2] == b"PK", f"support bundle zip ({len(sb.content)} bytes)")
import io, zipfile  # noqa: E402
names = zipfile.ZipFile(io.BytesIO(sb.content)).namelist()
vals_txt = [zipfile.ZipFile(io.BytesIO(sb.content)).read(n).decode() for n in names if n.endswith("values.env")]
check(vals_txt and all("admin@agvdb" not in t for t in vals_txt), "support bundle values.env redacted")

# ---------------------------------------------------------------------------------------------- delete
if "--keep" not in sys.argv:
    j = wait_job(c.post(f"/api/deployments/{dep}/delete", json={"confirm_name": "e2e", "remove_data": True,
                                                                "confirm_data": "DELETE DATA"}).json()["job"])
    left = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    check(j["status"] == "succeeded" and not any(p in left for p in PODS_A + PODS_B), "delete removes all pods")
    check(not any(os.path.exists(f"/opt/yellowmetal/{p}") for p in PODS_A + PODS_B), "delete removed pod data")

print("\nRESULT:", "ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
