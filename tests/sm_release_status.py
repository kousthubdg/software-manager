"""Print status + progress of one release: python sm_release_status.py <url> <admin_pw> <version>"""
import sys

import httpx

url, pw, ver = sys.argv[1:4]
c = httpx.Client(base_url=url, timeout=30, verify=False)
c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": pw}).json()["token"]
r = c.get(f"/api/releases/{ver}").json()
p = r.get("progress") or {}
print(r["status"], p.get("phase"), f"{p.get('done')}/{p.get('total')}", p.get("current") or "", p.get("error") or "")
