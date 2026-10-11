"""Print releases, nodes and deployments from a Software Manager SQLite DB (read only).
   sm_live_state.py <path to sm.db>"""
import sqlite3
import sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for table, cols in (("releases", "version,status"), ("nodes", "name,ip,port"), ("deployments", "*")):
    try:
        rows = con.execute(f"SELECT {cols} FROM {table}").fetchall()
    except sqlite3.Error as e:
        print(table, "error", e)
        continue
    print(f"== {table} ({len(rows)})")
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, str) and len(v) > 300:
                d[k] = v[:300] + "..."
        print(d)
