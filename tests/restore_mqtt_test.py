"""Restore + MQTT alert test against the live manager (WSL lab).

python restore_mqtt_test.py URL ADMIN_PW DEPLOYMENT_NAME
  restore: marker row 'before' -> backup job -> marker 'after' -> restore job -> marker must be 'before' again
  mqtt:    subscribe on the deployment's mqtt pod, raise + clear a test alert inside the manager container
"""
import json
import subprocess
import sys
import time

import httpx

url, pw, dep_name = sys.argv[1:4]
c = httpx.Client(base_url=url, verify=False, timeout=60)
h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": pw}).json()["token"]}
fails = []


def sh(cmd: str) -> str:
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout.strip()


def psql(sql: str) -> str:
    return subprocess.run(["docker", "exec", "-e", f"Q={sql}", "postgres-database", "sh", "-c",
                           'psql -h /var/run/postgresql -p $SERVICE_PORT -U $POSTGRES_USER -d $POSTGRES_DB -tAc "$Q"'],
                          capture_output=True, text=True).stdout.strip()


def wait_job(jid: int) -> dict:
    for _ in range(240):
        j = c.get(f"/api/jobs/{jid}", headers=h).json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(3)
    return j


dep = next(d for d in c.get("/api/deployments", headers=h).json() if d["name"] == dep_name)
did = dep["id"]

# ---------------------------------------------------------------- restore
psql("CREATE TABLE IF NOT EXISTS public.sm_restore_test(v text); DELETE FROM public.sm_restore_test; "
     "INSERT INTO public.sm_restore_test VALUES ('before')")
j = wait_job(c.post(f"/api/deployments/{did}/backup", headers=h).json()["job"])
print("backup job:", j["status"], j["steps"][0]["output"])
backup = c.get(f"/api/deployments/{did}/backups", headers=h).json()[0]["id"]
psql("UPDATE public.sm_restore_test SET v='after'")
print("marker before restore:", psql("SELECT v FROM public.sm_restore_test"))
r = c.post(f"/api/deployments/{did}/restore", headers=h, json={"backup": backup, "confirm_name": dep_name})
j = wait_job(r.json()["job"])
print("restore job:", j["status"])
for s in j["steps"]:
    print(f"  {s['id']} {s['kind']} {s.get('pod') or s.get('node_name', '')} {s['status']}: {str(s.get('output', ''))[:160]}")
marker = psql("SELECT v FROM public.sm_restore_test")
print("marker after restore:", marker)
if j["status"] != "succeeded" or marker != "before":
    fails.append("restore")
psql("DROP TABLE IF EXISTS public.sm_restore_test")
pods = c.get(f"/api/deployments/{did}", headers=h).json().get("pods", [])
print("pods:", json.dumps(pods)[:600])

# ---------------------------------------------------------------- mqtt alert
sub = subprocess.Popen(["bash", "-c", "docker exec mqtt sh -c 'mosquitto_sub -h 127.0.0.1 -p ${MQTT_PORT:-11883} "
                        "-t \"yellowmetal/#\" -C 2 -W 40 -v'"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(3)
sh("docker exec software-manager python -c \"from app import health; "
   "health.raise_alert('test:mqtt', 'mqtt alert path test'); health.clear_alert('test:mqtt')\"")
out, _ = sub.communicate(timeout=60)
print("mqtt received:\n" + out)
msgs = [ln for ln in out.splitlines() if "/software-manager/alerts" in ln]
if len(msgs) != 2 or '"raised"' not in msgs[0] or '"cleared"' not in msgs[1]:
    fails.append("mqtt")
print("RESULT:", "ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
