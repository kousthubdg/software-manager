"""Local release-flow test against a running manager (WSL): login, registry, upload, scan, download.

Usage: python local_release_test.py <manager_url> <admin_password> <manifest> <registry_host> <reg_user> <reg_pw_file> <ca_file>
"""
import sys
import time

import httpx

url, admin_pw, mpath, reg, reg_user, reg_pw_file, ca_file = sys.argv[1:8]
c = httpx.Client(base_url=url, timeout=60, verify=False)
tok = c.post("/api/auth/login", json={"username": "admin", "password": admin_pw}).raise_for_status().json()["token"]
c.headers["Authorization"] = f"Bearer {tok}"
print("login ok")
c.put("/api/registries", json={"host": reg, "username": reg_user, "password": open(reg_pw_file).read().strip(),
                               "ca_pem": open(ca_file).read()}).raise_for_status()
print("registry saved")
r = c.post("/api/releases/upload", files={"file": ("m.yml", open(mpath, "rb").read())})
print("upload", r.status_code, r.json())
ver = r.json().get("version") or "1.0.0"


def wait(states_done: set, limit: float = 900) -> dict:
    end = time.time() + limit
    while time.time() < end:
        rel = c.get(f"/api/releases/{ver}").json()
        if rel["status"] in states_done:
            return rel
        time.sleep(2)
    raise SystemExit(f"timeout; status {rel['status']}")


rel = wait({"scanned", "scan_failed"})
print("scan:", rel["status"])
for inst in rel["scan"]["installers"]:
    bad = {k: v.get("error") for k, v in inst["checks"].items() if not v["ok"]}
    print(f"  {inst['name']:20} {'OK' if inst['ok'] else 'FAIL'} {bad or ''} "
          f"digest={inst['checks'].get('image', {}).get('digest', '')[:19]} port={inst['checks']['scripts'].get('default_port')}")
for w in rel["scan"].get("warnings", []):
    print("  warn:", w)
if rel["status"] != "scanned":
    raise SystemExit(1)
c.post(f"/api/releases/{ver}/download").raise_for_status()
rel = wait({"downloaded", "download_failed"})
print("download:", rel["status"], rel["progress"].get("error", ""))
print("resolved sample:", {k: rel["resolved"][k] for k in list(rel["resolved"])[:2]})
sys.exit(0 if rel["status"] == "downloaded" else 1)
