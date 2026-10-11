# Context for platform debugging (shared by debug agents)

Lab access (lab only, never write it into code/docs): `wsl -d Ubuntu-26.04 -u root -- <cmd>`.
Files written from Windows must be converted to LF before running in the lab (`sed -i 's/\r$//' file`).
Prefer script files over complex inline quoting in `wsl bash -c`. Run long commands in the background and poll a log.

## What runs

Software Manager (:2999) deployed release `1.0.0` (YELLOW_METAL_PLATFORM) as deployment `branch` on node `node-a`
(all 16 pods on one host, `--network host`). Overrides: postgres-database on **55432**, mqtt on **11883** (the
Windows host already uses 5432/1883). Other ports are the standard ones (SKILL.md section 22 table):
configuration 8001, cds 8002, crowding-detection 8003, thumbnail 8004, alarm 8005, scheduler 8006,
face-recognition 8013, jewelry-detection 8014, media-ws 8020, overlay-ws 8021, control-ws 8023, stream-ingest 8024,
api-gateway 8080, ui 3000. Host IP for the services: `SYS_IP` in any values.env.

Per pod on the host: `/opt/yellowmetal/<pod>/`
- `values.env` (managed wiring: DB_IP/DB_PORT, MQTT_HOST/PORT, service URLs; secrets are `*_FILE` under `secrets/`)
- `compose/<ver>-<ver>.yml` (rendered compose), `data/`, `logs/`, `scripts/`
- container name = pod name; image `localhost:5000/<pod>:1.0.0`

Architecture and rules: `C:\platform\skills\video-analytics-expert\SKILL.md` section 22 (data flow, ports, crowding
rules), `C:\platform\SHARED_CONTEXT.md`, `C:\platform\docs\*.md` (architecture, WS_PLANES, STREAM_INGEST,
mqtt-topics). Camera credentials only in `C:\cds\src\.env` (never print or copy them).

## Hot-fix loop (testing only)

1. Fix code in `C:\<service folder>\src` (folder names: see `C:\platform\scripts\build-all-services.sh` FOLDER_OF).
2. Build: `cd /mnt/c/<folder>/build && sed -i 's/\r$//' build.sh && bash build.sh 1` → image `<svc>:<version>`.
3. `docker tag <svc>:1.0.0 localhost:5000/<svc>:1.0.0`
4. Recreate only that pod: project = `docker inspect <pod> --format '{{index .Config.Labels "com.docker.compose.project"}}'`,
   file = label `com.docker.compose.project.config_files`; then
   `docker compose -p <project> -f <file> --env-file /dev/null up -d --force-recreate --no-deps` (check how the agent
   runs it in `C:\sm-agent\src\app\pods.py` and do the same).
5. Verify health + behaviour. Do NOT push to the registry and do NOT bump versions; the coordinator cuts release
   1.0.1 at the end and updates through Software Manager. List every changed file in your report.

## Rules

- Never touch: software-manager, sm-agent, sm-logger, docker-registry containers or their ports (2999/2997/2998/5000),
  the release path `/mnt/releases` (read only), other agents' services (only recreate the pods you own).
- No hardcodes (IPs, RTSP URLs, credentials, thresholds): env + Postgres only. `DEVICE=cpu`, `YOLO_MODEL=yolo11n.pt`.
- One Postgres, one schema per service; services talk over MQTT / HTTP / WS only.
- Never write 'WSL' or lab paths into code or docs.
