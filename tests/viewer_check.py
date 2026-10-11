"""Call the read endpoints as a given user and print status + size.   env: SM_URL, SM_USER, SM_PW"""
import os

import httpx

c = httpx.Client(base_url=os.environ["SM_URL"], timeout=30, verify=False)
r = c.post("/api/auth/login", json={"username": os.environ["SM_USER"], "password": os.environ["SM_PW"]})
print("login", r.status_code, r.json().get("role"))
c.headers["Authorization"] = "Bearer " + r.json()["token"]
for p in ("/api/me", "/api/releases", "/api/release-source", "/api/bundles", "/api/nodes", "/api/deployments",
          "/api/registries", "/api/settings"):
    r = c.get(p)
    print(p, r.status_code, r.text[:200])
