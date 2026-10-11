"""Entry point: HTTPS by default (self-signed certificate generated once under the state dir), then uvicorn."""
from __future__ import annotations

import ipaddress
import logging
import subprocess

import uvicorn

from .config import STATE, settings

log = logging.getLogger("software-manager.serve")


def ensure_cert() -> tuple[str, str]:
    if settings.sm_tls_cert and settings.sm_tls_key:
        return settings.sm_tls_cert, settings.sm_tls_key
    d = STATE / "tls"
    d.mkdir(parents=True, exist_ok=True)
    crt, key = d / "server.crt", d / "server.key"
    if not (crt.exists() and key.exists()):
        try:
            ipaddress.ip_address(settings.sys_ip)
            own = f"IP:{settings.sys_ip}"
        except ValueError:
            own = f"DNS:{settings.sys_ip}"
        san = f"subjectAltName={own},IP:127.0.0.1,DNS:localhost"
        p = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
                            "-subj", "/CN=software-manager", "-addext", san, "-keyout", str(key), "-out", str(crt)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            crt.unlink(missing_ok=True)
            key.unlink(missing_ok=True)
            raise SystemExit(f"self-signed certificate generation failed: {p.stderr.strip()[-500:]}")
        key.chmod(0o600)
    return str(crt), str(key)


def main() -> None:
    kw: dict = {}
    if settings.sm_https:
        kw["ssl_certfile"], kw["ssl_keyfile"] = ensure_cert()
    uvicorn.run("app.main:app", host=settings.host, port=settings.service_port, log_level=settings.log_level.lower(),
                proxy_headers=False, **kw)


if __name__ == "__main__":
    main()
