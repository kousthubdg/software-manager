"""T4 API edge cases against a running Software Manager (see test.md 4.1).

env (nothing is fixed to one host):
  SM_URL              manager base URL, e.g. http://127.0.0.1:<port>
  SM_ADMIN_PW         admin password of that manager
  SM_RELEASE_SOURCE   the manager's release path (read only; only read here)
  REGISTRY, REG_USER, REG_PASSWORD_FILE, REG_CA   registry the release images are in (password never printed)
  WORK                scratch folder for generated YML / tars (default $HOME/sm-t4-inputs)
  SM_STATE_DIR, SM_RELEASE_STORE, SM_IMPORT_DIR   the manager's folders (cases that need them are skipped if unset)
  T4_SERVICE          installer of the release-path YML used for the small stored release (default mqtt)
  T4_SCRATCH_SOURCE=1 SM_RELEASE_SOURCE is a scratch folder: the test adds broken release folders to it
  T4_SKIP_DESTRUCTIVE=1  skip the traversal-delete case (for running against code that may not guard it)
Exit 0 when every case passes.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import ssl
import sys
import tarfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

URL = os.environ["SM_URL"].rstrip("/")
ADMIN_PW = os.environ["SM_ADMIN_PW"]
SRC = Path(os.environ["SM_RELEASE_SOURCE"])
REGISTRY = os.environ["REGISTRY"]
REG_USER = os.environ["REG_USER"]
REG_PASSWORD_FILE = os.environ["REG_PASSWORD_FILE"]
REG_CA = os.environ["REG_CA"]
WORK = Path(os.environ.get("WORK") or Path.home() / "sm-t4-inputs")
STATE = Path(os.environ["SM_STATE_DIR"]) if os.environ.get("SM_STATE_DIR") else None
STORE = Path(os.environ["SM_RELEASE_STORE"]) if os.environ.get("SM_RELEASE_STORE") else None
IMPORTS = Path(os.environ["SM_IMPORT_DIR"]) if os.environ.get("SM_IMPORT_DIR") else None
SERVICE = os.environ.get("T4_SERVICE", "mqtt")
SCRATCH = os.environ.get("T4_SCRATCH_SOURCE") == "1"
SKIP_DESTRUCTIVE = os.environ.get("T4_SKIP_DESTRUCTIVE") == "1"
PREREQ_DIR = "pre_requisite_installer"

RUN = secrets.token_hex(3)
SMALL_VER, SMALL_NAME = f"9.0.0-t4{RUN}", "T4_SMALL"
BAD_VER, BAD_NAME = f"9.2.0-t4{RUN}", "T4_BAD"
BUSY_VER = f"9.4.0-t4{RUN}"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), "" if ok else detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"), flush=True)
    return bool(ok)


def client(token: str = "") -> httpx.Client:
    c = httpx.Client(base_url=URL, timeout=120, verify=False)
    if token:
        c.headers["Authorization"] = f"Bearer {token}"
    return c


def login(user: str, pw: str) -> str:
    r = client().post("/api/auth/login", json={"username": user, "password": pw})
    r.raise_for_status()
    return r.json()["token"]


def raw(method: str, path: str, token: str = "", body: dict | None = None) -> tuple[int, str]:
    """Send the path exactly as given (no client-side normalisation of %2F / %2E)."""
    u = urlsplit(URL)
    if u.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            u.hostname, u.port, timeout=120, context=ssl._create_unverified_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=120)
    hdrs = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    r = conn.getresponse()
    out = r.status, r.read().decode(errors="replace")
    conn.close()
    return out


def make_tar(path: Path, files: dict[str, bytes], symlink: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tf:
        for n, data in files.items():
            info = tarfile.TarInfo(n)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o755
            tf.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tf.addfile(info)
    return path


def manifest_text(name: str, ver: str, installers: list[dict], desc: str = "t4") -> str:
    return yaml.safe_dump({"manifest_schema": 1, "software_name": name, "software_version": ver,
                           "description": desc, "installers": installers}, sort_keys=False)


def upload(c: httpx.Client, text: str | bytes, fname: str = "m.yml") -> httpx.Response:
    data = text.encode() if isinstance(text, str) else text
    (WORK / fname).write_bytes(data)
    return c.post("/api/releases/upload", files={"file": (fname, data)})


def wait_status(c: httpx.Client, ver: str, states: set[str], limit: float = 900) -> dict:
    end, rel = time.time() + limit, {}
    while time.time() < end:
        r = c.get(f"/api/releases/{ver}")
        if r.status_code == 200:
            rel = r.json()
            if rel["status"] in states:
                return rel
        time.sleep(2)
    return rel


def release_versions(c: httpx.Client) -> set[str]:
    return {r["version"] for r in c.get("/api/releases").json()}


def source_release() -> tuple[Path, dict]:
    for d in sorted(SRC.iterdir()):
        if not d.is_dir() or d.name == PREREQ_DIR or d.name.startswith(("T4", ".")):
            continue
        for p in sorted(d.glob("*.y*ml")):
            txt = p.read_text(errors="replace")
            if "software_version" in txt:
                return d, yaml.safe_load(txt)
    raise SystemExit(f"no release folder with a YML under {SRC}")


class HangServer:
    """Accepts HTTP connections and never answers (keeps a scan busy until stopped)."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.stop_ev = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self.sock.settimeout(0.5)
        while not self.stop_ev.is_set():
            try:
                conn, _ = self.sock.accept()
                self.conns.append(conn)
            except OSError:
                continue

    def stop(self) -> None:
        self.stop_ev.set()
        for s in self.conns + [self.sock]:
            try:
                s.close()
            except OSError:
                pass


GOOD_TPL = b"""services:
  {{ container_name }}:
    container_name: {{ container_name }}
    image: {{ registry_server }}/{{ image }}:{{ version }}
    restart: unless-stopped
"""
GOOD_PRE = b'#!/bin/bash\nset -e\nPORT="${SERVICE_PORT:-18830}"\necho "prestart $1 $PORT"\n'


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    src_dir, src_doc = source_release()
    src_ver, src_name = str(src_doc["software_version"]), str(src_doc["software_name"])
    base = next((i for i in src_doc["installers"] if i["name"] == SERVICE), None)
    if not base:
        raise SystemExit(f"installer {SERVICE} not in {src_dir}")
    spath = str(base["scripts_path"])
    if not spath.startswith(("/", "http://", "https://")):
        spath = str(src_dir / spath)
    small_inst = {"name": base["name"], "image": base["image"],
                  "image_ver": str(base.get("image_ver") or base.get("image_version")),
                  "scripts_ver": str(base.get("scripts_ver") or base.get("image_ver")),
                  "scripts_path": spath, "datafile_path": "none"}
    image, image_ver = small_inst["image"], small_inst["image_ver"]

    # ------------------------------------------------------------------ auth / roles
    check("health", client().get("/health").status_code == 200)
    admin_tok = login("admin", ADMIN_PW)
    a = client(admin_tok)
    anon = client()
    check("auth: no token -> 401", anon.get("/api/releases").status_code == 401)
    check("auth: garbage token -> 401", client("x.y").get("/api/releases").status_code == 401)
    check("auth: no token upload -> 401", anon.post("/api/releases/upload", json={"text": "x"}).status_code == 401)
    pw_v, pw_o = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
    for u, pw, role in ((f"t4viewer{RUN}", pw_v, "viewer"), (f"t4operator{RUN}", pw_o, "operator")):
        r = a.post("/api/users", json={"username": u, "password": pw, "role": role})
        check(f"admin creates {role}", r.status_code == 200, r.text)
    v = client(login(f"t4viewer{RUN}", pw_v))
    o = client(login(f"t4operator{RUN}", pw_o))
    check("viewer: GET releases 200", v.get("/api/releases").status_code == 200)
    check("viewer: GET release-source 200", v.get("/api/release-source").status_code == 200)
    check("viewer: GET bundles 200", v.get("/api/bundles").status_code == 200)
    r = v.post("/api/releases/upload", files={"file": ("m.yml", b"software_name: x")})
    check("viewer: upload -> 403", r.status_code == 403, f"{r.status_code} {r.text}")
    check("viewer: JSON upload -> 403", v.post("/api/releases/upload", json={"text": "a: 1"}).status_code == 403)
    check("viewer: build package -> 403",
          v.post("/api/bundles", json={"prereq": "x.tar", "versions": []}).status_code == 403)
    check("viewer: release-source import -> 403",
          v.post(f"/api/release-source/{src_dir.name}/import", json={"download": False}).status_code == 403)
    check("viewer: DELETE release -> 403", v.delete("/api/releases/0.0.1").status_code == 403)
    check("viewer: DELETE bundle -> 403", v.delete("/api/bundles/x.tar").status_code == 403)
    check("viewer: download bundle -> 403", v.get("/api/bundles/x.tar").status_code == 403)
    check("viewer: import-local -> 403", v.post("/api/releases/import-local").status_code == 403)
    check("viewer: rescan -> 403", v.post("/api/releases/0.0.1/scan").status_code == 403)
    check("viewer: create user -> 403",
          v.post("/api/users", json={"username": "zz", "password": "12345678", "role": "admin"}).status_code == 403)
    r = o.post("/api/releases/upload", files={"file": ("m.yml", b"a: [b")})
    check("operator: upload allowed (bad YML -> 422, not 403)", r.status_code == 422, f"{r.status_code} {r.text}")
    check("operator: DELETE release -> 403", o.delete("/api/releases/0.0.1").status_code == 403)
    check("operator: DELETE bundle -> 403", o.delete("/api/bundles/x.tar").status_code == 403)
    check("operator: download missing bundle -> 404", o.get("/api/bundles/nope.tar").status_code == 404)
    check("operator: import-local -> 403", o.post("/api/releases/import-local").status_code == 403)
    check("operator: registries edit -> 403", o.put("/api/registries", json={"host": "h"}).status_code == 403)
    check("operator: users list -> 403", o.get("/api/users").status_code == 403)

    # ------------------------------------------------------------------ registry
    r = a.put("/api/registries", json={"host": REGISTRY, "username": REG_USER,
                                       "password": Path(REG_PASSWORD_FILE).read_text().strip(),
                                       "ca_pem": Path(REG_CA).read_text()})
    check("registry saved", r.status_code == 200, r.text)
    host, _, repo = image.partition("/")
    r = o.post(f"/api/registries/{host}/test", params={"repo": repo, "tag": image_ver})
    check(f"registry has {image}:{image_ver}", r.status_code == 200 and r.json().get("ok"), r.text)

    # ------------------------------------------------------------------ release path listing / import guards
    rs = v.get("/api/release-source").json()
    check("release-source: root + exists", rs.get("root") == str(SRC) and rs.get("exists") is True, str(rs)[:300])
    item = next((x for x in rs.get("releases", []) if x["folder"] == src_dir.name), {})
    check("release-source: folder listed with version + installers",
          item.get("version") == src_ver and item.get("installers") == len(src_doc["installers"])
          and not item.get("error"), str(item)[:300])
    check("release-source: prerequisites listed",
          any(p["path"].endswith(".tar") and "/" in p["path"] for p in rs.get("prerequisites", [])),
          str(rs.get("prerequisites")))
    prereq_path = rs["prerequisites"][0]["path"] if rs.get("prerequisites") else ""
    r = o.post("/api/release-source/V0.0.0-none-t4/import", json={"download": False})
    check("import: non-existent folder -> 422", r.status_code == 422, f"{r.status_code} {r.text}")
    for bad in ("..%2Fx", "%2E%2E", "..", "%2Fetc", "%2Fetc%2Fpasswd", ".hidden", "%2E", "V1%5C..%5Cx",
                f"..%2F{src_dir.name}"):
        st, txt = raw("POST", f"/api/release-source/{bad}/import", admin_tok, {"download": False})
        check(f"import: traversal folder '{bad}' rejected (422)", st == 422, f"{st} {txt[:200]}")

    # ------------------------------------------------------------------ upload: parse-level errors
    before = release_versions(a)
    cases = [
        ("malformed YAML", b"software_name: [x\n  : :", "YAML error"),
        ("YAML list, not mapping", b"- a\n- b\n", "mapping"),
        ("missing image", manifest_text("T4P", "9.9.1", [{k: x for k, x in small_inst.items() if k != "image"}]),
         "image is required"),
        ("missing software_version", yaml.safe_dump({"software_name": "T4P", "installers": [small_inst]}),
         "software_version is required"),
        ("version with path chars", manifest_text("T4P", "../x", [small_inst]), "invalid characters"),
        ("absolute version", manifest_text("T4P", "/etc", [small_inst]), "invalid characters"),
        ("image with tag", manifest_text("T4P", "9.9.2", [{**small_inst, "image": image + ":1"}]), "tag"),
        ("empty installers", manifest_text("T4P", "9.9.3", []), "installers"),
        ("installer name traversal", manifest_text("T4P", "9.9.4", [{**small_inst, "name": "../x"}]), "name must"),
        ("duplicate installer", manifest_text("T4P", "9.9.5", [small_inst, small_inst]), "duplicate"),
    ]
    for label, text, want in cases:
        r = upload(o, text)
        body = r.text
        check(f"upload {label} -> 422 '{want}'", r.status_code == 422 and want.lower() in body.lower(),
              f"{r.status_code} {body[:300]}")
    r = upload(o, b"\xff\xfe\x00bad")
    check("upload non-UTF-8 -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = upload(o, b"#" * 2_100_000)
    check("upload > 2 MB -> 413", r.status_code == 413, f"{r.status_code}")
    r = o.post("/api/releases/upload", content=b"not json", headers={"Content-Type": "application/json"})
    check("upload no file / bad JSON -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    r = o.post("/api/releases/upload", json={"text": "a: [b"})
    check("JSON {text} malformed -> 422", r.status_code == 422 and "YAML error" in r.text, r.text[:200])
    check("parse errors created no release rows", release_versions(a) == before)

    # ------------------------------------------------------------------ small valid release (stored) in background
    r = upload(o, manifest_text(SMALL_NAME, SMALL_VER, [small_inst]), "small.yml")
    check("upload valid YML with absolute paths -> 200", r.status_code == 200 and r.json().get("ok"), r.text[:300])

    # ------------------------------------------------------------------ scan failures, one installer per case
    def bad(name: str, files: dict[str, bytes] | None = None, raw_bytes: bytes | None = None, symlink: str = "",
            **over: str) -> dict:
        inst = {**small_inst, "name": name, **over}
        if files is not None or raw_bytes is not None:
            p = WORK / f"{name}.tar"
            if raw_bytes is not None:
                p.write_bytes(raw_bytes)
            else:
                make_tar(p, files or {}, symlink)
            inst["scripts_path"] = str(p)
        return inst

    two = GOOD_TPL + b"  other:\n    image: busybox\n"
    bad_cases = {
        "ctl-good": (bad("ctl-good", {"compose_template.yml": GOOD_TPL, "pre_start.sh": GOOD_PRE}), None),
        "bad-notpl": (bad("bad-notpl", {"pre_start.sh": GOOD_PRE}), "must contain one compose_template.yml"),
        "bad-nopre": (bad("bad-nopre", {"compose_template.yml": GOOD_TPL}), "must contain one compose_template.yml"),
        "bad-twotpl": (bad("bad-twotpl", {"compose_template.yml": GOOD_TPL, "composetemplate.yml": GOOD_TPL,
                                          "pre_start.sh": GOOD_PRE}), "must contain one compose_template.yml"),
        "bad-twosvc": (bad("bad-twosvc", {"compose_template.yml": two, "pre_start.sh": GOOD_PRE}),
                       "exactly one service"),
        "bad-cname": (bad("bad-cname", {"compose_template.yml": GOOD_TPL.replace(b"{{ container_name }}", b"fixed"),
                                        "pre_start.sh": GOOD_PRE}), "container_name"),
        "bad-crlf": (bad("bad-crlf", {"compose_template.yml": GOOD_TPL,
                                      "pre_start.sh": GOOD_PRE.replace(b"\n", b"\r\n")}), "CRLF"),
        "bad-placeholder": (bad("bad-placeholder", {"compose_template.yml": GOOD_TPL + b"    x: {{ nope }}\n",
                                                    "pre_start.sh": GOOD_PRE}), "unknown placeholder"),
        "bad-jinja": (bad("bad-jinja", {"compose_template.yml": GOOD_TPL + b"{% if %}\n",
                                        "pre_start.sh": GOOD_PRE}), "template error"),
        "bad-bash": (bad("bad-bash", {"compose_template.yml": GOOD_TPL, "pre_start.sh": b"#!/bin/bash\nif then (\n"}),
                     "syntax error"),
        "bad-nottar": (bad("bad-nottar", raw_bytes=b"this is not a tar file" * 50), "tar"),
        "bad-emptytar": (bad("bad-emptytar", {}), "must contain one compose_template.yml"),
        "bad-symlink": (bad("bad-symlink", {"compose_template.yml": GOOD_TPL, "pre_start.sh": GOOD_PRE},
                            symlink="evil"), "regular files"),
        "bad-listdoc": (bad("bad-listdoc", {"compose_template.yml": b"- a\n- {{ container_name }}\n",
                                            "pre_start.sh": GOOD_PRE}), "mapping"),
        "bad-svcstr": (bad("bad-svcstr", {"compose_template.yml": b"services:\n  {{ container_name }}: hello\n",
                                          "pre_start.sh": GOOD_PRE}), "mapping"),
        "bad-svclist": (bad("bad-svclist", {"compose_template.yml": b"services:\n  - {{ container_name }}\n",
                                            "pre_start.sh": GOOD_PRE}), "mapping"),
        "bad-nonutf8": (bad("bad-nonutf8", {"compose_template.yml": b"\xff\xfeservices: {}\n",
                                            "pre_start.sh": GOOD_PRE}), "UTF-8"),
        "bad-badyaml": (bad("bad-badyaml", {"compose_template.yml": b"services: [\n  {{ container_name }}:\n",
                                            "pre_start.sh": GOOD_PRE}), "not valid YAML"),
        "bad-wire": (bad("bad-wire", {"compose_template.yml": GOOD_TPL + b"    labels:\n      com.yellowmetal.wire:"
                                                                         b" {a: 1}\n",
                                      "pre_start.sh": GOOD_PRE}), None),
        "bad-relpath": (bad("bad-relpath", scripts_path="nosuch/rel_scripts.tar"), "not found"),
        "bad-missing": (bad("bad-missing", scripts_path=str(WORK / "does-not-exist.tar")), "not found"),
        "bad-tag": (bad("bad-tag", {"compose_template.yml": GOOD_TPL, "pre_start.sh": GOOD_PRE},
                        image_ver="0.0.0-t4-missing"), "not found in registry"),
        "bad-repo": (bad("bad-repo", {"compose_template.yml": GOOD_TPL, "pre_start.sh": GOOD_PRE},
                         image=f"{REGISTRY}/t4-no-such-repo"), "not found in registry"),
    }
    bad_text = manifest_text(BAD_NAME, BAD_VER, [x[0] for x in bad_cases.values()])
    r = upload(o, bad_text, "bad.yml")
    check("upload release with broken installers -> 200 (accepted, then checked)", r.status_code == 200, r.text[:300])
    rel = wait_status(a, BAD_VER, {"scan_failed", "scanned", "downloaded", "download_failed"}, 600)
    check("broken release -> scan_failed", rel.get("status") == "scan_failed", f"status {rel.get('status')}")
    insts = {x["name"]: x for x in (rel.get("scan") or {}).get("installers", [])}
    check("broken release: every installer has its own result (no crash of the whole check)",
          set(insts) == set(bad_cases), f"got {sorted(insts)}; progress {rel.get('progress')}")
    check("broken release: no unexpected-error fallback", not (rel.get("progress") or {}).get("error"),
          str((rel.get("progress") or {}).get("error")))
    for name, (_inst, want) in bad_cases.items():
        res = insts.get(name)
        if not res:
            check(f"scan {name}", False, "no result")
            continue
        errs = " | ".join(f"{k}: {c.get('error')}" for k, c in res["checks"].items() if not c.get("ok"))
        if want is None and name == "ctl-good":
            check("scan ctl-good passes", res["ok"], errs)
        elif want is None:
            check(f"scan {name} handled (result recorded, warning)", "scripts" in res["checks"], errs)
        else:
            check(f"scan {name} -> fails '{want}'", not res["ok"] and want.lower() in errs.lower(), errs or "passed")
    tag_err = insts.get("bad-tag", {}).get("checks", {}).get("image", {}).get("error", "")
    check("missing tag error names the image tag", "0.0.0-t4-missing" in tag_err, tag_err)

    r = upload(o, bad_text, "bad.yml")
    check("re-upload scan_failed version, same content -> 200", r.status_code == 200, r.text[:200])
    rel = wait_status(a, BAD_VER, {"scan_failed", "scanned"}, 600)
    check("  re-check ends scan_failed again", rel.get("status") == "scan_failed", rel.get("status", ""))
    fewer = [bad_cases["bad-tag"][0], bad_cases["bad-crlf"][0]]
    r = upload(o, manifest_text(BAD_NAME, BAD_VER, fewer), "bad2.yml")
    check("re-upload scan_failed version, different content -> 200", r.status_code == 200, r.text[:200])
    rel = wait_status(a, BAD_VER, {"scan_failed", "scanned"}, 600)
    check("  new content is what got checked",
          rel.get("status") == "scan_failed" and len(rel.get("scan", {}).get("installers", [])) == 2,
          f"{rel.get('status')} {len(rel.get('scan', {}).get('installers', []))}")

    # ------------------------------------------------------------------ release path import (check only)
    r = o.post(f"/api/release-source/{src_dir.name}/import", json={"download": False})
    check("release-source import (download=false) -> 200", r.status_code == 200 and r.json().get("ok"), r.text[:300])
    rel = wait_status(a, src_ver, {"scanned", "scan_failed"}, 900)
    check("release-source import finishes the check", rel.get("status") in ("scanned", "scan_failed")
          and len(rel.get("scan", {}).get("installers", [])) == len(src_doc["installers"]),
          f"{rel.get('status')} {rel.get('progress')}")
    if rel.get("status") == "scan_failed":
        for x in rel.get("scan", {}).get("installers", []):
            if not x["ok"]:
                print("   note: release path installer failed check:", x["name"],
                      {k: c.get("error") for k, c in x["checks"].items() if not c["ok"]})
    r = o.post(f"/api/release-source/{src_dir.name}/import", json={"download": False})
    check("release-source import again -> 200, same version, nothing duplicated",
          r.status_code == 200 and r.json().get("version") == src_ver
          and sum(1 for x in a.get("/api/releases").json() if x["version"] == src_ver) == 1, r.text[:300])
    r = upload(o, manifest_text("T4_OTHER_NAME", src_ver, [small_inst]))
    check("same version, different software_name -> 422 clear message",
          r.status_code == 422 and src_name in r.text and "already exists" in r.text, r.text[:300])
    item = next((x for x in v.get("/api/release-source").json()["releases"] if x["folder"] == src_dir.name), {})
    check("release-source shows the stored status", item.get("status") in ("scanned", "scan_failed"), str(item)[:200])

    # ------------------------------------------------------------------ small release stored
    rel = wait_status(a, SMALL_VER, {"downloaded", "download_failed", "scan_failed"}, 1200)
    stored = rel.get("status") == "downloaded"
    check("valid YML with absolute paths -> downloaded", stored,
          f"{rel.get('status')} {rel.get('progress')} {rel.get('scan', {}).get('installers')}"[:600])
    if stored and STORE:
        check("stored image tar + scripts tar exist",
              (STORE / SMALL_VER / SERVICE / "image.tar").is_file()
              and (STORE / SMALL_VER / SERVICE / "scripts.tar").is_file())
    small_text = manifest_text(SMALL_NAME, SMALL_VER, [small_inst])
    r = upload(o, small_text, "small.yml")
    check("duplicate upload, same sha -> 422 immutable", r.status_code == 422 and "immutable" in r.text, r.text[:300])
    r = upload(o, manifest_text(SMALL_NAME, SMALL_VER, [small_inst], desc="changed"), "small2.yml")
    check("duplicate upload, different content -> 422 immutable",
          r.status_code == 422 and "immutable" in r.text, r.text[:300])
    r = upload(o, manifest_text("T4_RENAMED", SMALL_VER, [small_inst]), "small3.yml")
    check("same downloaded version, other software_name -> 422", r.status_code == 422 and SMALL_NAME in r.text,
          r.text[:300])
    check("rescan of downloaded release -> 409", o.post(f"/api/releases/{SMALL_VER}/scan").status_code == 409)
    check("stored release still downloaded + unchanged sha",
          a.get(f"/api/releases/{SMALL_VER}").json().get("manifest_sha256")
          == hashlib.sha256(small_text.encode()).hexdigest())

    # ------------------------------------------------------------------ delete while busy
    hang = HangServer()
    busy_inst = {**small_inst, "name": "busy", "scripts_path": f"http://127.0.0.1:{hang.port}/s.tar"}
    r = upload(o, manifest_text("T4_BUSY", BUSY_VER, [busy_inst]), "busy.yml")
    check("upload release whose scripts URL hangs -> 200", r.status_code == 200, r.text[:200])
    end = time.time() + 120
    while time.time() < end and not hang.conns:
        time.sleep(0.5)
    check("  check is running (scripts URL fetched)", bool(hang.conns))
    r = a.delete(f"/api/releases/{BUSY_VER}")
    check("DELETE release while its check runs -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    r = upload(o, manifest_text("T4_BUSY", BUSY_VER, [busy_inst], desc="again"), "busy2.yml")
    check("re-upload while its check runs -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    hang.stop()
    rel = wait_status(a, BUSY_VER, {"scan_failed", "scanned"}, 180)
    busy_res = (rel.get("scan") or {}).get("installers") or []
    check("  dropped connection -> scan_failed with a per-service error",
          rel.get("status") == "scan_failed" and busy_res and not busy_res[0]["checks"]["scripts"]["ok"],
          f"{rel.get('status')} {rel.get('progress')}")

    # ------------------------------------------------------------------ release delete
    check("DELETE unknown release -> 404", a.delete("/api/releases/0.0.0-nope-t4").status_code == 404)
    if not SKIP_DESTRUCTIVE:
        for bad_v in ("%2E%2E", "..", "%2E", "..%2F..", "%2Fetc"):
            st, txt = raw("DELETE", f"/api/releases/{bad_v}", admin_tok)
            check(f"DELETE release '{bad_v}' rejected (404/422)", st in (404, 405, 422), f"{st} {txt[:200]}")
        check("  manager still healthy after traversal deletes", client().get("/health").status_code == 200)
        if STATE:
            check("  state DB still there", (STATE / "software-manager.db").is_file())
        if STORE:
            check("  release store still there", STORE.is_dir() and (STORE / SMALL_VER).is_dir() == stored)
        check("  releases still listed", SMALL_VER in release_versions(a))
    if STATE and stored:
        con = sqlite3.connect(STATE / "software-manager.db", timeout=30)
        now = time.time()
        con.execute("INSERT INTO deployments(name,software_name,version,alarm_base_path,map_json,state,created_by,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"t4-fake-{RUN}", SMALL_NAME, SMALL_VER, "/tmp/t4", "{}", "new", "t4", now, now))
        con.commit()
        r = a.delete(f"/api/releases/{SMALL_VER}")
        check("DELETE release used by a deployment -> 409", r.status_code == 409 and "t4-fake" in r.text, r.text)
        rel = a.get(f"/api/releases/{SMALL_VER}").json()
        check("  release still listed with used_by", f"t4-fake-{RUN}" in rel.get("used_by", []), str(rel.get("used_by")))
        con.execute("DELETE FROM deployments WHERE name=?", (f"t4-fake-{RUN}",))
        con.commit()
        con.close()
    r = a.delete(f"/api/releases/{BAD_VER}")
    check("DELETE unused release -> 200", r.status_code == 200, r.text)
    check("  gone from the list", BAD_VER not in release_versions(a))
    check("  second DELETE -> 404", a.delete(f"/api/releases/{BAD_VER}").status_code == 404)
    a.delete(f"/api/releases/{BUSY_VER}")

    # ------------------------------------------------------------------ bundles: errors
    def post_bundle(prereq: str, versions: list[str], who: httpx.Client = o) -> httpx.Response:
        return who.post("/api/bundles", json={"prereq": prereq, "versions": versions})

    for label, pre in (("unknown prereq", "0.0.0/nope.tar"), ("empty prereq", ""),
                       ("prereq folder", prereq_path.split("/")[0] if prereq_path else "x"),
                       ("prereq traversal to release folder",
                        f"../{src_dir.name}/{Path(spath).relative_to(src_dir) if spath.startswith(str(src_dir)) else 'x'}"),
                       ("prereq absolute outside", "/etc/hostname"), ("prereq ../../", "../../etc/hostname"),
                       ("prereq sibling-prefix folder", f"../{PREREQ_DIR}_x/a.tar")):
        r = post_bundle(pre, [])
        check(f"bundle {label} -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    if prereq_path:
        r = post_bundle(prereq_path, ["0.0.0-nope-t4"])
        check("bundle unknown version -> 422", r.status_code == 422 and "not downloaded" in r.text, r.text[:200])
        r = post_bundle(prereq_path, [src_ver])
        check("bundle version not downloaded (scanned only) -> 422", r.status_code == 422, r.text[:200])
        r = post_bundle(prereq_path, ["../x"])
        check("bundle version traversal -> 422", r.status_code == 422, r.text[:200])
        r = post_bundle(prereq_path, [SMALL_VER], v)
        check("viewer bundle build -> 403", r.status_code == 403)
    check("bundle missing body -> 422", o.post("/api/bundles", json={}).status_code == 422)

    for bad_n in ("..%2F", "..%2Fx.tar", "a%2Fb.tar", "x.txt", "%2E%2E", "..", ".x.tar", "..%2F..%2Fstate%2Fsoftware-manager.db",
                  "%2Fetc%2Fpasswd", "nope.tar", "x.tar%00.txt"):
        st, txt = raw("GET", f"/api/bundles/{bad_n}", admin_tok)
        check(f"GET bundle '{bad_n}' -> 404", st == 404, f"{st} {txt[:150]}")
        st, txt = raw("DELETE", f"/api/bundles/{bad_n}", admin_tok)
        check(f"DELETE bundle '{bad_n}' -> 404", st == 404, f"{st} {txt[:150]}")
    if STATE:
        check("  state DB still there after bundle traversal", (STATE / "software-manager.db").is_file())

    # ------------------------------------------------------------------ bundles: build, download, delete
    def wait_bundle(name: str, limit: float = 1200) -> dict:
        end = time.time() + limit
        while time.time() < end:
            b = next((x for x in a.get("/api/bundles").json() if x["name"] == name), None)
            if b and not b["building"]:
                return b
            if not b:
                return {}
            time.sleep(3)
        return {"building": True}

    if prereq_path and stored:
        r = post_bundle(prereq_path, [SMALL_VER, SMALL_VER])
        check("bundle with stored release -> 200", r.status_code == 200, r.text[:200])
        bname = r.json().get("name", "")
        r = a.delete(f"/api/releases/{SMALL_VER}")
        check("DELETE release while a package with it is building -> 409", r.status_code == 409,
              f"{r.status_code} {r.text[:200]}")
        b = wait_bundle(bname)
        check("  package built, lists the release once", b.get("releases") == [SMALL_VER] and b.get("size", 0) > 0,
              str(b))
        st = o.get(f"/api/bundles/{bname}")
        dl = WORK / "bundle.tar"
        if st.status_code == 200:
            dl.write_bytes(st.content)
        check("  operator downloads package", st.status_code == 200 and dl.stat().st_size == b.get("size"),
              f"{st.status_code}")
        if dl.exists():
            names = tarfile.open(dl).getnames()
            check("  package has releases/<ver>/release.json + files/",
                  any(n.endswith(f"/releases/{SMALL_VER}/release.json") for n in names)
                  and any(f"/releases/{SMALL_VER}/files/" in n for n in names), str(names[:5]))
            dl.unlink()
        check("  operator cannot delete package", o.delete(f"/api/bundles/{bname}").status_code == 403)
        check("  admin deletes package", a.delete(f"/api/bundles/{bname}").status_code == 200)
        check("  deleted package -> 404", o.get(f"/api/bundles/{bname}").status_code == 404)
    if prereq_path:
        r = post_bundle(prereq_path, [])
        check("bundle with empty release list (prerequisites only) -> 200", r.status_code == 200, r.text[:200])
        bname = r.json().get("name", "")
        b = wait_bundle(bname)
        check("  prerequisites-only package built", b.get("size", 0) > 0 and b.get("releases") == [], str(b))
        a.delete(f"/api/bundles/{bname}")

    # ------------------------------------------------------------------ import-local
    if IMPORTS and STORE:
        def imp(folder: str, ver: str, text: str, with_files: bool = True, sha: str = "") -> Path:
            d = IMPORTS / folder
            d.mkdir(parents=True, exist_ok=True)
            meta = {"version": ver, "software_name": "T4_IMPORT", "description": "t4", "manifest_text": text,
                    "manifest_sha256": sha or hashlib.sha256(text.encode()).hexdigest(), "uploaded_by": "t4",
                    "uploaded_at": time.time(), "scan_json": json.dumps({"ok": True}), "resolved_json": "{}"}
            (d / "release.json").write_text(json.dumps(meta))
            if with_files:
                (d / "files").mkdir(exist_ok=True)
                (d / "files" / "manifest.yml").write_text(text)
            return d

        def il(who: httpx.Client = a) -> dict:
            r = who.post("/api/releases/import-local")
            return {x["version"]: x["result"] for x in r.json().get("result", [])} if r.status_code == 200 else {}

        iv = f"9.3.0-imp{RUN}"
        itext = manifest_text("T4_IMPORT", iv, [small_inst])
        d1 = imp(f"{iv}", iv, itext)
        res = il()
        check("import-local valid folder -> imported", res.get(iv) == "imported", str(res))
        check("  stored + import folder removed", (STORE / iv / "manifest.yml").is_file() and not d1.exists())
        check("  release is downloaded", a.get(f"/api/releases/{iv}").json().get("status") == "downloaded")
        d2 = imp(f"{iv}-again", iv, itext)
        res = il()
        check("import-local same release again -> 'already here', folder removed",
              res.get(iv) == "already here" and not d2.exists(), str(res))
        d3 = imp(f"{iv}-clash", iv, manifest_text("T4_IMPORT", iv, [small_inst], desc="other"))
        res = il()
        check("import-local clashing version -> skipped, left in place",
              str(res.get(iv, "")).startswith("skipped") and d3.exists(), str(res))
        shutil.rmtree(d3, ignore_errors=True)
        iv2 = f"9.3.1-imp{RUN}"
        d4 = imp(iv2, iv2, manifest_text("T4_IMPORT", iv2, [small_inst]), with_files=False)
        res = il()
        check("import-local folder without files/ -> failed, left in place",
              str(res.get(iv2, "")).startswith("failed") and d4.exists() and iv2 not in release_versions(a),
              str(res))
        shutil.rmtree(d4, ignore_errors=True)
        evil = f"t4evil{RUN}"
        d5 = imp("t4trav", f"../{evil}", manifest_text("T4_IMPORT", f"9.3.2-imp{RUN}", [small_inst]))
        res = il()
        check("import-local version '../x' in release.json -> failed, nothing written outside the store",
              not (STORE.parent / evil).exists() and f"../{evil}" not in release_versions(a)
              and not any(str(x).startswith("imported") for x in res.values()), str(res))
        shutil.rmtree(d5, ignore_errors=True)
        shutil.rmtree(STORE.parent / evil, ignore_errors=True)
        iv3 = f"9.3.3-imp{RUN}"
        d6 = imp(iv3, iv3, manifest_text("T4_IMPORT", "9.3.99", [small_inst]))
        res = il()
        check("import-local version not matching its manifest -> failed",
              iv3 not in release_versions(a) and not str(res.get(iv3, "failed")).startswith("imported"), str(res))
        shutil.rmtree(d6, ignore_errors=True)
        iv4 = f"9.3.4-imp{RUN}"
        d7 = imp(iv4, iv4, manifest_text("T4_IMPORT", iv4, [small_inst]), sha="0" * 64)
        res = il()
        check("import-local wrong manifest_sha256 -> failed", iv4 not in release_versions(a), str(res))
        shutil.rmtree(d7, ignore_errors=True)
        (IMPORTS / "t4-badjson").mkdir(exist_ok=True)
        (IMPORTS / "t4-badjson" / "release.json").write_text("{not json")
        res = il()
        check("import-local broken release.json -> failed, no crash",
              str(res.get("t4-badjson", "")).startswith("failed"), str(res))
        shutil.rmtree(IMPORTS / "t4-badjson", ignore_errors=True)
        res = il()
        check("import-local with nothing to do -> empty result", res == {}, str(res))
        a.delete(f"/api/releases/{iv}")
    else:
        print("SKIP import-local cases (SM_IMPORT_DIR / SM_RELEASE_STORE not set)")

    # ------------------------------------------------------------------ broken release-path folders (scratch only)
    if SCRATCH:
        (SRC / "T4-noyml").mkdir(exist_ok=True)
        (SRC / "T4-noyml" / "readme.txt").write_text("x")
        (SRC / "T4-badyml").mkdir(exist_ok=True)
        (SRC / "T4-badyml" / "upload_releaseinfo_x.yml").write_text("software_version: [1\n")
        (SRC / "T4-invalid").mkdir(exist_ok=True)
        (SRC / "T4-invalid" / "upload_releaseinfo_x.yml").write_text("software_name: X\nsoftware_version: 1\n")
        rs = v.get("/api/release-source").json()
        by = {x["folder"]: x for x in rs["releases"]}
        for f in ("T4-noyml", "T4-badyml", "T4-invalid"):
            check(f"release-source lists {f} with an error", bool(by.get(f, {}).get("error")), str(by.get(f)))
            r = o.post(f"/api/release-source/{f}/import", json={"download": False})
            check(f"  import {f} refused (422)", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
        for f in ("T4-noyml", "T4-badyml", "T4-invalid"):
            shutil.rmtree(SRC / f, ignore_errors=True)

    # ------------------------------------------------------------------ cleanup
    if stored:
        r = a.delete(f"/api/releases/{SMALL_VER}")
        check("DELETE stored unused release -> 200, store folder removed",
              r.status_code == 200 and (not STORE or not (STORE / SMALL_VER).exists()), r.text)
    for u in (f"t4viewer{RUN}", f"t4operator{RUN}"):
        a.delete(f"/api/users/{u}")
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # report what passed so far
        import traceback
        traceback.print_exc()
        check("test script ran to the end", False, f"{type(exc).__name__}: {exc}")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    for n, _, d in failed:
        print(f"  FAILED: {n} -- {d}")
    sys.exit(1 if failed else 0)
