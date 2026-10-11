# Software Manager – User Guide

How to build service images, put a release in the release path, upload it in Software Manager,
build the prerequisites package, install a branch (online or offline) and install / start / stop
the platform. Hosts are any Ubuntu/Linux machine with Docker; nothing here depends on a particular
machine. Values in `<angle brackets>` are yours to fill in.

Related: `plan.md` (design and code map), `test.md` (how it is tested).

---

## 1. The pieces

| Piece | Where | Port | What it does |
|---|---|---|---|
| Service folders | `<services root>/<service>/` | – | each service: `build/` + `src/`, own image, own release |
| Docker registry | `docker-registry` | 5000 | holds the built images (`<registry>/<service>:<version>`) |
| Release path | `SM_RELEASE_SOURCE` (e.g. `/mnt/releases`) | – | engineers drop releases here; **Software Manager only reads it** |
| Software Manager | `software-manager` | 2999 | web UI + API: Release Uploader, store, prerequisites packages, nodes, deployments |
| sm-agent | every node | 2997 | runs the pods (docker compose) on that node |
| sm-logger | manager node | 2998 | collects logs from agents |

Software Manager keeps its own data under `$HOME/AlarmCenter/software-manager/data/`
(each path can be changed in `src/env.defaults` / the env file):

| Folder | Env | Contents |
|---|---|---|
| `state/` | `SM_STATE_DIR` | SQLite DB, initial admin password, secrets |
| `releases/<version>/` | `SM_RELEASE_STORE` | stored releases: `manifest.yml`, `resolved.json`, `<service>/image.tar`, `<service>/scripts.tar` |
| `bundles/` | `SM_BUNDLE_DIR` | prerequisites packages built from the UI |
| `import/` | `SM_IMPORT_DIR` | releases handed over by `prereq.sh`; imported at start-up |

---

## 2. Build the Docker images of the services (build machine)

Every service has its own `build/build.sh` with a menu (or pass the option number):

| Option | Does |
|---|---|
| 1 | build the Docker image |
| 2 | build + save `build/release/<service>/<service>.tar` + `install.sh` |
| 3 | clear the release folder |
| 4 | 3 + 2 (+ 6 if the service has `build/sm/`) |
| 5 | push the image to `${REGISTRY}` |
| 6 | build the Software Manager scripts tar `build/release/<service>/<service>_scripts.tar` |

The version comes from `build/buildConfig.yml` (`version:`). Bump it there before a new release.

One service:

```bash
cd <services root>/alarm/build
./build.sh 4                                 # image + release + scripts tar
REGISTRY=<registry-host>:5000 ./build.sh 5   # push
```

All services (helper, runs each service's own `build.sh` in turn):

```bash
cd <services root>/platform/scripts
./build-all-services.sh 4                    # build everything
./build-all-services.sh 4 ui cds             # only some
REGISTRY=<registry-host>:5000 ./build-all-services.sh 5    # push all images
./build-all-services.sh 6                    # scripts tars
```

Log in to the registry first if it needs auth: `docker login <registry-host>:5000`.
Check what is in the registry: `platform/scripts/reg-catalog.sh`.

### Scripts tar (what option 6 packs)

Per service, `build/sm/` holds:

- `compose_template.yml` (or `composetemplate.yml`) – exactly one service; its key or
  `container_name` must be `{{container_name}}`. Placeholders such as `{{image}}`, `{{service_port}}`,
  `{{data_dir}}` are filled in by the agent at install time.
- `pre_start.sh` (or `prestart.sh`) – optional; runs in the pod data folder before start, `$1` is
  the container name (creates env files, folders…).

---

## 3. Put a release in the release path

The release path is a plain folder (local disk or a share mounted on the manager host at
`SM_RELEASE_SOURCE`). One folder per release version:

```
<release path>/
  V1.0.0/
    upload_releaseinfo_YELLOW_METAL_PLATFORM.yml
    alarm/alarm_scripts.tar
    cds/cds_scripts.tar
    ...
  pre_requisite_installer/
    1.0.0/sm_pre_requisites.tar
```

### 3.1 The release YML

Generate a draft from the build configs, then check it:

```bash
cd <services root>/platform/scripts
REGISTRY=<registry-host>:5000 SOFTWARE_VERSION=1.0.0 \
  MANIFEST_OUT=/tmp/upload_releaseinfo_YELLOW_METAL_PLATFORM.yml \
  ./build-all-services.sh manifest
```

Each installer entry looks like:

```yaml
- name: alarm
  image: <registry-host>:5000/alarm
  image_ver: 1.0.0
  datafile_path: none
  datafile_ver: none
  datafile_md5sum: none
  datafile_size_bytes: none
  scripts_ver: 1.0.0
  scripts_path: alarm/alarm_scripts.tar     # relative to the YML's folder
```

### 3.2 Copy the files

```bash
REL=<release path>/V1.0.0
mkdir -p "$REL"
cp /tmp/upload_releaseinfo_YELLOW_METAL_PLATFORM.yml "$REL/"
for s in alarm cds ...; do                      # every service in the YML
  mkdir -p "$REL/$s"
  cp <services root>/<folder of $s>/build/release/$s/${s}_scripts.tar "$REL/$s/"
done
```

Edit `scripts_path` in the YML to `<service>/<service>_scripts.tar` if the draft has absolute paths.

### 3.3 Prerequisites installer

Build the prerequisites tar once per Software Manager version (needs option 2 done for
`sm-agent`, `sm-logger`, `software-manager`):

```bash
python3 <services root>/platform/scripts/make_prerequisites.py 1.0.0 --out <release path>
# -> <release path>/pre_requisite_installer/1.0.0/sm_pre_requisites.tar
```

(`--out` can be left out when `SM_RELEASE_SOURCE` is set; `SERVICES_ROOT` overrides where the
service folders are.)

---

## 4. Open Software Manager and log in

- URL: `https://<manager-host>:2999` (or `http://` when `SM_HTTPS=false`).
- First login: user `admin` (`SM_ADMIN_USER`). The password is `SM_ADMIN_PASSWORD` if it was set
  at install, otherwise it is generated once into `<state dir>/initial-admin-password`
  (`$HOME/AlarmCenter/software-manager/data/state/initial-admin-password`). Change it in **Settings**.
- Roles: **viewer** (read only), **operator** (upload, build packages, install/start/stop),
  **admin** (users, settings, delete). Create users in **Settings**.
- **Settings → Registries → Add / edit registry**: enter the registry host, user, password and CA certificate
  (`docker-registry/data/certs/ca.crt`) once. Needed for uploads that read images from the registry.

---

## 5. Upload a release (Release page → Release Uploader)

1. **Release** page → card **Release Uploader**. It shows the release path (read only) and a
   dropdown of the release folders in it (`V1.0.0`, …).
2. Pick the folder → **Upload**. Software Manager:
   - reads the YML,
   - checks every image in the registry (digest + size) and every scripts tar
     (template, one service, `{{container_name}}`, `docker compose config`) and datafile md5,
   - then **stores** everything in its release store: each image is saved as `image.tar`
     (digest verified), each scripts tar copied.
3. Status goes `scanning → scanned → downloading → downloaded`. Errors show on the release
   (`scan_failed` / `download_failed`) with the reason; fix the release path and upload again.

Alternative: card **Upload a release file (.yml)** – choose or paste a YML (paths in it must be
reachable from the manager).

**Stored releases**: select a version to see services, images, digests, sizes and status.
The release path is never modified.

---

## 6. Build the prerequisites package (for a branch / offline site)

Release page → card **Prerequisites package**:

1. Pick the prerequisites installer version (from `pre_requisite_installer/` in the release path).
2. Tick the stored (downloaded) releases to include.
3. **Build package** → a row appears in the table when ready
   (`sm_pre_requisites_<ver>_<timestamp>.tar`, several GB with all images).
4. **Download** it (or copy it from `SM_BUNDLE_DIR` on the manager host) to a USB disk / the site.
   **Delete** removes it (admin).

The package contains `prereq.sh`, the `sm-agent`, `sm-logger`, `software-manager` images and
`releases/<version>/` with everything needed – no network or registry needed at the site.

---

## 7. Install a branch (site)

On **every node** (as root), unpack the package and run `prereq.sh`:

```bash
mkdir -p /opt/sm-install && tar -xf sm_pre_requisites_<...>.tar -C /opt/sm-install
cd /opt/sm-install
# manager node (also runs software-manager + sm-logger):
sudo bash prereq.sh --manager --branch <branch name> --node-name <node1>
# other nodes:
sudo bash prereq.sh --manager-ip <manager ip> --branch <branch name> --node-name <node2>
```

`prereq.sh --check` only reports. Useful options: `--data-root DIR`, `--gpu`, `--no-firewall`,
`--registry HOST:PORT --registry-ca FILE --registry-user USER` (only for online sites that pull from
a registry). It is safe to run again.

What happens: Docker + compose, log rotation, live-restore, NTP check, firewall, then sm-agent
(and on the manager node sm-logger + software-manager). On the manager node the releases in the
package are copied to `SM_IMPORT_DIR`; Software Manager imports them at start-up, so **all release
versions from the package appear as `downloaded`** without any network. `prereq.sh` prints the
node **token** at the end – keep it for the next step.

---

## 7a. New releases reach the branches automatically (office → branch)

Do this once; afterwards every release you upload at the office shows up at every branch by itself.

**At the office Software Manager** (where you upload releases):

1. **Settings → Release catalog → Turn on publishing (generate branch key)**.
2. Copy the **branch key** shown (it is shown only once; *Generate new branch key* replaces it,
   *Turn off publishing* stops it).
3. The office manager port (2999) must be reachable from the branches (VPN, office firewall rule
   or port forward). Branches only connect *to* the office, never the other way round.

**At each branch Software Manager**:

1. **Settings → Release catalog**: *Office address* `https://<office host or IP>:2999`,
   *Branch key* = the key from the office, optional *Office CA certificate* (the office manager's
   certificate; without it the connection is encrypted but the certificate is not checked) → **Save**.
   The same can be given when installing: `prereq.sh --manager … --office-url https://<office>:2999
   --office-key-file <file with the key>`.
2. The branch asks the office every `SM_CENTRAL_POLL_SEC` (default 5 min; **Check now** on the
   Release page asks immediately). New versions are **downloaded automatically** (images, scripts,
   datafiles, md5-checked) and then appear as `downloaded` in **Stored releases** and in the
   **Deployment** version dropdown. Card **Releases from the office** on the Release page shows
   each version: *available*, *downloading %*, *downloaded*, *failed* (with reason), or *conflict*
   (same version number but different content here – not touched).
3. Installing / updating stays a manual click on the **Deployment** page.

Set `SM_CENTRAL_AUTO_DOWNLOAD=false` to only list them; then use **Download** on each row.
Branches without any connection still use the USB package (sections 6 and 7).

---

## 8. Nodes, deployment, install / start / stop

1. **Nodes → Add Node**: name, IP, agent port (2997), token printed by `prereq.sh`. Repeat for
   each node; it should turn online.
2. **Deployment**: choose the release **version** from the dropdown, map each pod (service) to a
   node, optional per-pod overrides (ports…), save.
3. **Install**: the manager sends each node its pods; the agent loads `image.tar` (offline) or
   pulls from the registry, verifies the digest, renders the compose file, runs `pre_start.sh`
   and starts the container. Progress is in the job list.
4. **Start / Stop / Restart** per pod or for the whole deployment. **Update** to a newer stored
   version keeps a backup and can **roll back**. **Delete** removes the containers (data stays
   under `SM_DATA_ROOT`, default `/opt/yellowmetal`).
5. **Logs**: live and stored logs per node/pod (from sm-logger); download a support bundle
   (secrets masked).

---

## 9. New version later

1. Bump `version` in the changed services' `buildConfig.yml`, build (option 4), push (option 5),
   scripts tar (option 6).
2. New folder `<release path>/V1.0.1/` with the YML and scripts tars (section 3).
3. Release Uploader → pick `V1.0.1` → Upload.
4. Branches connected to the office (section 7a) download `1.0.1` by themselves – just
   Deployment → select `1.0.1` → Update there. Branches without a connection: build a new prerequisites
   package with `1.0.1` ticked, run `prereq.sh --manager` again on the manager node (it only
   hands over the releases and restarts the manager if nothing else changed), then Update.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| Release folder not in dropdown | `SM_RELEASE_SOURCE` and that the folder is mounted into the manager container (read only) |
| `scan_failed`: image not found | image pushed (option 5)? tag = `image_ver`? registry settings in Settings |
| `scan_failed`: scripts tar | template name, exactly one service, `{{container_name}}` |
| Release not shown at site after prereq | manager log: `import` lines; leftover folder in `SM_IMPORT_DIR` = skipped/failed (reason in log) |
| Branch: "the office refused the branch key" | key generated again at the office? enter the new key in Settings → Release catalog |
| Branch: office not reachable | office address/port, VPN / firewall; `curl -k https://<office>:2999/health` from the branch |
| Branch: release *conflict* | that version number exists here with other content; delete it here (if not deployed) and Download |
| Node offline | agent running (`docker ps`), port 2997 open, token matches |
| Pod not starting | Logs page → pod log; `docker logs <container>` on the node |
