"""Re-link a test branch to a test office after central_test.py.  central_relink.py <office url> <branch url>"""
import sys

import httpx

c = {}
for name, url in (("o", sys.argv[1]), ("b", sys.argv[2])):
    c[name] = httpx.Client(base_url=url, timeout=60)
    c[name].headers["Authorization"] = "Bearer " + c[name].post(
        "/api/auth/login", json={"username": "admin", "password": "admin-test-pw"}).json()["token"]
key = c["o"].post("/api/catalog/key").json()["key"]
c["b"].put("/api/central", json={"url": sys.argv[1], "key": key, "ca_pem": None}).raise_for_status()
c["b"].post("/api/central/check").raise_for_status()
print(c["b"].get("/api/central").json()["available"])
