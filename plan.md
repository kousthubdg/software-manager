# Software Manager — what is built, and how it works

This explains the Software Manager (the "Docker orchestrator"): what each piece does, the release flow from the
release path to running containers, where every file is stored, and what is still open. It runs on any Linux host
with Docker; nothing depends on the machine it was developed on. No passwords are in this file.

---

## 1. The pieces

Four programs, each its own folder, its own Docker image and its own release package (`build/release/<name>/`).

| Program | Folder | Default port | Runs on | Job |
| --- | --- | --- | --- | --- |
| **Software Manager** | `software-manager` | 2999 (HTTPS) | one node per site (the "manager node") | Web UI + API. Release uploader, release store, prerequisites packages, nodes, deployments, logs, settings. State in SQLite. |
| **sm-agent** | `sm-agent` | 2997 (HTTPS) | **every** node | The Docker work on its node: load the image, unpack scripts, run `pre_start.sh`, render the compose file, `docker compose up / stop / down`, health, restart crashed containers, ship logs. |
| **sm-logger** | `sm-logger` | 2998 | manager node | Logs from all agents, stored per day (gzip); search, tail, download. |
| **docker-registry** | `docker-registry` | 5000 (HTTPS + login) | build / central host | Holds the service images. Needed only where releases are **uploaded**, not at sites. |

Ports, paths, user ids, URLs and the platform are **defaults in `.env`** (section 8).

Same roles as the company system in `SBI_POC_Release_And_Install.md`:

| Company | Ours |
| --- | --- |
| Installer share `/mnt/fileserver/RELEASES/.../V1.0.x/` | **Release path** `SM_RELEASE_SOURCE` (read only) |
| Release Uploader `:8050` | Release page → **Release Uploader** card |
| Fileserver + local registry filled during download | **Release store** `SM_RELEASE_STORE` (scripts **and image tars**) |
| `agv_pre_requisites.tar` | **Prerequisites package**: prerequisites + chosen stored releases, one tar |
| Docker Orchestrator `:8082` | sm-agent (one per node) |
| `deploymentmap.yml` | Deployment page (service → node) |
| `compose_template.yml` + `pre_start.sh` | same (older names `composetemplate.yml` / `prestart.sh` also accepted) |

---

## 2. The whole flow

```text
 ENGINEERS                      SOFTWARE MANAGER (central)                    SITE (may have no network)
 ─────────                      ──────────────────────────                    ──────────────────────────
 build images, push to          Release > Release Uploader
 the registry                     pick V1.0.0 from the release path
                                  > Upload
 put in the RELEASE PATH          1. check: every image tag in the registry,
 (reference, never changed          every scripts tar, every datafile
  by Software Manager):           2. store in the RELEASE STORE:
  V1.0.0/                            scripts tar + image tar (saved from the
    upload_releaseinfo_*.yml         registry) + datafile per service
    <svc>/<svc>_scripts.tar
  pre_requisite_installer/      Release > Prerequisites package
    1.0.0/sm_pre_requisites.tar   pick prerequisites + tick releases
                                  > Build package > Download  ───────────►  tar -xf <package>.tar
                                                                            sudo bash prereq.sh --manager ...
                                                                            (other nodes: --manager-ip ...)
                                                                                 │
                                                                            Software Manager starts with the
                                                                            packaged releases already stored
                                                                                 │
                                                                            Nodes > Add Node
                                                                            Deployment > pick release in dropdown
                                                                            > Install > Start / Stop / Update ...
```

---

## 3. Step by step

### Step A — engineers fill the release path (outside Software Manager)

The release path is the engineers' reference. Software Manager **only reads** it (the container mounts it read only).

```text
<SM_RELEASE_SOURCE>/
  V1.0.0/
    upload_releaseinfo_<name>.yml       release YML (below)
    <service>/<service>_scripts.tar     compose_template.yml + pre_start.sh
    <service>/compose_template.yml      optional unpacked copies, for reading
    <service>/pre_start.sh
  pre_requisite_installer/
    1.0.0/sm_pre_requisites.tar         prereq.sh + sm-agent, sm-logger, software-manager packages
```

Helpers: each service `build/build.sh` option 2 (image + package), 5 (push to `$REGISTRY`), 6 (scripts tar);
`REGISTRY=<host:port> platform/scripts/build-all-services.sh manifest` drafts the YML;
`SERVICES_ROOT=<services folder> python3 platform/scripts/make_prerequisites.py <ver> --out <release path>` makes
the prerequisites tar.

### Step B — the release YML (same shape as the company `upload_releaseinfo_local.yml`)

```yaml
software_name: YELLOW_METAL_PLATFORM
software_version: 1.0.1
description: ...
installers:
- name: alarm                          # service name used on the Deployment page
  image: <registry-host>:<port>/alarm  # must already be in this registry
  image_ver: 1.0.1
  scripts_ver: 1.0.1
  scripts_path: alarm/alarm_scripts.tar   # relative to V<version>/, or absolute, or https URL
  datafile_path: none                  # or a tar of models/config (+ datafile_md5sum, datafile_ver)
```

Also accepted: `image_version` (alias), `image_source: tar` + `image_tar_path`, `depends_on`, `build_no`.

### Step C — Release Uploader: upload = check + store

**Release → Release Uploader**: dropdown of every `V<version>` folder in the release path → **Upload**.
(A YML that is not in the release path can be uploaded as a file in the card below it; its paths must be absolute
or URLs.)

1. **Check** every service: image tag exists in its registry (digest recorded; optional Trivy / cosign); scripts tar
   has one compose template + one pre-start script, LF endings, `bash -n` passes, template renders with known
   placeholders, exactly one service, `docker compose config` passes; datafile md5 / size.
2. **Store** in the release store (`SM_RELEASE_STORE/<version>/`): the scripts tar, the datafile, and the **image
   saved from the registry as a `docker load` tar**. The saved image must have the same digest the check recorded
   (a tag re-pushed in between is refused).
3. Status goes `scanning → downloading → downloaded`. Versions are immutable.

After this the release needs neither the registry nor the release path.

### Step D — prerequisites package (for any site, including offline ones)

**Release → Prerequisites package**: choose a prerequisites tar from the release path, tick the stored releases to
include, **Build package**, then **Download**. The package is one tar:

```text
sm_pre_requisites/
  prereq.sh
  sm-agent/  sm-logger/  software-manager/      <m>.tar + install.sh
  releases/<version>/release.json               release record (YML, check results, digests)
  releases/<version>/files/...                  the stored files (scripts, image tars, datafiles)
```

Packages are kept in `SM_BUNDLE_DIR` (list, download, delete on the same card).

### Step E — at the site

```text
tar -xf <package>.tar && cd sm_pre_requisites
manager node:  sudo bash prereq.sh --manager --branch <branch> --node-name <name>
other nodes:   sudo bash prereq.sh --manager-ip <manager-ip> --node-name <name>
```

`prereq.sh` sets up Docker settings (live-restore, log rotation), NTP check, firewall ports, optional NVIDIA
toolkit (`--gpu`), the pod data folder, then installs sm-agent (manager node also sm-logger + Software Manager) and
prints the **node token** and the first admin password. On the manager node it copies `releases/` into
`SM_IMPORT_DIR`; when Software Manager starts it registers those releases as **downloaded** (and removes them from
the import folder). Safe to run again; `--check` only reports.

Note: installing Docker itself needs the OS package repositories (or Docker already installed). Everything after that
works without network.

### Step E2 — connected branches get new releases from the office by themselves

Office: **Settings → Release catalog → Turn on publishing** gives a branch key (only its sha256 is kept). Branch:
**Settings → Release catalog** = office address + branch key (+ optional office CA), or `prereq.sh --office-url
--office-key-file`. The branch polls `GET /api/catalog` every `SM_CENTRAL_POLL_SEC`; each new version is downloaded
(`/api/catalog/<ver>/files`, `/file?path=`) into a hidden folder in `SM_IMPORT_DIR`, size + md5 checked against the
office's `resolved.json`, then registered by the same import as a USB package → `downloaded` in every dropdown.
Same version with different content = *conflict* (left alone). Install / Update stays a click on the Deployment page.
Only the branch connects (to the office port), so branches behind NAT work. The USB package (Step D) stays for
branches with no link.

### Step F — nodes

**Nodes → Add Node**: name, IP, agent port, node token. The manager talks to each agent over HTTPS with that token;
agents accept only the manager IPs they were given.

### Step G — deployment: install / start / stop

**Deployment → Add Deployment Map**: pick the release in the dropdown, choose a node for each service.

| Action | What happens |
| --- | --- |
| **Install** | per service, in order (database first, `depends_on`): the agent downloads the scripts tar and the **image tar from the manager**, `docker load`s it, runs `pre_start.sh`, renders `docker-compose.yml`, starts it, waits for healthy. |
| **Start / Stop / Restart** | whole deployment or one service. |
| **Update** | only changed services; database backed up first (`pg_dump`); a service that does not get healthy is rolled back. |
| **Rollback / Delete / Backups / Restore** | as named; every action is a job with per-node steps, resumable. |

Per service on its node:

```text
<SM_DATA_ROOT>/<service>/
  scripts/<scripts_ver>/compose_template.yml, pre_start.sh
  compose/<ver>-<scripts_ver>.yml + docker-compose.yml
  data/  logs/  secrets/  values.env
```

`pre_start.sh` runs in the service data folder with the container name as `$1` (like the company scripts) and env:
`CONTAINER_NAME DATA_FILE_PATH REGISTRY_SERVER IMAGE VERSION RUN_UID RUN_GID SERVICE_PORT NODE_IP ALARM_BASE_PATH
HOST_TZ` + `SMV_<KEY>` values from the UI. Template placeholders: `{{container_name}} {{registry_server}} {{image}}
{{version}} {{data_file_path}} {{run_uid}} {{run_gid}} {{alarm_base_path}} {{node_ip}} {{service_port}}
{{mem_limit}} {{cpus}} {{devices}}`.

### Step H — day to day

| Page | What it gives |
| --- | --- |
| **Release** | Release Uploader, upload a YML file, stored releases (details, check again, delete), prerequisites packages |
| **Nodes** | health, per-service logs / inspect / values, prune old images |
| **Deployment** | maps, install / start / stop / update / rollback / delete, jobs, backups |
| **Logs** | search, live tail (viewer reads; operator / admin download), support bundle with secrets masked |
| **Settings** | registries (host, login, CA), users and roles, audit, alerts, self-update |

Self-healing (agent): restarts stopped / unhealthy containers, crash-loop backoff + alert (MQTT if enabled),
reconcile every `RECONCILE_SEC`.

---

## 4. Where things are stored

| What | Setting | Default |
| --- | --- | --- |
| Release path (engineers, read only) | `SM_RELEASE_SOURCE` | `/mnt/releases` |
| Release store (scripts, image tars, datafiles per version) | `SM_RELEASE_STORE` | `$HOME/AlarmCenter/software-manager/data/releases/<version>/` |
| Prerequisites packages | `SM_BUNDLE_DIR` | `$HOME/AlarmCenter/software-manager/data/bundles/` |
| Import folder (prereq.sh hands releases over) | `SM_IMPORT_DIR` | `$HOME/AlarmCenter/software-manager/data/import/` |
| State: SQLite (releases, nodes, deployments, jobs, users, audit), keys | `SM_STATE_DIR` | `$HOME/AlarmCenter/software-manager/data/state/` |
| Database backups | `SM_BACKUP_DIR` | `$HOME/AlarmCenter/software-manager/data/backups/` |
| Service data on each node | `SM_DATA_ROOT` | `/opt/yellowmetal/<service>/` |
| Agent state + node token | — | `$HOME/AlarmCenter/sm-agent/data/` |

Release store layout per version: `manifest.yml`, `resolved.json` (digests, md5s, ports, health, wiring),
`<service>/scripts.tar`, `<service>/image.tar`, `<service>/datafile/<name>`.

---

## 5. Code map

| File | Does |
| --- | --- |
| `software-manager/src/app/manifest.py` | parse + validate the YML |
| `software-manager/src/app/releases.py` | release path list, upload, check, store, prerequisites packages, import |
| `software-manager/src/app/registry.py` | registry login / CA, digests, image size, **save image as tar** |
| `software-manager/src/app/deploy.py` | install / start / stop / update / rollback / delete jobs, backups, self-update |
| `software-manager/src/app/main.py` | REST API + React UI |
| `software-manager/src/frontend/src/pages/Release.jsx` | Release Uploader, upload file, stored releases, prerequisites packages |
| `sm-agent/src/app/pods.py` | per-service work on a node |
| `sm-agent/build/prereq.sh` | node prerequisites + hand-over of packaged releases |
| `platform/scripts/make_prerequisites.py` | builds `sm_pre_requisites.tar` |
| `platform/packages/common/gen_release_builds.py` | generates every service's build files and `.env` defaults |

API added for this flow: `GET /api/release-source` (release path + prerequisites tars),
`POST /api/release-source/{folder}/import`, `GET|POST /api/bundles`, `GET|DELETE /api/bundles/{name}`,
`POST /api/releases/import-local`.
Office → branch catalog (`app/central.py`): office `GET /api/catalog`, `GET /api/catalog/{ver}`, `.../files`,
`.../file?path=` (header `X-Catalog-Key`), `POST|DELETE /api/catalog/key` (admin); branch `GET|PUT /api/central`,
`POST /api/central/check`, `POST /api/central/{ver}/download`.

---

## 6. Tests

`software-manager/tests/` (all settings from env, run on any Linux host with Docker):

| Script | Checks |
| --- | --- |
| `company_format_test.py` | company-style scripts tar passes the check; `pre_start.sh` writes `<container>.env` in the data folder |
| `offline-flow.sh <V-folder> <prereq ver>` | release path unchanged; upload stores scripts + image tars; package built; then runs `offline-site.sh` |
| `offline-site.sh <package.tar>` | an empty manager starts with the packaged releases `downloaded`; `docker load` accepts a stored image |
| `registry_save_check.py`, `sm_release_status.py` | save one image from a registry; print a release's status |
| `run-local.sh` | starts a manager from source with throwaway state |

Results so far: see section 9.

---

## 7. Differences from the company system (open choices)

1. **Pods** — their `deploymentfest_*.yml` groups containers into pods per node; ours maps services one by one.
2. **Catalog** — their branch lists releases from a release-info API; ours: the office lists the release path, and
   branches pull the office's stored releases through the release catalog (Step E2).
3. **Two upload targets** (test / deploy) — ours has one.
4. **Install-time prerequisite check** (Docker / Compose / NVIDIA versions) — ours checks in `prereq.sh` only.

---

## 8. Configuration (no hardcoding)

| Setting | Default | Where |
| --- | --- | --- |
| `SERVICE_PORT` | 2999 / 2997 / 2998 | each service `.env` |
| `SM_RELEASE_SOURCE`, `SM_RELEASE_STORE`, `SM_BUNDLE_DIR`, `SM_IMPORT_DIR`, `SM_STATE_DIR`, `SM_BACKUP_DIR` | section 4 | software-manager `.env` |
| `SM_PLATFORM` | `linux/amd64` | image platform saved from multi-arch registries |
| `SM_DATA_ROOT`, `SM_RUN_UID`, `SM_RUN_GID` | `/opt/yellowmetal`, 2759, 2759 | `.env`, `prereq.sh --data-root --run-uid --run-gid` |
| `SM_MIN_FREE_GB`, `SM_BACKUP_KEEP`, `SM_POLL_SEC` | 10, 5, 15 | `.env` |
| `SM_HTTPS`, `SM_TLS_CERT`, `SM_TLS_KEY`, `SM_PUBLIC_URL` | HTTPS, self-signed | `.env` |
| `SM_LOGGER_URL`, `LOGGER_TOKEN`, `SM_MQTT_ALERTS`, `BRANCH_NAME`, `SM_TRIVY`, `SM_COSIGN_PUBKEY` | — | `.env` / `prereq.sh` |
| Registries (host, user, password, CA) | none | Settings page (password encrypted) |
| `SM_CENTRAL_URL`, `SM_CENTRAL_KEY` / `SM_CENTRAL_KEY_FILE`, `SM_CENTRAL_POLL_SEC`, `SM_CENTRAL_AUTO_DOWNLOAD` | none, none, 300, true | `.env` / `prereq.sh`; the Settings page overrides URL + key |
| `REGISTRY` for builds / YML draft | none — required | env |
| Agent `MANAGER_IPS`, `NODE_NAME`, `RECONCILE_SEC`, `UNHEALTHY_GRACE_SEC`, `LOG_BUFFER_MB` | from prereq | sm-agent `.env` |

`127.0.0.1` is used only for a service checking itself on its own machine.

---

## 9. Status

| Item | Status |
| --- | --- |
| Upload from release path → check → store scripts + image tars; release path unchanged | **passed** (16 services stored in 73 s from a local registry; release path files identical before and after) |
| Prerequisites package → empty manager starts with the release stored | **passed** (package 3.96 GB; site manager shows YELLOW_METAL_PLATFORM 1.0.0 `downloaded`; import folder emptied; stored image `docker load`s) |
| Install / start / stop from a stored (image-tar) release on a real node | not yet run |
| Earlier (registry-pull releases): two agents, install, update + auto-rollback, stop / start / delete, self-update, backup + restore, alerts, logs | passed |
| Older lab helper scripts in `platform/scripts` (`*-wsl.sh`, `rebuild-push.sh`, `test-scripts-tar.sh`) | still use fixed lab paths; to be made env-driven or removed |
