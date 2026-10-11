"""Release path -> Upload (check + store scripts and images) -> prerequisites package, against a running manager.

  python offline_flow_test.py <url> <admin_pw> <folder> <prereq rel path> <registry_host> <reg_user> <reg_pw_file> <ca_file> <out_file>
Writes the built package name to <out_file> and {"version": ...} to version.json next to it.
"""
import json
import os
import sys
import time

import httpx

url, pw, folder, prereq, reg, ruser, rpw_file, ca_file, out_file = sys.argv[1:10]
c = httpx.Client(base_url=url, timeout=60)
c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": pw}).json()["token"]
c.put("/api/registries", json={"host": reg, "username": ruser, "password": open(rpw_file).read().strip(),
                               "ca_pem": open(ca_file).read()}).raise_for_status()
src = c.get("/api/release-source").json()
print("release path:", src["root"], [x["folder"] for x in src["releases"]], "prereq:", [p["path"] for p in src["prerequisites"]])
r = c.post(f"/api/release-source/{folder}/import", json={"download": True})
print("upload", r.status_code, r.json())
ver = r.json()["version"]
t0 = time.time()
while True:
    rel = c.get(f"/api/releases/{ver}").json()
    if rel["status"] in ("downloaded", "download_failed", "scan_failed"):
        break
    time.sleep(3)
print(f"stored: {rel['status']} in {time.time() - t0:.0f}s", (rel.get("progress") or {}).get("error") or "")
if rel["status"] != "downloaded":
    sys.exit(1)
r = c.post("/api/bundles", json={"prereq": prereq, "versions": [ver]})
print("package", r.status_code, r.json())
name = r.json()["name"]
while True:
    b = next(x for x in c.get("/api/bundles").json() if x["name"] == name)
    if not b["building"]:
        break
    time.sleep(3)
print(f"package ready: {name} {b['size'] / 1e9:.2f} GB releases={b['releases']}")
open(out_file, "w").write(name)
json.dump({"version": ver}, open(os.path.join(os.path.dirname(out_file), "version.json"), "w"))
