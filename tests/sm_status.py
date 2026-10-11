"""Print deployments, pods and recent jobs from a manager (lab helper). Usage: sm_status.py URL PASSWORD"""
import sys

import httpx

c = httpx.Client(base_url=sys.argv[1], timeout=60, verify=False)
c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": sys.argv[2]}).json()["token"]
for d in c.get("/api/deployments").json():
    print(f"deployment {d['name']} {d['version']} state={d['state']} prev={d['prev_version']}")
    for p in d["pods"]:
        s = p["status"]
        print(f"  {p['name']:20} {p['node']:8} :{p['port']} {s.get('state')}/{s.get('health')} v{s.get('version')}")
for j in c.get("/api/jobs?limit=12").json():
    print(f"job #{j['id']:3} {j['kind']:14} {j['status']:10} {j['progress']['done']}/{j['progress']['total']}")
print("alerts:", [a["message"] for a in c.get("/api/alerts").json()])
