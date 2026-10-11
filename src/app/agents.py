"""HTTP(S) client for sm-agent on each node (node token auth).

Agents serve HTTPS with a self-signed certificate. TLS verification follows SM_AGENT_CA_FILE (CA bundle / the
agents' certificates) or SM_AGENT_TLS_VERIFY (system CAs); plain-http fallback only with SM_AGENT_HTTP_FALLBACK.
"""
from __future__ import annotations

from typing import Any, Optional, Union

import httpx

from . import db
from .config import settings
from .security import decrypt


class AgentError(RuntimeError):
    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status

    @property
    def transient(self) -> bool:
        """Unreachable / TLS / 5xx: worth retrying (e.g. while the agent is being replaced)."""
        return self.status is None or self.status >= 500


def _verify() -> Union[bool, str]:
    if settings.sm_agent_ca_file:
        return settings.sm_agent_ca_file
    return settings.sm_agent_tls_verify


def _schemes() -> tuple[str, ...]:
    return ("https", "http") if settings.sm_agent_http_fallback else ("https",)


def base_url(node: dict) -> str:
    scheme = node.get("scheme") or "https"
    return f"{scheme}://{node['ip']}:{node['port']}"


def node(node_id: int) -> dict:
    n = db.one("SELECT * FROM nodes WHERE id=?", node_id)
    if not n:
        raise AgentError(f"node {node_id} not found", 404)
    return n


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
        return d if isinstance(d, str) else str(d)
    except Exception:
        return r.text[:500]


def _tls_error(exc: Exception) -> bool:
    return "SSL" in str(exc).upper() or "CERTIFICATE" in str(exc).upper()


def call(n: dict, method: str, path: str, json: Any = None, timeout: float = 60, token: Optional[str] = None,
         raw: bool = False) -> Any:
    tok = token if token is not None else decrypt(n["token_enc"])
    last = ""
    for scheme in _schemes():
        url = f"{scheme}://{n['ip']}:{n['port']}{path}"
        try:
            r = httpx.request(method, url, json=json, timeout=timeout, verify=_verify(),
                              headers={"Authorization": f"Bearer {tok}"})
        except httpx.ConnectError as exc:
            last = f"{n['ip']}:{n['port']} unreachable ({exc})"
            continue
        except httpx.HTTPError as exc:
            if scheme == "https" and _tls_error(exc):
                last = f"{n['ip']}:{n['port']} TLS error ({exc})"
                continue
            raise AgentError(f"{n['ip']}: {exc}")
        if r.status_code >= 400:
            raise AgentError(f"{n['name']} ({n['ip']}): {_detail(r)} [HTTP {r.status_code}]", r.status_code)
        if raw:
            return r
        ct = r.headers.get("content-type", "")
        if "json" not in ct:
            return r.text
        try:
            return r.json()
        except ValueError:
            raise AgentError(f"{n['name']} ({n['ip']}): invalid JSON reply for {path}", 502)
    raise AgentError(last or f"{n['ip']}:{n['port']} unreachable")


def stream(n: dict, path: str, dest, timeout: float = 3600) -> int:
    """Stream a GET response (e.g. pg_dump) into an open binary file; returns bytes written."""
    tok = decrypt(n["token_enc"])
    last = ""
    for scheme in _schemes():
        url = f"{scheme}://{n['ip']}:{n['port']}{path}"
        total = 0
        try:
            with httpx.stream("GET", url, timeout=httpx.Timeout(30, read=timeout), verify=_verify(),
                              headers={"Authorization": f"Bearer {tok}"}) as r:
                if r.status_code >= 400:
                    r.read()
                    raise AgentError(f"{n['name']}: {_detail(r)} [HTTP {r.status_code}]", r.status_code)
                for chunk in r.iter_bytes(1 << 20):
                    dest.write(chunk)
                    total += len(chunk)
            return total
        except httpx.ConnectError as exc:
            last = f"{n['ip']}:{n['port']} unreachable ({exc})"
            continue
        except httpx.HTTPError as exc:
            if scheme == "https" and total == 0 and _tls_error(exc):
                last = f"{n['ip']}:{n['port']} TLS error ({exc})"
                continue
            raise AgentError(f"{n['name']}: {exc}")
    raise AgentError(last or f"{n['ip']}:{n['port']} unreachable")


def health(n: dict, timeout: float = 5) -> bool:
    for scheme in _schemes():
        try:
            r = httpx.get(f"{scheme}://{n['ip']}:{n['port']}/health", timeout=timeout, verify=_verify())
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            continue
    return False
