from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    service_port: int = 2999
    host: str = "0.0.0.0"
    module_name: str = "software-manager"
    sys_ip: str = "127.0.0.1"
    sm_state_dir: str = ""
    sm_release_store: str = ""
    sm_backup_dir: str = ""
    sm_admin_user: str = "admin"
    sm_admin_password: str = ""
    sm_secret_key: str = ""
    sm_https: bool = True
    sm_tls_cert: str = ""
    sm_tls_key: str = ""
    sm_public_url: str = ""
    sm_data_root: str = "/opt/yellowmetal"
    sm_run_uid: str = "2759"
    sm_run_gid: str = "2759"
    sm_logger_url: str = "http://127.0.0.1:2998"
    logger_token: str = ""
    sm_min_free_gb: float = 10.0
    sm_release_source: str = "/mnt/releases"
    sm_bundle_dir: str = ""
    sm_import_dir: str = ""
    sm_platform: str = "linux/amd64"
    sm_backup_keep: int = 5
    sm_trivy: bool = False
    sm_cosign_pubkey: str = ""
    sm_mqtt_alerts: bool = True
    sm_poll_sec: float = 15.0
    sm_session_hours: float = 12.0
    sm_offline_after_sec: float = 60.0
    sm_clock_skew_sec: float = 2.0
    sm_agent_port: int = 2997
    sm_agent_tls_verify: bool = False
    sm_agent_ca_file: str = ""
    sm_agent_http_fallback: bool = False
    sm_mqtt_default_port: int = 1883
    sm_heavy_pods: str = "crowding-detection,jewelry-detection,face-recognition,postgres-database"
    sm_analytics_pods: str = "crowding-detection,jewelry-detection"
    sm_restore_keep_running: str = "mqtt"
    sm_heavy_wait_sec: float = 600.0
    sm_pod_wait_sec: float = 300.0
    sm_self_update_wait_sec: float = 600.0
    sm_capacity_budget: float = 0.7
    sm_central_url: str = ""
    sm_central_key: str = ""
    sm_central_key_file: str = ""
    sm_central_poll_sec: float = 300.0
    sm_central_auto_download: bool = True
    branch_name: str = ""
    log_level: str = "INFO"


settings = Settings()

AC = Path(os.getenv("AC_DIR") or Path(os.getenv("HOME", "/root")) / "AlarmCenter" / settings.module_name)
STATE = Path(settings.sm_state_dir or AC / "data" / "state")
RELEASES = Path(settings.sm_release_store or AC / "data" / "releases")
BACKUPS = Path(settings.sm_backup_dir or AC / "data" / "backups")
BUNDLES = Path(settings.sm_bundle_dir or AC / "data" / "bundles")
IMPORTS = Path(settings.sm_import_dir or AC / "data" / "import")
for _p in (STATE, RELEASES, BACKUPS, BUNDLES, IMPORTS):
    _p.mkdir(parents=True, exist_ok=True)
STATIC = Path(os.getenv("SM_STATIC_DIR") or Path(__file__).resolve().parent.parent / "static")


def public_url() -> str:
    """How agents on other nodes reach this manager (file downloads)."""
    if settings.sm_public_url:
        return settings.sm_public_url.rstrip("/")
    scheme = "https" if settings.sm_https else "http"
    return f"{scheme}://{settings.sys_ip}:{settings.service_port}"
