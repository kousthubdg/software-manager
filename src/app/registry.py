"""Registry v2 client: manifest HEAD (digest) with Basic or Bearer-token auth, per-registry CA / insecure."""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import shutil
import ssl
import tarfile
from pathlib import Path
from typing import Optional

import httpx

from . import db
from .config import STATE
from .security import decrypt

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class RegistryError(RuntimeError):
    pass


def creds(host: str) -> dict:
    r = db.one("SELECT * FROM registries WHERE host=?", host) or {}
    verify: object = True
    if r.get("ca_pem"):
        p = STATE / "ca" / (re.sub(r"[^A-Za-z0-9._-]", "_", host) + ".pem")
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file() or p.read_text() != r["ca_pem"]:
            tmp = p.with_name(f".{p.name}.{secrets.token_hex(4)}")
            tmp.write_text(r["ca_pem"])
            tmp.replace(p)
        ctx = ssl.create_default_context(cadata=r["ca_pem"])
        # Private registry CAs made with plain `openssl req -x509` lack keyUsage, which strict X.509 mode rejects.
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        verify = ctx
    elif r.get("insecure"):
        verify = False
    pw = decrypt(r["password_enc"]) if r.get("password_enc") else ""
    return {"username": r.get("username", ""), "password": pw, "verify": verify, "insecure": bool(r.get("insecure"))}


def docker_auth(host: str) -> Optional[dict]:
    c = creds(host)
    return {"username": c["username"], "password": c["password"]} if c["username"] else None


def _bearer(resp: httpx.Response, repo: str, c: dict, client: httpx.Client) -> Optional[str]:
    hdr = resp.headers.get("www-authenticate", "")
    if not hdr.lower().startswith("bearer"):
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', hdr))
    realm = params.get("realm")
    if not realm:
        return None
    q = {"service": params.get("service", ""), "scope": params.get("scope") or f"repository:{repo}:pull"}
    auth = (c["username"], c["password"]) if c["username"] else None
    t = client.get(realm, params=q, auth=auth)
    if t.status_code != 200:
        raise RegistryError(f"token endpoint {realm}: HTTP {t.status_code}")
    js = t.json()
    return js.get("token") or js.get("access_token")


def manifest_digest(host: str, repo: str, tag: str) -> dict:
    """HEAD /v2/<repo>/manifests/<tag> -> {'digest', 'media_type', 'scheme'}; raises RegistryError."""
    c = creds(host)
    auth = (c["username"], c["password"]) if c["username"] else None
    last_err = ""
    for scheme in (["https", "http"] if c["insecure"] else ["https"]):
        url = f"{scheme}://{host}/v2/{repo}/manifests/{tag}"
        try:
            with httpx.Client(verify=c["verify"], timeout=20, follow_redirects=True) as cl:
                hdrs, gauth = {"Accept": ACCEPT}, auth
                r = cl.head(url, headers=hdrs, auth=auth)
                if r.status_code == 401:
                    tok = _bearer(r, repo, c, cl)
                    if tok:
                        hdrs, gauth = {**hdrs, "Authorization": f"Bearer {tok}"}, None
                        r = cl.head(url, headers=hdrs)
                if r.status_code == 404:
                    raise RegistryError(f"{host}/{repo}:{tag} not found in registry")
                if r.status_code == 401:
                    raise RegistryError(f"{host}: unauthorized (set registry credentials in Settings)")
                if r.status_code != 200:
                    raise RegistryError(f"{host}/{repo}:{tag}: HTTP {r.status_code}")
                digest = r.headers.get("docker-content-digest", "")
                if not digest:
                    g = cl.get(url, headers=hdrs, auth=gauth)
                    if g.status_code != 200:
                        raise RegistryError(f"{host}/{repo}:{tag}: HTTP {g.status_code}")
                    digest = "sha256:" + hashlib.sha256(g.content).hexdigest()
                return {"digest": digest, "media_type": r.headers.get("content-type", ""), "scheme": scheme}
        except RegistryError:
            raise
        except httpx.HTTPError as exc:
            last_err = f"{scheme}://{host}: {exc}"
    raise RegistryError(last_err or f"{host}: unreachable")


def _get(cl: httpx.Client, url: str, repo: str, c: dict, accept: str = "", stream: bool = False) -> httpx.Response:
    auth = (c["username"], c["password"]) if c["username"] else None
    hdrs = {"Accept": accept} if accept else {}
    r = cl.send(cl.build_request("GET", url, headers=hdrs), auth=auth, stream=stream)
    if r.status_code == 401:
        r.close()
        tok = _bearer(r, repo, c, cl)
        if tok:
            req = cl.build_request("GET", url, headers={**hdrs, "Authorization": f"Bearer {tok}"})
            r = cl.send(req, stream=stream)
    if r.status_code != 200:
        r.close()
        raise RegistryError(f"{url}: HTTP {r.status_code}")
    return r


def _image_manifest(cl: httpx.Client, base: str, repo: str, tag: str, c: dict, platform: str) -> tuple[str, dict]:
    """(digest of the tag, single-platform image manifest)."""
    r = _get(cl, f"{base}/manifests/{tag}", repo, c, ACCEPT)
    digest = r.headers.get("docker-content-digest") or "sha256:" + hashlib.sha256(r.content).hexdigest()
    m = r.json()
    if m.get("manifests"):
        os_, arch = (platform.split("/") + ["amd64"])[:2]
        pick = next((x for x in m["manifests"] if (x.get("platform") or {}).get("os") == os_
                     and (x.get("platform") or {}).get("architecture") == arch), None)
        if not pick:
            raise RegistryError(f"{repo}:{tag}: no {platform} image in the manifest list")
        m = _get(cl, f"{base}/manifests/{pick['digest']}", repo, c, ACCEPT).json()
    if not m.get("config") or "layers" not in m:
        raise RegistryError(f"{repo}:{tag}: unsupported manifest")
    return digest, m


def _scheme(host: str, c: dict) -> str:
    """https, or for registries marked insecure: https without verification if it answers, else plain http."""
    if not c["insecure"]:
        return "https"
    try:
        with httpx.Client(verify=False, timeout=10) as cl:
            cl.get(f"https://{host}/v2/")
        return "https"
    except httpx.HTTPError:
        return "http"


def image_size(host: str, repo: str, tag: str, platform: str) -> int:
    c = creds(host)
    scheme = _scheme(host, c)
    with httpx.Client(verify=c["verify"], timeout=60, follow_redirects=True) as cl:
        _, m = _image_manifest(cl, f"{scheme}://{host}/v2/{repo}", repo, tag, c, platform)
    return int(m["config"].get("size", 0)) + sum(int(x.get("size", 0)) for x in m["layers"])


def save_image(host: str, repo: str, tag: str, ref: str, dest: Path, platform: str) -> str:
    """Write `docker load`-able tar (docker-archive) of host/repo:tag tagged as ref. Returns the tag digest."""
    c = creds(host)
    scheme = _scheme(host, c)
    base = f"{scheme}://{host}/v2/{repo}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.parent / f".{dest.name}.blobs"
    work.mkdir(exist_ok=True)
    try:
        with httpx.Client(verify=c["verify"], timeout=httpx.Timeout(30, read=600), follow_redirects=True) as cl:
            digest, m = _image_manifest(cl, base, repo, tag, c, platform)
            names: list[tuple[Path, str]] = []
            for blob in [m["config"]] + list(m["layers"]):
                d = blob["digest"]
                p = work / d.replace(":", "_")
                if not p.exists() or p.stat().st_size != int(blob.get("size", -1)):
                    h = hashlib.sha256()
                    r = _get(cl, f"{base}/blobs/{d}", repo, c, stream=True)
                    try:
                        with open(p, "wb") as f:
                            for chunk in r.iter_bytes(1 << 20):
                                h.update(chunk)
                                f.write(chunk)
                    finally:
                        r.close()
                    if "sha256:" + h.hexdigest() != d:
                        p.unlink(missing_ok=True)
                        raise RegistryError(f"{repo}:{tag}: blob {d[:19]} checksum mismatch")
                hexd = d.split(":", 1)[1]
                names.append((p, f"{hexd}.json" if blob is m["config"] else f"{hexd}/layer.tar"))
        mf = [{"Config": names[0][1], "RepoTags": [ref], "Layers": [n for _, n in names[1:]]}]
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with tarfile.open(tmp, "w") as tf:
                for p, n in names:
                    tf.add(p, arcname=n)
                data = json.dumps(mf).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            tmp.replace(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return digest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ca_file(host: str) -> Optional[Path]:
    p = STATE / "ca" / (re.sub(r"[^A-Za-z0-9._-]", "_", host) + ".pem")
    return p if p.exists() else None
