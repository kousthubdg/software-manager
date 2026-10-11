"""Yellow Metal Platform release manifest: parse + validate (format as uploaded by engineers).

software_name / software_version / description / installers[]:
  name, image (registry/repo, no tag), image_ver | image_version (alias), scripts_ver, scripts_path,
  datafile_path / datafile_ver / datafile_md5sum / datafile_size_bytes ("none" or missing = no datafile),
  build_no (optional), image_source: registry | tar (+ image_tar_path, image_tar_md5sum),
  depends_on: [..] (optional), self: true (Software Manager stack, self-update only)
Optional top-level: manifest_schema: 1
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import yaml

NONE = {"", "none", "null", "na", "n/a", "-"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
VER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
REPO_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
MD5_RE = re.compile(r"^[a-fA-F0-9]{32}$")


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _none(v: Any) -> bool:
    return _s(v).lower() in NONE


@dataclass
class Installer:
    name: str
    image: str
    registry_server: str
    repo: str
    image_ver: str
    image_source: str = "registry"
    image_tar_path: str = ""
    image_tar_md5sum: str = ""
    scripts_ver: str = ""
    scripts_path: str = ""
    datafile_path: str = ""
    datafile_ver: str = ""
    datafile_md5sum: str = ""
    datafile_size_bytes: Optional[int] = None
    build_no: str = ""
    depends_on: list[str] = field(default_factory=list)
    self_update: bool = False

    @property
    def has_datafile(self) -> bool:
        return bool(self.datafile_path)


@dataclass
class Manifest:
    software_name: str
    software_version: str
    description: str
    manifest_schema: int
    installers: list[Installer]
    sha256: str

    def to_dict(self) -> dict:
        return asdict(self)

    def get(self, name: str) -> Optional[Installer]:
        return next((i for i in self.installers if i.name == name), None)


def split_image(image: str) -> tuple[str, str]:
    """'host:5000/team/app' -> ('host:5000', 'team/app'); 'app' -> ('', 'app')."""
    parts = image.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        return parts[0], parts[1]
    return "", image


def parse(text: str) -> tuple[Optional[Manifest], list[str], list[str]]:
    """Returns (manifest or None, errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    sha = hashlib.sha256(text.encode()).hexdigest()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"YAML error: {exc}"], []
    if not isinstance(data, dict):
        return None, ["manifest must be a YAML mapping"], []
    name = _s(data.get("software_name"))
    ver = _s(data.get("software_version"))
    if not name:
        errors.append("software_name is required")
    if not ver:
        errors.append("software_version is required")
    elif not VER_RE.match(ver):
        errors.append(f"software_version '{ver}' has invalid characters")
    schema = data.get("manifest_schema", 1)
    if str(schema) not in ("1",):
        errors.append(f"manifest_schema {schema} is not supported (expected 1)")
    raw = data.get("installers")
    if not isinstance(raw, list) or not raw:
        errors.append("installers must be a non-empty list")
        raw = []
    known = {"name", "image", "image_ver", "image_version", "image_source", "image_tar_path", "image_tar_md5sum",
             "scripts_ver", "scripts_path", "datafile_path", "datafile_ver", "datafile_md5sum",
             "datafile_size_bytes", "build_no", "depends_on", "self"}
    installers: list[Installer] = []
    seen: set[str] = set()
    for idx, it in enumerate(raw):
        where = f"installers[{idx}]"
        if not isinstance(it, dict):
            errors.append(f"{where} must be a mapping")
            continue
        iname = _s(it.get("name"))
        where = f"installer '{iname or idx}'"
        for k in it:
            if k not in known:
                warnings.append(f"{where}: unknown key '{k}' ignored")
        if not iname:
            errors.append(f"{where}: name is required")
        elif not NAME_RE.match(iname):
            errors.append(f"{where}: name must be lowercase letters, digits, '-', '_' or '.'")
        elif iname in seen:
            errors.append(f"{where}: duplicate name")
        seen.add(iname)
        image = _s(it.get("image"))
        iv, iv2 = _s(it.get("image_ver")), _s(it.get("image_version"))
        if iv and iv2 and iv != iv2:
            errors.append(f"{where}: image_ver '{iv}' and image_version '{iv2}' differ")
        image_ver = iv or iv2
        if not image:
            errors.append(f"{where}: image is required")
        reg, repo = split_image(image)
        if image and ("@" in image or (":" in repo)):
            errors.append(f"{where}: image must not contain a tag or digest; use image_ver")
        if repo and not REPO_RE.match(repo):
            errors.append(f"{where}: image repository '{repo}' is not a valid name")
        if not image_ver:
            errors.append(f"{where}: image_ver (or image_version) is required")
        elif not VER_RE.match(image_ver):
            errors.append(f"{where}: image_ver '{image_ver}' is not a valid tag")
        source = _s(it.get("image_source")).lower() or "registry"
        if source not in ("registry", "tar"):
            errors.append(f"{where}: image_source must be 'registry' or 'tar'")
        tar_path = "" if _none(it.get("image_tar_path")) else _s(it.get("image_tar_path"))
        tar_md5 = "" if _none(it.get("image_tar_md5sum")) else _s(it.get("image_tar_md5sum")).lower()
        if source == "registry" and not reg:
            errors.append(f"{where}: image must include the registry host (<registry-host>:<port>/{repo or 'svc'})")
        if source == "tar" and not tar_path:
            errors.append(f"{where}: image_source tar needs image_tar_path")
        if tar_md5 and not MD5_RE.match(tar_md5):
            errors.append(f"{where}: image_tar_md5sum is not an md5")
        spath = _s(it.get("scripts_path"))
        if _none(spath):
            errors.append(f"{where}: scripts_path is required")
            spath = ""
        elif not (spath.endswith(".tar")):
            warnings.append(f"{where}: scripts_path does not end with .tar")
        sver = "" if _none(it.get("scripts_ver")) else _s(it.get("scripts_ver"))
        if not sver:
            sver = image_ver
            warnings.append(f"{where}: scripts_ver missing; using image_ver {image_ver}")
        dpath = "" if _none(it.get("datafile_path")) else _s(it.get("datafile_path"))
        dmd5 = "" if _none(it.get("datafile_md5sum")) else _s(it.get("datafile_md5sum")).lower()
        dver = "" if _none(it.get("datafile_ver")) else _s(it.get("datafile_ver"))
        dsize_raw = it.get("datafile_size_bytes")
        dsize: Optional[int] = None
        if dpath:
            if not dmd5:
                warnings.append(f"{where}: datafile_md5sum missing; md5 will be computed but not verified")
            elif not MD5_RE.match(dmd5):
                errors.append(f"{where}: datafile_md5sum is not an md5")
            if not dver:
                dver = image_ver
            if not _none(dsize_raw):
                try:
                    dsize = int(str(dsize_raw).strip())
                except ValueError:
                    errors.append(f"{where}: datafile_size_bytes must be an integer")
        deps = it.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append(f"{where}: depends_on must be a list")
            deps = []
        installers.append(Installer(
            name=iname, image=image, registry_server=reg, repo=repo, image_ver=image_ver, image_source=source,
            image_tar_path=tar_path, image_tar_md5sum=tar_md5, scripts_ver=sver, scripts_path=spath,
            datafile_path=dpath, datafile_ver=dver, datafile_md5sum=dmd5, datafile_size_bytes=dsize,
            build_no=_s(it.get("build_no")), depends_on=[_s(d) for d in deps],
            self_update=bool(it.get("self", False)),
        ))
    names = [i.name for i in installers]
    for i in installers:
        for d in i.depends_on:
            if d not in names:
                errors.append(f"installer '{i.name}': depends_on '{d}' is not in this manifest")
            elif names.index(d) > names.index(i.name):
                warnings.append(f"installer '{i.name}' depends on '{d}' which is listed later; start order follows the list")
    if "postgres-database" in names and names.index("postgres-database") != 0:
        warnings.append("postgres-database is not first; dependent pods may start before the database")
    if errors:
        return None, errors, warnings
    return Manifest(software_name=name, software_version=ver, description=_s(data.get("description")),
                    manifest_schema=int(schema), installers=installers, sha256=sha), errors, warnings


def diff(old: Manifest, new: Manifest, old_digests: dict[str, str], new_digests: dict[str, str]) -> dict[str, list[str]]:
    """Which pods change between releases (image_ver, scripts_ver, datafile_ver or digest)."""
    o = {i.name: i for i in old.installers}
    n = {i.name: i for i in new.installers}
    changed, same = [], []
    for name, ni in n.items():
        oi = o.get(name)
        if oi is None:
            continue
        if (oi.image_ver, oi.scripts_ver, oi.datafile_ver, oi.image) != (ni.image_ver, ni.scripts_ver, ni.datafile_ver, ni.image) \
                or old_digests.get(name, "") != new_digests.get(name, ""):
            changed.append(name)
        else:
            same.append(name)
    return {"changed": changed, "unchanged": same,
            "added": [x for x in n if x not in o], "removed": [x for x in o if x not in n]}
