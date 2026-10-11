"""Count recent logger entries by node/service/source: python logship_check.py SINCE_ISO"""
import collections
import re
import sys
from pathlib import Path

import httpx

env = Path("/root/AlarmCenter/software-manager/.env").read_text()
tok = re.search(r"^LOGGER_TOKEN=(.*)$", env, re.M).group(1).strip()
r = httpx.get("http://127.0.0.1:2998/api/logs", params={"limit": 5000, "since": sys.argv[1]},
              headers={"Authorization": f"Bearer {tok}"}, timeout=30)
d = r.json()
print("entries:", d["count"])
pref = [e for e in d["entries"] if re.match(r"^[0-9a-f]{12}_", e["svc"])]
print("prefixed:", len(pref), "newest:", max((e["ts"] for e in pref), default="-"))
for k, v in sorted(collections.Counter((e["node"], e["svc"], e["src"]) for e in d["entries"]).items()):
    print(f"  {k[0]:<10} {k[1]:<28} {k[2]:<7} {v}")
