"""Self-update test against the live manager.

python selfupdate_test.py URL ADMIN_PW COMPONENT IMAGE [NODE_ID ...]
Starts a self-update job, waits for it (the manager may restart under us) and prints the result.
Exit 0 only if the job succeeded.
"""
import sys
import time

import httpx

url, pw, comp, image = sys.argv[1:5]
node_ids = [int(x) for x in sys.argv[5:]]
c = httpx.Client(base_url=url, verify=False, timeout=30)


def login() -> dict:
    for _ in range(60):
        try:
            r = c.post("/api/auth/login", json={"username": "admin", "password": pw})
            if r.status_code == 200:
                return {"Authorization": f"Bearer {r.json()['token']}"}
        except httpx.HTTPError:
            pass
        time.sleep(3)
    sys.exit("manager not reachable")


h = login()
r = c.post("/api/self-update", headers=h, json={"component": comp, "image": image, "node_ids": node_ids})
print("start:", r.status_code, r.text)
r.raise_for_status()
jid = r.json()["job"]
end = time.time() + 900
job = {}
while time.time() < end:
    time.sleep(5)
    try:
        r = c.get(f"/api/jobs/{jid}", headers=h)
        if r.status_code == 401:
            h = login()
            continue
        job = r.json()
    except httpx.HTTPError:
        continue  # manager restarting
    if job.get("status") not in ("pending", "running"):
        break
print("job", jid, job.get("status"))
for s in job.get("steps", []):
    print(f"  {s['id']} {s['kind']} {s.get('node_name', '')} {s['status']}: {s.get('output', '')}")
print("health:", httpx.get(f"{url}/health", verify=False, timeout=10).text)
sys.exit(0 if job.get("status") == "succeeded" else 1)
