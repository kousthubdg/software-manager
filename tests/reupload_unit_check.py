"""In-process checks of re-uploading a stored version (same version, new content). No agents, no network.

  cd software-manager/src && PYTHONPATH=. SM_STATE_DIR=<tmp>/state SM_RELEASE_STORE=<tmp>/rel SM_BACKUP_DIR=<tmp>/bk \
    SM_BUNDLE_DIR=<tmp>/b SM_IMPORT_DIR=<tmp>/i AC_DIR=<tmp>/ac SM_HTTPS=false SM_DATA_ROOT=<tmp>/data \
    $PY ../tests/reupload_unit_check.py
Use a throwaway <tmp> folder and delete it afterwards."""
from __future__ import annotations

import io
import json
import tarfile
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException

from app import central, db, deploy, releases
from app.security import encrypt

fails: list[str] = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    if not cond:
        fails.append(what)


V = "7.7.7"


def manifest_text(pods: list[str]) -> str:
    out = [f"software_name: REUPLOAD_TEST\nsoftware_version: {V}\ninstallers:"]
    for p in pods:
        out.append(f"  - name: {p}\n    image: {p}\n    image_ver: {V}\n    image_source: tar\n"
                   f"    image_tar_path: /nonexistent/{p}.tar\n    scripts_ver: {V}\n"
                   f"    scripts_path: /nonexistent/{p}_scripts.tar")
    return "\n".join(out) + "\n"


def resolved(ids: dict[str, str]) -> str:
    return json.dumps({p: {"image_source": "tar", "image_id": i, "scripts_md5": "s" * 32, "port": 9000 + n}
                       for n, (p, i) in enumerate(ids.items())})


# --- image id from a docker-archive tar
with tempfile.TemporaryDirectory() as td:
    t = Path(td) / "img.tar"
    with tarfile.open(t, "w") as tf:
        body = json.dumps([{"Config": "blobs/sha256/" + "ab" * 32, "RepoTags": ["x:1"]}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    check(releases.tar_image_id(t) == "sha256:" + "ab" * 32, f"tar_image_id ({releases.tar_image_id(t)[:20]}…)")

# --- a stored release and a deployment installed from it
old_text = manifest_text(["a", "b"])
now = time.time() - 100
db.ex("INSERT INTO releases(version,software_name,description,manifest_text,manifest_sha256,uploaded_by,uploaded_at,"
      "status,scan_json,progress_json,updated_at,resolved_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      V, "REUPLOAD_TEST", "", old_text, releases.manifest.parse(old_text)[0].sha256, "tester", now, "downloaded",
      "{}", "{}", now, resolved({"a": "sha256:1", "b": "sha256:2"}))
db.ex("INSERT INTO nodes(name,ip,port,token_enc,added_at,status) VALUES(?,?,?,?,?,?)",
            "n1", "10.255.255.1", 2997, encrypt("x"), now, "online")
nid = db.one("SELECT id FROM nodes WHERE name='n1'")["id"]
db.ex("INSERT INTO deployments(name,software_name,version,alarm_base_path,map_json,state,created_by,created_at,updated_at)"
      " VALUES(?,?,?,?,?,?,?,?,?)", "d1", "REUPLOAD_TEST", V, deploy.data_root() + "/d1",
      json.dumps({"nodes": [{"node_id": nid, "pods": ["a", "b"]}]}), "running", "tester", now, now)
dep = lambda: db.one("SELECT * FROM deployments WHERE name='d1'")  # noqa: E731
rel = lambda: db.one("SELECT * FROM releases WHERE version=?", V)  # noqa: E731

# --- same version without replace -> asks
res = releases.upload(manifest_text(["a", "b", "c"]), "tester", True)
check(not res["ok"] and res.get("exists") and res["uploaded_by"] == "tester", f"upload of stored version asks first: {res.get('errors')}")
check(rel()["manifest_text"] == old_text, "nothing changed without replace")

# --- replace whose check fails -> stored copy back, error kept
res = releases.upload(manifest_text(["a", "b", "c"]), "tester2", True, replace=True)
check(res["ok"] and res["reupload"], "re-upload started")
check(bool(json.loads(dep()["installed_json"] or "{}").get("manifest_text")), "deployment snapshot taken before the re-upload")
try:
    deploy.create_job(dep()["id"], "restart", [], {}, "tester")
    check(False, "jobs blocked while re-uploading")
except HTTPException as exc:
    check(exc.status_code == 409, f"jobs blocked while re-uploading ({exc.detail})")
end = time.time() + 30
while time.time() < end and rel()["reupload_json"]:
    time.sleep(0.2)
r = rel()
prog = json.loads(r["progress_json"] or "{}")
check(r["status"] == "downloaded" and r["manifest_text"] == old_text and r["uploaded_by"] == "tester",
      f"failed re-upload restores the stored copy (status {r['status']})")
check(bool(prog.get("reupload_error")) and r["revision"] == 1, f"failure reason kept: {prog.get('reupload_error', '')[:80]}")

# --- simulate a successful re-upload: image of a re-pushed, service c added
new_text = manifest_text(["a", "b", "c"])
db.ex("UPDATE releases SET manifest_text=?, manifest_sha256=?, resolved_json=?, uploaded_at=?, revision=2, progress_json='{}' "
      "WHERE version=?", new_text, releases.manifest.parse(new_text)[0].sha256,
      resolved({"a": "sha256:1b", "b": "sha256:2", "c": "sha256:3"}), time.time(), V)
pc = deploy.pending_changes(dep())
check(pc and pc["changed"] == ["a"] and pc["added"] == ["c"] and not pc["removed"] and pc["revision"] == 2,
      f"pending changes: {pc}")

captured: dict = {}
real_create = deploy.create_job
deploy.create_job = lambda dep_id, kind, steps, params, user, dep_state=None: captured.update(
    kind=kind, steps=steps, params=params) or 1
try:
    deploy.wf_update(dep(), V, "tester")
    check(False, "added pod without a node is rejected")
except HTTPException as exc:
    check(exc.status_code == 422 and "'c'" in str(exc.detail), f"added pod without a node is rejected ({exc.detail})")
deploy.wf_update(dep(), V, "tester", {"nodes": [{"node_id": nid, "pods": ["a", "b", "c"]}]})
st = captured["steps"]
touched = {(s["kind"], s.get("pod")): s.get("compensate", "-") for s in st}
check(captured["kind"] == "reupload_apply", f"job kind {captured['kind']}")
check(("prepare", "a") in touched and ("prepare", "c") in touched and ("prepare", "b") not in touched,
      f"only a and c are touched: {sorted(k for k in touched if k[0] in ('prepare', 'start'))}")
check(touched[("start", "a")] is None and touched[("start", "c")] == "down", "a: no image rollback, c: removed on failure")
fin = st[-1]["set"]
check(fin["version"] == V and fin["prev_version"] == "" and json.loads(fin["installed_json"])["manifest_text"] == new_text,
      "finalize keeps prev_version and records what is installed")
deploy.create_job = real_create

db.ex("UPDATE deployments SET installed_json=?, map_json=? WHERE name='d1'", fin["installed_json"], fin["map_json"])
check(deploy.pending_changes(dep()) is None, "no pending changes once applied")
try:
    deploy.wf_update(dep(), V, "tester")
    check(False, "nothing to apply -> 422")
except HTTPException as exc:
    check(exc.status_code == 422, f"nothing to apply -> 422 ({exc.detail})")

# --- removed service is dropped from the map automatically
removed_text = manifest_text(["a", "c"])
db.ex("UPDATE releases SET manifest_text=?, manifest_sha256=?, resolved_json=? WHERE version=?", removed_text,
      releases.manifest.parse(removed_text)[0].sha256, resolved({"a": "sha256:1b", "c": "sha256:3"}), V)
deploy.create_job = lambda dep_id, kind, steps, params, user, dep_state=None: captured.update(steps=steps) or 1
deploy.wf_update(dep(), V, "tester")
deploy.create_job = real_create
check(any(s["kind"] == "down" and s.get("pod") == "b" for s in captured["steps"])
      and "b" not in json.loads(captured["steps"][-1]["set"]["map_json"])["nodes"][0]["pods"], "removed b goes down and leaves the map")

# --- branch: the office re-uploaded a version that is stored here
loc = central._local(V)
item = {"version": V, "software_name": "REUPLOAD_TEST", "manifest_sha256": loc["manifest_sha256"],
        "content_sha256": releases.content_sha256(loc["manifest_sha256"], loc["resolved_json"]), "uploaded_at": loc["uploaded_at"]}
check(central._state_for(item, loc) == "downloaded", "same content -> downloaded")
newer = {**item, "content_sha256": "x" * 64, "uploaded_at": loc["uploaded_at"] + 60}
check(central._state_for(newer, loc) == "update", "newer upload at the office -> update")
check(central._state_for({**newer, "uploaded_at": loc["uploaded_at"] - 60}, loc) == "conflict", "older different copy -> conflict")
check(central._state_for({**newer, "software_name": "OTHER"}, loc) == "conflict", "other software -> conflict")
check(central._state_for(item, None) == "available", "not here -> available")

# --- restart during a re-upload
snap = {k: rel()[k] for k in releases.REUPLOAD_KEEP} | {"progress_json": "{}"}
db.ex("UPDATE releases SET reupload_json=?, status='scanning', manifest_text='broken' WHERE version=?", json.dumps(snap), V)
releases.recover_reuploads()
r = rel()
check(r["status"] == "downloaded" and r["manifest_text"] == removed_text and not r["reupload_json"], "restart restores the stored copy")

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
raise SystemExit(1 if fails else 0)
