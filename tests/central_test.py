"""Office -> branch release catalog checks.  central_test.py <office url> <branch url> <office release store>"""
import sys
import time
from pathlib import Path

import httpx

OFFICE, BRANCH, STORE = sys.argv[1], sys.argv[2], Path(sys.argv[3])
PW = "admin-test-pw"
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what)
    fails += 0 if ok else 1


def login(url):
    c = httpx.Client(base_url=url, timeout=60)
    c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": PW}).json()["token"]
    return c


def wait_state(b, ver, want, timeout=1800):
    end = time.time() + timeout
    while time.time() < end:
        st = b.get("/api/central").json()
        p = st.get("progress", {}).get(ver, {})
        if p.get("state") in want:
            return p
        time.sleep(3)
    return {"state": "timeout"}


o, b = login(OFFICE), login(BRANCH)
ver = o.get("/api/releases").json()[0]["version"]
print("office release", ver)

raw = httpx.Client(base_url=OFFICE, timeout=30)
check(raw.get("/api/catalog").status_code == 401, "catalog off: no key -> 401")
key = o.post("/api/catalog/key").json()["key"]
check(o.get("/api/central").json()["catalog_enabled"], "office publishing on")
check(raw.get("/api/catalog").status_code == 401, "no key -> 401")
check(raw.get("/api/catalog", headers={"X-Catalog-Key": "wrong"}).status_code == 401, "wrong key -> 401")
kh = {"X-Catalog-Key": key}
cat = raw.get("/api/catalog", headers=kh).json()
check(any(x["version"] == ver for x in cat), "catalog lists the release")
for bad in ("../../state/sm.db", "/etc/passwd", "..\\x", ""):
    r = raw.get(f"/api/catalog/{ver}/file", params={"path": bad}, headers=kh)
    check(r.status_code in (400, 404), f"file path {bad!r} refused ({r.status_code})")
check(raw.get("/api/catalog/..%2F..%2Fx/files", headers=kh).status_code in (400, 404), "bad version refused")

check(b.put("/api/central", json={"url": "ftp://x", "key": None, "ca_pem": None}).status_code in (400, 422), "non-http url refused")
b.put("/api/central", json={"url": OFFICE, "key": "wrong", "ca_pem": None}).raise_for_status()
b.post("/api/central/check").raise_for_status()
time.sleep(2)
check("refused" in (b.get("/api/central").json().get("error") or ""), "branch reports a wrong key")

b.put("/api/central", json={"url": OFFICE, "key": key, "ca_pem": None}).raise_for_status()
b.post("/api/central/check").raise_for_status()
st = b.get("/api/central").json()
check(st["key_set"] and not st["error"], "branch connects with the right key")
p = wait_state(b, ver, ("downloaded", "failed"))
check(p.get("state") == "downloaded", f"auto download -> {p.get('state')} {p.get('error', '')}")
rel = {r["version"]: r for r in b.get("/api/releases").json()}
check(rel.get(ver, {}).get("status") == "downloaded", "release shows as downloaded on the branch (in dropdowns)")
ores = o.get(f"/api/releases/{ver}").json()
check(rel.get(ver, {}).get("manifest_sha256") == ores.get("manifest_sha256"), "same manifest as the office")
b.post("/api/central/check").raise_for_status()
time.sleep(2)
av = {a["version"]: a["state"] for a in b.get("/api/central").json()["available"]}
check(av.get(ver) == "downloaded", "catalog row now says downloaded")

# tampered file on the office: same size, other content -> md5 mismatch, nothing imported
tar = next(STORE.glob(f"{ver}/*/scripts.tar"))
orig = tar.read_bytes()
tampered = bytearray(orig)
tampered[-1] ^= 0xFF
tar.write_bytes(bytes(tampered))
try:
    b.delete(f"/api/releases/{ver}").raise_for_status()
    b.post(f"/api/central/{ver}/download").raise_for_status()
    p = wait_state(b, ver, ("downloaded", "failed"))
    check(p.get("state") == "failed" and "md5" in p.get("error", ""), f"tampered file rejected: {p.get('error', '')[:120]}")
    check(ver not in {r["version"] for r in b.get("/api/releases").json()}, "tampered release not registered")
finally:
    tar.write_bytes(orig)
b.post(f"/api/central/{ver}/download").raise_for_status()
p = wait_state(b, ver, ("downloaded", "failed"))
check(p.get("state") == "downloaded", "download again after the fix")

check(o.delete("/api/catalog/key").status_code < 300, "office publishing off")
check(raw.get("/api/catalog", headers=kh).status_code == 401, "old key no longer works")
print("FAILURES", fails)
sys.exit(1 if fails else 0)
