"""Health poller: node reachability, pod state per node, crash-loop / unhealthy / offline / clock-skew alerts.

Alerts are kept in SQLite (shown in the UI) and published to the deployment's MQTT pod when one is running.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from typing import Optional

from . import agents, db
from .config import settings
from .deploy import placement
from .security import decrypt

log = logging.getLogger("software-manager.health")
OFFLINE_AFTER = settings.sm_offline_after_sec
_stop = threading.Event()
_tick = 0


def raise_alert(key: str, message: str) -> None:
    now = time.time()
    r = db.one("SELECT * FROM alerts WHERE key=?", key)
    if r and r["active"]:
        db.ex("UPDATE alerts SET last_ts=?, message=? WHERE key=?", now, message, key)
        return
    db.ex("INSERT INTO alerts(key,first_ts,last_ts,message,active) VALUES(?,?,?,?,1) "
          "ON CONFLICT(key) DO UPDATE SET first_ts=excluded.first_ts, last_ts=excluded.last_ts,"
          " message=excluded.message, active=1", key, now, now, message)
    log.warning("ALERT %s: %s", key, message)
    publish({"event": "raised", "key": key, "message": message, "ts": now})


def clear_alert(key: str) -> None:
    r = db.one("SELECT active, message FROM alerts WHERE key=?", key)
    if r and r["active"]:
        db.ex("UPDATE alerts SET active=0, last_ts=? WHERE key=?", time.time(), key)
        log.info("cleared %s", key)
        publish({"event": "cleared", "key": key, "message": r["message"], "ts": time.time()})


def _mqtt_target() -> Optional[tuple[str, int]]:
    for d in db.q("SELECT * FROM deployments WHERE state IN ('running','partial','updating')"):
        mp = json.loads(d["map_json"])
        nid = placement(mp).get("mqtt")
        if not nid:
            continue
        n = db.one("SELECT ip FROM nodes WHERE id=?", nid)
        res = json.loads((db.one("SELECT resolved_json FROM releases WHERE version=?", d["version"]) or
                          {"resolved_json": "{}"})["resolved_json"])
        ov = (mp.get("overrides") or {}).get("mqtt") or {}
        port = ov.get("service_port") or (res.get("mqtt") or {}).get("port") or settings.sm_mqtt_default_port
        if n:
            return n["ip"], int(port)
    return None


def publish(payload: dict) -> None:
    if not settings.sm_mqtt_alerts:
        return
    tgt = _mqtt_target()
    if not tgt:
        return
    try:
        import paho.mqtt.publish as mqtt_publish
        user = db.kv_get("mqtt_alert_user", "")
        pw_enc = db.kv_get("mqtt_alert_password_enc", "")
        auth = {"username": user, "password": decrypt(pw_enc) if pw_enc else ""} if user else None
        topic = f"yellowmetal/{settings.branch_name or 'branch'}/software-manager/alerts"
        mqtt_publish.single(topic, json.dumps({"branch": settings.branch_name, **payload}), qos=1,
                            hostname=tgt[0], port=tgt[1], auth=auth,
                            client_id=f"software-manager-alerts-{secrets.token_hex(4)}",
                            keepalive=10)
    except Exception as exc:
        log.info("mqtt alert publish failed: %s", exc)


def poll_node(n: dict, with_info: bool) -> None:
    try:
        _poll_node(n, with_info)
    except Exception as exc:  # a malformed agent reply must not kill the poll thread
        log.exception("poll of node %s failed", n.get("id"))
        db.ex("UPDATE nodes SET last_error=? WHERE id=?", f"{type(exc).__name__}: {exc}"[:500], n["id"])


def _poll_node(n: dict, with_info: bool) -> None:
    now = time.time()
    try:
        pods = agents.call(n, "GET", "/api/pods", timeout=15)
        info = agents.call(n, "GET", "/api/info", timeout=30) if with_info else None
        if not isinstance(pods, dict) or (info is not None and not isinstance(info, dict)):
            raise agents.AgentError(f"{n['name']}: unexpected reply from agent")
    except agents.AgentError as exc:
        db.ex("UPDATE nodes SET status=CASE WHEN ?-last_seen > ? THEN 'offline' ELSE status END, last_error=? WHERE id=?",
              now, OFFLINE_AFTER, str(exc), n["id"])
        if now - (n["last_seen"] or 0) > OFFLINE_AFTER and n["last_seen"]:
            raise_alert(f"node:{n['id']}", f"node {n['name']} ({n['ip']}) unreachable: {exc}")
        return
    except Exception as exc:
        db.ex("UPDATE nodes SET last_error=? WHERE id=?", str(exc), n["id"])
        return
    db.ex("UPDATE nodes SET status='online', last_seen=?, last_error='', pods_json=? WHERE id=?",
          now, json.dumps(pods.get("pods", [])), n["id"])
    clear_alert(f"node:{n['id']}")
    if info:
        db.ex("UPDATE nodes SET info_json=? WHERE id=?", json.dumps(info), n["id"])
        skew = abs(info.get("time", now) - now)
        if skew > settings.sm_clock_skew_sec:
            raise_alert(f"skew:{n['id']}", f"node {n['name']} clock differs by {skew:.1f}s (check NTP)")
        else:
            clear_alert(f"skew:{n['id']}")
        if info.get("disk_free_gb", 99) < settings.sm_min_free_gb:
            raise_alert(f"disk:{n['id']}", f"node {n['name']} has only {info['disk_free_gb']} GB free")
        else:
            clear_alert(f"disk:{n['id']}")
    for p in pods.get("pods", []):
        key = f"pod:{n['id']}:{p['name']}"
        if p.get("crashloop"):
            raise_alert(key, f"{p['name']} on {n['name']} is crash-looping (restarts {p.get('restart_count')}, "
                             f"exit {p.get('exit_code')}{', OOM killed' if p.get('oom_killed') else ''})")
        elif p.get("desired") == "running" and (p.get("state") != "running" or p.get("health") == "unhealthy"):
            raise_alert(key, f"{p['name']} on {n['name']} is {p.get('state')}/{p.get('health')} "
                             f"{(p.get('health_output') or '')[:200]}")
        else:
            clear_alert(key)


def poll_once() -> None:
    global _tick
    _tick += 1
    with_info = _tick % 20 == 1
    threads = [threading.Thread(target=poll_node, args=(n, with_info), daemon=True) for n in db.q("SELECT * FROM nodes")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)


def _loop() -> None:
    while not _stop.is_set():
        try:
            poll_once()
        except Exception:
            log.exception("health poll failed")
        _stop.wait(max(5.0, settings.sm_poll_sec))


def start_thread() -> None:
    threading.Thread(target=_loop, daemon=True, name="health-poller").start()
