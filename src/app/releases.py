"""Releases: upload (from the release path or a .yml) -> check -> store everything locally -> offline bundles.

Release path (SM_RELEASE_SOURCE, read only, the engineers' reference):
  V<version>/upload_releaseinfo_*.yml, V<version>/<name>/<name>_scripts.tar,
  pre_requisite_installer/<version>/*.tar (prereq.sh + SM stack packages).
  Relative manifest paths resolve against V<version>/.

Store (SM_RELEASE_STORE/<software_version>/), self-contained so installs need no registry or network:
  manifest.yml, resolved.json (digests, md5s, sizes, ports, health, wiring)
  <installer>/scripts.tar, <installer>/image.tar (saved from the registry), <installer>/datafile/<name>

Prerequisites packages (SM_BUNDLE_DIR): prerequisites tar + releases/<version>/{release.json, files/};
prereq.sh copies releases/ into SM_IMPORT_DIR and Software Manager registers them on start (import_local).

Scripts tar: compose_template.yml + pre_start.sh (composetemplate.yml / prestart.sh also accepted).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import jinja2
import yaml

from . import db, manifest, registry
from .config import BUNDLES, IMPORTS, RELEASES, settings
from .security import decrypt

log = logging.getLogger("software-manager.releases")
TEMPLATE_NAMES = ("compose_template.yml", "composetemplate.yml")
PRESTART_NAMES = ("pre_start.sh", "prestart.sh")
_busy: set[str] = set()
_busy_lock = threading.Lock()

DUMMY = {
    "container_name": "scan-test", "registry_server": "registry.local:5000", "image": "scan-test",
    "version": "0.0.0", "run_uid": "2759", "run_gid": "2759", "alarm_base_path": "/tmp/ym-scan/alarms",
    "node_ip": "127.0.0.1", "service_port": "65000", "mem_limit": "", "cpus": "", "devices": [],
}


class ScanError(RuntimeError):
    pass


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fileserver_auth(src: str) -> Optional[tuple[str, str]]:
    base = (db.kv_get("fileserver_base_url", "") or "").rstrip("/")
    user = db.kv_get("fileserver_user", "") or ""
    if not base or not user or not src.startswith(base):
        return None
    enc = db.kv_get("fileserver_password_enc", "")
    return user, decrypt(enc) if enc else ""


def fetch(src: str, dest: Path) -> Path:
    """Copy a local path or download an http(s) URL to dest (file-server credentials from Settings)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.startswith(("http://", "https://")):
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with httpx.stream("GET", src, timeout=httpx.Timeout(30, read=600), follow_redirects=True,
                              auth=_fileserver_auth(src)) as r:
                if r.status_code != 200:
                    raise ScanError(f"{src}: HTTP {r.status_code}")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
        except httpx.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise ScanError(f"{src}: download failed: {type(exc).__name__}: {exc}")
        tmp.replace(dest)
        return dest
    p = Path(src)
    if not p.is_file():
        raise ScanError(f"{src}: file not found or not readable from Software Manager")
    shutil.copyfile(p, dest)
    return dest


def remote_md5(src: str) -> tuple[str, int]:
    if src.startswith(("http://", "https://")):
        with tempfile.TemporaryDirectory() as td:
            p = fetch(src, Path(td) / "f")
            return md5_file(p), p.stat().st_size
    p = Path(src)
    if not p.is_file():
        raise ScanError(f"{src}: file not found or not readable from Software Manager")
    return md5_file(p), p.stat().st_size


def check_scripts_tar(path: Path) -> dict[str, Any]:
    """One compose template + one pre-start script; LF + bash -n; strict render; docker compose config."""
    out: dict[str, Any] = {"warnings": []}
    try:
        tf = tarfile.open(path)
    except tarfile.TarError as exc:
        raise ScanError(f"scripts file is not a readable tar: {exc}")
    with tf:
        members = [m for m in tf.getmembers() if not m.isdir()]
        names = [Path(m.name).name for m in members]
        if any(not m.isfile() for m in members):
            raise ScanError("scripts tar may only contain regular files")
        files = {Path(m.name).name: tf.extractfile(m).read() for m in members}  # type: ignore[union-attr]
    tnames = [n for n in TEMPLATE_NAMES if n in files]
    pnames = [n for n in PRESTART_NAMES if n in files]
    if len(tnames) != 1 or len(pnames) != 1:
        raise ScanError("scripts tar must contain one compose_template.yml and one pre_start.sh "
                        f"(or composetemplate.yml / prestart.sh); found {sorted(names)}")
    extra = sorted(set(names) - {tnames[0], pnames[0]})
    if extra:
        out["warnings"].append(f"scripts tar has extra files (ignored): {extra}")
    tname, pname = tnames[0], pnames[0]
    pre, tpl = files[pname], files[tname]
    if b"\r\n" in pre:
        raise ScanError(f"{pname} has CRLF line endings (must be LF)")
    if b"\r\n" in tpl:
        out["warnings"].append(f"{tname} has CRLF line endings")
    p = subprocess.run(["bash", "-n"], input=pre, capture_output=True, timeout=60)
    if p.returncode != 0:
        raise ScanError(f"{pname} syntax error: {p.stderr.decode(errors='replace').strip()}")
    try:
        tpl_text = tpl.decode("utf-8")
    except UnicodeDecodeError:
        raise ScanError(f"{tname} is not UTF-8 text")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    with tempfile.TemporaryDirectory() as td:
        dfp = Path(td) / "pod"
        dfp.mkdir()
        (dfp / "values.env").write_text("")
        try:
            rendered = env.from_string(tpl_text).render(**{**DUMMY, "data_file_path": str(dfp) + "/"})
        except jinja2.UndefinedError as exc:
            raise ScanError(f"{tname} uses an unknown placeholder: {exc}")
        except jinja2.TemplateError as exc:
            raise ScanError(f"{tname} template error: {exc}")
        try:
            doc = yaml.safe_load(rendered)
        except yaml.YAMLError as exc:
            raise ScanError(f"rendered {tname} is not valid YAML: {exc}")
        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            raise ScanError(f"rendered {tname} must be a YAML mapping with a 'services' mapping")
        services = doc["services"]
        if len(services) != 1:
            raise ScanError(f"{tname} must define exactly one service")
        key, svc = next(iter(services.items()))
        svc = {} if svc is None else svc
        if not isinstance(svc, dict):
            raise ScanError(f"{tname}: service '{key}' must be a mapping")
        if DUMMY["container_name"] not in (str(key), str(svc.get("container_name", ""))):
            raise ScanError(f"{tname}: service name or container_name must be {{{{container_name}}}}")
        labels = svc.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(str(x).split("=", 1) for x in labels if "=" in str(x))
        if not isinstance(labels, dict):
            raise ScanError(f"{tname}: labels must be a mapping or a list of key=value")
        if "healthcheck" not in svc:
            out["warnings"].append("no healthcheck: self-healing cannot detect a hung container")
        if (svc.get("restart") or "") not in ("unless-stopped", "always"):
            out["warnings"].append("restart policy is not unless-stopped/always")
        if "logging" not in svc:
            out["warnings"].append("no logging limits: container logs can fill the disk")
        env_files = svc.get("env_file") or []
        for ef in [env_files] if isinstance(env_files, str) else env_files:
            ep = Path(ef if isinstance(ef, str) else (ef or {}).get("path", ""))
            if str(ep).startswith(str(dfp)) and not ep.exists():
                ep.parent.mkdir(parents=True, exist_ok=True)
                ep.touch()
        rfile = Path(td) / "compose.yml"
        rfile.write_text(rendered)
        if shutil.which("docker"):
            c = subprocess.run(["docker", "compose", "-f", str(rfile), "config", "-q"], capture_output=True, text=True,
                               timeout=120)
            if c.returncode != 0:
                raise ScanError(f"docker compose config: {c.stderr.strip()[-800:]}")
        else:
            out["warnings"].append("docker CLI not available; compose config check skipped")
    try:
        wire = json.loads(labels.get("com.yellowmetal.wire") or "{}")
        if not isinstance(wire, dict):
            raise ValueError
    except (TypeError, ValueError):
        wire = {}
        out["warnings"].append("com.yellowmetal.wire label is not a JSON object string; dependency wiring disabled")
    out.update(
        port_label=str(labels.get("com.yellowmetal.port", "")),
        health=str(labels.get("com.yellowmetal.health", "")).replace(DUMMY["service_port"], "{port}"),
        wire=wire, template=tpl_text,
    )
    return out


def _prestart_default_port(path: Path) -> Optional[int]:
    import re
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if Path(m.name).name in PRESTART_NAMES:
                txt = tf.extractfile(m).read().decode(errors="replace")  # type: ignore[union-attr]
                hit = re.search(r'PORT="\$\{SERVICE_PORT:-(\d+)\}"', txt)
                return int(hit.group(1)) if hit else None
    return None


def _trivy(ref: str, host: str) -> dict:
    if not settings.sm_trivy or not shutil.which("trivy"):
        return {"status": "skipped"}
    c = registry.creds(host)
    env = {**os.environ, "TRIVY_USERNAME": c["username"], "TRIVY_PASSWORD": c["password"]}
    if c["insecure"]:
        env["TRIVY_INSECURE"] = "true"
    p = subprocess.run(["trivy", "image", "--quiet", "--format", "json", "--severity", "HIGH,CRITICAL", ref],
                       capture_output=True, text=True, env=env, timeout=900)
    if p.returncode != 0:
        return {"status": "error", "detail": p.stderr[-500:]}
    js = json.loads(p.stdout or "{}")
    counts = {"HIGH": 0, "CRITICAL": 0}
    for r in js.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            counts[v.get("Severity", "")] = counts.get(v.get("Severity", ""), 0) + 1
    return {"status": "ok", **counts}


def _cosign(ref: str) -> dict:
    if not settings.sm_cosign_pubkey or not shutil.which("cosign"):
        return {"status": "skipped"}
    p = subprocess.run(["cosign", "verify", "--key", settings.sm_cosign_pubkey, ref], capture_output=True, text=True,
                       timeout=300)
    return {"status": "ok" if p.returncode == 0 else "failed", "detail": (p.stderr or p.stdout)[-500:]}


def scan_installer(inst: manifest.Installer) -> dict[str, Any]:
    res: dict[str, Any] = {"name": inst.name, "ok": True, "checks": {}, "warnings": []}

    def check(key: str, fn: Callable[[], dict]) -> None:
        try:
            res["checks"][key] = {"ok": True, **fn()}
        except (ScanError, registry.RegistryError, OSError, tarfile.TarError) as exc:
            res["checks"][key] = {"ok": False, "error": str(exc)}
            res["ok"] = False
        except Exception as exc:  # one bad service must not abort the check of the others
            log.exception("check %s/%s failed", inst.name, key)
            res["checks"][key] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            res["ok"] = False

    if inst.image_source == "registry":
        def img() -> dict:
            d = registry.manifest_digest(inst.registry_server, inst.repo, inst.image_ver)
            ref = f"{inst.registry_server}/{inst.repo}:{inst.image_ver}"
            extra: dict[str, Any] = {"trivy": _trivy(ref, inst.registry_server), "cosign": _cosign(ref)}
            if extra["cosign"]["status"] == "failed":
                raise ScanError("cosign signature verification failed")
            block = db.kv_get("trivy_block_critical", False)
            if block and extra["trivy"].get("CRITICAL", 0) > 0:
                raise ScanError(f"trivy: {extra['trivy']['CRITICAL']} CRITICAL vulnerabilities (blocking enabled)")
            size = registry.image_size(inst.registry_server, inst.repo, inst.image_ver, settings.sm_platform)
            return {**d, **extra, "size": size}
        check("image", img)
    else:
        def imgtar() -> dict:
            md5, size = remote_md5(inst.image_tar_path)
            if inst.image_tar_md5sum and md5 != inst.image_tar_md5sum:
                raise ScanError(f"image tar md5 {md5} != manifest {inst.image_tar_md5sum}")
            return {"md5": md5, "size": size}
        check("image", imgtar)

    def scripts() -> dict:
        with tempfile.TemporaryDirectory() as td:
            p = fetch(inst.scripts_path, Path(td) / "scripts.tar")
            info = check_scripts_tar(p)
            info["md5"] = md5_file(p)
            info["size"] = p.stat().st_size
            info["default_port"] = _prestart_default_port(p)
        res["warnings"] += info.pop("warnings")
        info.pop("template", None)
        return info
    check("scripts", scripts)

    if inst.has_datafile:
        def datafile() -> dict:
            md5, size = remote_md5(inst.datafile_path)
            if inst.datafile_md5sum and md5 != inst.datafile_md5sum:
                raise ScanError(f"datafile md5 {md5} != manifest {inst.datafile_md5sum}")
            if inst.datafile_size_bytes is not None and size != inst.datafile_size_bytes:
                raise ScanError(f"datafile size {size} != manifest {inst.datafile_size_bytes}")
            return {"md5": md5, "size": size}
        check("datafile", datafile)
    return res


def _progress(version: str, **kw: Any) -> None:
    r = db.one("SELECT progress_json FROM releases WHERE version=?", version)
    prog = json.loads(r["progress_json"]) if r else {}
    prog.update(kw)
    db.ex("UPDATE releases SET progress_json=?, updated_at=? WHERE version=?", json.dumps(prog), time.time(), version)


def _set(version: str, **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    db.ex(f"UPDATE releases SET {sets}, updated_at=? WHERE version=?", *cols.values(), time.time(), version)


def source_root() -> Path:
    return Path(settings.sm_release_source)


def source_dir(version: str) -> Path:
    for name in (f"V{version}", f"v{version}", version):
        if (source_root() / name).is_dir():
            return source_root() / name
    return source_root() / f"V{version}"


def _resolve_paths(m: manifest.Manifest) -> None:
    base = source_dir(m.software_version)
    for inst in m.installers:
        for attr in ("image_tar_path", "scripts_path", "datafile_path"):
            v = getattr(inst, attr)
            if v and not v.startswith(("http://", "https://", "/")):
                setattr(inst, attr, str(base / v))


def _folder_manifest(d: Path) -> Optional[Path]:
    for p in sorted(d.glob("*.y*ml")):
        try:
            if "software_version" in p.read_text(errors="replace")[:4000]:
                return p
        except OSError:
            continue
    return None


def _dir_size(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


PREREQ_DIR = "pre_requisite_installer"


def prereq_packages() -> list[dict]:
    """<release path>/pre_requisite_installer/<version>/*.tar (read only)."""
    d = source_root() / PREREQ_DIR
    if not d.is_dir():
        return []
    return [{"path": f"{p.parent.name}/{p.name}", "version": p.parent.name, "size": p.stat().st_size}
            for p in sorted(d.glob("*/*.tar"), reverse=True)]


def source_list() -> dict:
    root = source_root()
    out: list[dict] = []
    if root.is_dir():
        for d in sorted((x for x in root.iterdir()
                         if x.is_dir() and not x.name.startswith(".") and x.name != PREREQ_DIR),
                        key=lambda x: x.name, reverse=True):
            mf = _folder_manifest(d)
            item: dict[str, Any] = {"folder": d.name, "manifest": mf.name if mf else "", "size": _dir_size(d)}
            if mf:
                try:
                    m, errors, _ = manifest.parse(mf.read_text())
                except (OSError, UnicodeDecodeError) as exc:
                    m, errors = None, [f"{mf.name}: cannot read as UTF-8 text ({exc})"]
                if m:
                    r = db.one("SELECT status, software_name FROM releases WHERE version=?", m.software_version)
                    item.update(version=m.software_version, software_name=m.software_name,
                                description=m.description, installers=len(m.installers),
                                status=r["status"] if r and r["software_name"] == m.software_name else "")
                    if r and r["software_name"] != m.software_name:
                        item["error"] = f"version {m.software_version} is already used here by {r['software_name']}"
                else:
                    item["error"] = "; ".join(errors)[:300]
            else:
                item["error"] = "no manifest (*.yml with software_version) in this folder"
            out.append(item)
    return {"root": str(root), "exists": root.is_dir(), "releases": out, "prerequisites": prereq_packages()}


def _source_folder(folder: str) -> Path:
    if not folder or "/" in folder or "\\" in folder or folder.startswith("."):
        raise ScanError("bad release folder name")
    d = source_root() / folder
    if not d.is_dir():
        raise ScanError(f"{d} not found")
    return d


def import_source(folder: str, user: str, download: bool) -> dict:
    """Upload the manifest from a release folder; scan, and download everything when download=True."""
    d = _source_folder(folder)
    mf = _folder_manifest(d)
    if not mf:
        raise ScanError(f"{d}: no manifest (*.yml with software_version)")
    try:
        text = mf.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise ScanError(f"{mf.name}: cannot read as UTF-8 text ({exc})")
    m, errors, _ = manifest.parse(text)
    if not m:
        raise ScanError("; ".join(errors))
    r = db.one("SELECT status, scan_json, software_name FROM releases WHERE version=?", m.software_version)
    if r and r["software_name"] != m.software_name:
        raise ScanError(f"version {m.software_version} already exists here for {r['software_name']}; "
                        "delete it first or use another version")
    if r and r["status"] in ("downloaded", "downloading", "scanning"):
        return {"ok": True, "version": m.software_version, "status": r["status"]}
    if r and r["status"] in ("scanned", "download_failed") and json.loads(r["scan_json"] or "{}").get("ok"):
        started = download and start_bg(download_release, m.software_version)
        return {"ok": True, "version": m.software_version, "status": "downloading" if started else r["status"]}
    res = upload(text, user, then_download=download)
    if not res.get("ok"):
        raise ScanError("; ".join(res.get("errors", [])))
    return {**res, "status": "scanning"}


_bundles_building: dict[str, list[str]] = {}
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.tar$")


def valid_version(version: str) -> bool:
    return bool(manifest.VER_RE.match(version or ""))


def busy_reason(version: str) -> str:
    """Why a stored release must not be removed right now ('' = free)."""
    with _busy_lock:
        if version in _busy:
            return "a check or download of this release is running"
        if any(version in vs for vs in _bundles_building.values()):
            return "a prerequisites package with this release is being built"
    return ""


def _bundle_info(p: Path) -> Optional[dict]:
    try:
        st = p.stat()
    except OSError:  # deleted while listing
        return None
    try:
        rels = json.loads(p.with_suffix(".json").read_text()).get("releases", [])
    except (OSError, ValueError, AttributeError):
        rels = []
    return {"name": p.name, "size": st.st_size, "created": st.st_mtime, "building": False, "releases": rels}


def bundle_list() -> list[dict]:
    out = [x for x in (_bundle_info(p) for p in BUNDLES.glob("*.tar")) if x]
    out.sort(key=lambda x: x["created"], reverse=True)
    return [{"name": n, "building": True, "size": 0, "created": time.time(), "releases": []}
            for n in sorted(_bundles_building)] + out


def bundle_path(name: str) -> Path:
    if not BUNDLE_RE.match(name):
        raise ScanError("bad bundle name")
    p = BUNDLES / name
    if not p.is_file():
        raise ScanError(f"{name} not found")
    return p


def bundle_start(prereq: str, versions: list[str], user: str) -> str:
    """Prerequisites package for offline sites: the prerequisites tar from the release path (read only) +
    the selected downloaded releases under <top>/releases/<version>/. prereq.sh hands them to Software Manager."""
    pdir = (source_root() / PREREQ_DIR).resolve()
    src = (pdir / prereq).resolve() if prereq else pdir
    if pdir not in src.parents or src.suffix != ".tar" or not src.is_file():
        raise ScanError(f"prerequisites package {prereq!r} not found under {source_root() / PREREQ_DIR}")
    versions = list(dict.fromkeys(versions))
    rows = []
    for v in versions:
        r = db.one("SELECT * FROM releases WHERE version=?", v) if valid_version(v) else None
        if not r or r["status"] != "downloaded" or not (RELEASES / v).is_dir():
            raise ScanError(f"release {v} is not downloaded at this Software Manager")
        rows.append(r)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    with _busy_lock:
        gone = [v for v in versions if v in _busy]
        if gone:
            raise ScanError(f"release(s) {gone} are being changed or deleted; try again")
        name, n = f"{src.stem}_{src.parent.name}_{stamp}.tar", 1
        while name in _bundles_building or (BUNDLES / name).exists():
            n += 1
            name = f"{src.stem}_{src.parent.name}_{stamp}-{n}.tar"
        _bundles_building[name] = versions
    db.audit(user, "bundle_build", name, {"prereq": prereq, "releases": versions})

    def run() -> None:
        tmp = BUNDLES / f".{name}.part"
        try:
            with tarfile.open(src) as stf, tarfile.open(tmp, "w") as out:
                top = ""
                for mem in stf:
                    top = top or mem.name.split("/", 1)[0]
                    out.addfile(mem, stf.extractfile(mem) if mem.isfile() else None)
                for r in rows:
                    v = r["version"]
                    meta = {k: r[k] for k in ("version", "software_name", "description", "manifest_text",
                                              "manifest_sha256", "uploaded_by", "uploaded_at", "scan_json",
                                              "resolved_json")}
                    data = json.dumps(meta).encode()
                    info = tarfile.TarInfo(f"{top}/releases/{v}/release.json")
                    info.size, info.mtime = len(data), int(time.time())
                    out.addfile(info, io.BytesIO(data))
                    out.add(RELEASES / v, arcname=f"{top}/releases/{v}/files")
            (BUNDLES / name).with_suffix(".json").write_text(json.dumps({"prereq": prereq, "releases": versions}))
            tmp.replace(BUNDLES / name)
            db.audit("system", "bundle_build", name, {"ok": True})
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            if not (BUNDLES / name).exists():
                (BUNDLES / name).with_suffix(".json").unlink(missing_ok=True)
            log.exception("bundle %s failed", name)
            db.audit("system", "bundle_build", name, {"ok": False, "error": str(exc)})
        finally:
            with _busy_lock:
                _bundles_building.pop(name, None)
    threading.Thread(target=run, daemon=True, name=f"bundle-{name}").start()
    return name


def bundle_delete(name: str) -> None:
    p = bundle_path(name)
    p.unlink()
    p.with_suffix(".json").unlink(missing_ok=True)


def cleanup_partials() -> None:
    """Remove temp folders / files left by a download or package build that the last stop interrupted."""
    for p in list(RELEASES.glob(".*.partial")) + list(BUNDLES.glob(".*.part")):
        log.info("removing interrupted temp %s", p)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


_import_lock = threading.Lock()


def import_local() -> list[dict]:
    """Register releases that prereq.sh copied into IMPORTS/<version>/{release.json, files/} as downloaded."""
    with _import_lock:
        return _import_local()


def _import_local() -> list[dict]:
    done: list[dict] = []
    if not IMPORTS.is_dir():
        return done
    for d in sorted(x for x in IMPORTS.iterdir() if x.is_dir() and not x.name.startswith(".")):
        meta_p, files = d / "release.json", d / "files"
        try:
            meta = json.loads(meta_p.read_text())
            if not isinstance(meta, dict):
                raise ValueError("release.json is not an object")
            v = meta["version"]
            if not isinstance(v, str) or not manifest.VER_RE.match(v):
                raise ValueError(f"bad version {v!r} in release.json")
            m, errors, _ = manifest.parse(meta["manifest_text"])
            if not m:
                raise ScanError(f"manifest in release.json is invalid: {'; '.join(errors)[:300]}")
            if m.software_version != v or m.sha256 != meta["manifest_sha256"]:
                raise ScanError("release.json version / manifest_sha256 do not match its manifest")
            old = db.one("SELECT software_name, manifest_sha256, status FROM releases WHERE version=?", v)
            if old and old["manifest_sha256"] == meta["manifest_sha256"] and old["status"] == "downloaded":
                shutil.rmtree(d, ignore_errors=True)
                done.append({"version": v, "result": "already here"})
                continue
            if old:
                done.append({"version": v, "result": f"skipped: version {v} already exists here ({old['software_name']})"})
                continue
            if not files.is_dir():
                raise ScanError(f"{d}: files/ missing")
            shutil.rmtree(RELEASES / v, ignore_errors=True)
            shutil.move(str(files), str(RELEASES / v))
            now = time.time()
            try:
                db.ex("INSERT INTO releases(version,software_name,description,manifest_text,manifest_sha256,"
                      "uploaded_by,uploaded_at,status,scan_json,progress_json,resolved_json,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      v, m.software_name, m.description, meta["manifest_text"],
                      meta["manifest_sha256"], meta.get("uploaded_by", "import"), meta.get("uploaded_at", now),
                      "downloaded", meta.get("scan_json") or "{}", "{}", meta.get("resolved_json") or "{}", now)
            except Exception:
                shutil.move(str(RELEASES / v), str(files))
                raise
            db.audit("system", "release_import", v, {"from": str(d)})
            shutil.rmtree(d, ignore_errors=True)
            done.append({"version": v, "result": "imported"})
        except Exception as exc:
            log.error("import %s failed: %s", d, exc)
            done.append({"version": d.name, "result": f"failed: {exc}"})
    return done


def scan_then_download(version: str) -> None:
    scan(version)
    r = db.one("SELECT status FROM releases WHERE version=?", version)
    if r and r["status"] == "scanned":
        download(version)


def download_release(version: str) -> None:
    download(version)


def upload(text: str, user: str, then_download: bool = False) -> dict:
    m, errors, warnings = manifest.parse(text)
    if not m:
        return {"ok": False, "errors": errors, "warnings": warnings}
    why = _reserve(m.software_version)
    if why:
        return {"ok": False, "errors": [f"release {m.software_version}: {why}; try again when it has finished"],
                "warnings": warnings}
    started = False
    try:
        old = db.one("SELECT status, software_name FROM releases WHERE version=?", m.software_version)
        if old and old["software_name"] != m.software_name:
            return {"ok": False, "errors": [f"version {m.software_version} already exists here for "
                                            f"{old['software_name']}"], "warnings": warnings}
        if old and old["status"] in ("downloaded", "downloading", "scanning"):
            return {"ok": False, "errors": [f"release {m.software_version} already exists ({old['status']}); releases "
                                            "are immutable, ship a fix as a new software_version"], "warnings": warnings}
        now = time.time()
        db.ex("INSERT INTO releases(version,software_name,description,manifest_text,manifest_sha256,uploaded_by,"
              "uploaded_at,status,scan_json,progress_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(version) DO UPDATE SET software_name=excluded.software_name,"
              " description=excluded.description, manifest_text=excluded.manifest_text,"
              " manifest_sha256=excluded.manifest_sha256, uploaded_by=excluded.uploaded_by,"
              " uploaded_at=excluded.uploaded_at, status='uploaded', scan_json=excluded.scan_json,"
              " progress_json='{}', updated_at=excluded.updated_at",
              m.software_version, m.software_name, m.description, text, m.sha256, user, now, "uploaded",
              json.dumps({"warnings": warnings}), "{}", now)
        db.audit(user, "release_upload", m.software_version, {"sha256": m.sha256, "installers": len(m.installers)})
        started = start_bg(scan_then_download if then_download else scan, m.software_version, reserved=True)
    finally:
        if not started:
            _unreserve(m.software_version)
    return {"ok": True, "version": m.software_version, "sha256": m.sha256, "warnings": warnings,
            "installers": [i.name for i in m.installers]}


def _reserve(version: str) -> str:
    """Atomically mark a version busy; returns why it is already busy ('' = reserved now)."""
    with _busy_lock:
        if version in _busy:
            return "a check or download of this release is running"
        if any(version in vs for vs in _bundles_building.values()):
            return "a prerequisites package with this release is being built"
        _busy.add(version)
    return ""


def _unreserve(version: str) -> None:
    with _busy_lock:
        _busy.discard(version)


def start_bg(fn: Callable[[str], None], version: str, reserved: bool = False) -> bool:
    if not reserved:
        with _busy_lock:
            if version in _busy:
                return False
            _busy.add(version)

    def run() -> None:
        try:
            fn(version)
        except Exception as exc:
            log.exception("%s %s failed", fn.__name__, version)
            r = db.one("SELECT status FROM releases WHERE version=?", version)
            failed = {"uploaded": "scan_failed", "scanning": "scan_failed",
                      "downloading": "download_failed"}.get(r["status"] if r else "")
            if failed:
                _set(version, status=failed)
                _progress(version, error=f"{type(exc).__name__}: {exc}")
        finally:
            with _busy_lock:
                _busy.discard(version)
    threading.Thread(target=run, daemon=True, name=f"{fn.__name__}-{version}").start()
    return True


def load(version: str) -> tuple[dict, manifest.Manifest]:
    r = db.one("SELECT * FROM releases WHERE version=?", version)
    if not r:
        raise KeyError(version)
    m, errors, _ = manifest.parse(r["manifest_text"])
    if not m:
        raise ScanError("; ".join(errors))
    _resolve_paths(m)
    return r, m


def scan(version: str) -> None:
    r, m = load(version)
    _set(version, status="scanning", progress_json="{}")
    results = []
    names = [i.name for i in m.installers]
    for idx, inst in enumerate(m.installers):
        _progress(version, phase="scan", current=inst.name, done=idx, total=len(names))
        results.append(scan_installer(inst))
    other = db.one("SELECT 1 AS x FROM releases WHERE version<>? AND software_name<>? LIMIT 1", version, m.software_name)
    warnings = json.loads(r["scan_json"] or "{}").get("warnings", [])
    if other:
        warnings.append("other software names exist at this branch; deployments are per software_name")
    ok = all(x["ok"] for x in results)
    scan_doc = {"ok": ok, "at": time.time(), "installers": results, "warnings": warnings, "sha256": m.sha256}
    _set(version, status="scanned" if ok else "scan_failed", scan_json=json.dumps(scan_doc))
    _progress(version, phase="scan", current="", done=len(names), total=len(names))
    db.audit("system", "release_scan", version, {"ok": ok})


def download(version: str) -> None:
    r, m = load(version)
    scan_doc = json.loads(r["scan_json"] or "{}")
    if r["status"] not in ("scanned", "download_failed") or not scan_doc.get("ok"):
        raise ScanError("release must pass the scan before download")
    if scan_doc.get("sha256") != m.sha256 or r["manifest_sha256"] != m.sha256:
        raise ScanError("manifest changed after the scan; scan again")
    by_name = {x["name"]: x for x in scan_doc["installers"]}
    need = 0
    for x in scan_doc["installers"]:
        for k in ("scripts", "datafile", "image"):
            need += int(x["checks"].get(k, {}).get("size") or 0)
    free = shutil.disk_usage(RELEASES).free
    if free < need + settings.sm_min_free_gb * 1e9:
        _set(version, status="download_failed")
        _progress(version, phase="download", error=f"not enough disk: need {need / 1e9:.1f} GB + "
                  f"{settings.sm_min_free_gb} GB reserve, free {free / 1e9:.1f} GB")
        return
    _set(version, status="downloading", progress_json="{}")
    base = RELEASES / version
    tmp = RELEASES / f".{version}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    resolved: dict[str, Any] = {}
    try:
        for idx, inst in enumerate(m.installers):
            _progress(version, phase="download", current=inst.name, done=idx, total=len(m.installers),
                      items={**(json.loads(db.one("SELECT progress_json FROM releases WHERE version=?", version)
                                               ["progress_json"]).get("items") or {}), inst.name: "downloading"})
            sc = by_name[inst.name]["checks"]
            d = tmp / inst.name
            st = fetch(inst.scripts_path, d / "scripts.tar")
            if md5_file(st) != sc["scripts"]["md5"]:
                raise ScanError(f"{inst.name}: scripts tar changed after the scan")
            info = check_scripts_tar(st)
            entry: dict[str, Any] = {
                "image_source": inst.image_source, "scripts_md5": sc["scripts"]["md5"],
                "port": sc["scripts"].get("default_port"), "health": info["health"], "wire": info["wire"],
            }
            if inst.image_source == "registry":
                ref = f"{inst.registry_server}/{inst.repo}:{inst.image_ver}"
                it = d / "image.tar"
                now = registry.save_image(inst.registry_server, inst.repo, inst.image_ver, ref, it,
                                          settings.sm_platform)
                if now != sc["image"]["digest"]:
                    it.unlink(missing_ok=True)
                    raise ScanError(f"{inst.name}: image tag was re-pushed after the scan "
                                    f"({sc['image']['digest']} -> {now})")
                entry.update(digest=now, image_source="tar", image_tar_md5=md5_file(it),
                             image_size=it.stat().st_size)
            else:
                it = fetch(inst.image_tar_path, d / "image.tar")
                if md5_file(it) != sc["image"]["md5"]:
                    raise ScanError(f"{inst.name}: image tar changed after the scan")
                entry["image_tar_md5"] = sc["image"]["md5"]
            if inst.has_datafile:
                dname = Path(inst.datafile_path.split("?")[0]).name or "datafile"
                df = fetch(inst.datafile_path, d / "datafile" / dname)
                md5 = md5_file(df)
                if md5 != sc["datafile"]["md5"]:
                    raise ScanError(f"{inst.name}: datafile changed after the scan")
                entry.update(datafile_md5=md5, datafile_size=df.stat().st_size, datafile_name=dname)
            resolved[inst.name] = entry
            prog = json.loads(db.one("SELECT progress_json FROM releases WHERE version=?", version)["progress_json"])
            prog.setdefault("items", {})[inst.name] = "downloaded"
            _progress(version, items=prog["items"])
        (tmp / "manifest.yml").write_text(r["manifest_text"])
        (tmp / "resolved.json").write_text(json.dumps(resolved, indent=1))
        shutil.rmtree(base, ignore_errors=True)
        tmp.replace(base)
        _set(version, status="downloaded", resolved_json=json.dumps(resolved))
        _progress(version, phase="download", current="", done=len(m.installers), total=len(m.installers))
        db.audit("system", "release_download", version, {"ok": True})
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        _set(version, status="download_failed")
        _progress(version, phase="download", error=str(exc))
        db.audit("system", "release_download", version, {"ok": False, "error": str(exc)})
        raise


def file_path(version: str, name: str, kind: str) -> Path:
    if not valid_version(version) or not manifest.NAME_RE.match(name):
        raise FileNotFoundError(f"{version}/{name}/{kind}")
    base = RELEASES / version / name
    p: Optional[Path] = None
    if kind == "scripts":
        p = base / "scripts.tar"
    elif kind == "image":
        p = base / "image.tar"
    elif kind == "datafile":
        p = next((f for f in sorted((base / "datafile").glob("*")) if f.is_file()), None)
    if p is None or not p.is_file():
        raise FileNotFoundError(f"{version}/{name}/{kind}")
    return p


def delete(version: str) -> None:
    """Raises KeyError (unknown) or ScanError (busy)."""
    if not valid_version(version) or not db.one("SELECT 1 AS x FROM releases WHERE version=?", version):
        raise KeyError(version)
    why = _reserve(version)
    if why:
        raise ScanError(f"release {version} cannot be deleted now: {why}")
    try:
        shutil.rmtree(RELEASES / version, ignore_errors=True)
        db.ex("DELETE FROM releases WHERE version=?", version)
    finally:
        _unreserve(version)
