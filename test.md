# Software Manager — test runbook

How to test Software Manager, sm-agent and the release flow. Read `plan.md` first for how the system works.
Every test takes its paths, ports and registry from environment variables; nothing below is tied to one machine.

---

## 1. Rules for every test run

1. **Never touch what is live.** Do not stop, remove or rebuild: the installed Software Manager (port 2999), sm-agent
   (2997), sm-logger (2998), the registry (5000), or containers of a running deployment. Check first:
   `docker ps --format '{{.Names}} {{.Status}}'`.
2. **Own ports and folders per test.** Test managers run from source with `tests/run-local.sh` on their own port
   (`SM_TEST_PORT`) and state folder (`SM_TEST_ROOT`); test agents on their own port and `SM_DATA_ROOT`. Port plan:

   | Use | Ports |
   | --- | --- |
   | API / edge-case tests | manager 12991 |
   | install / start / stop tests | manager 12992, agent 12993 |
   | UI tests | manager 12994 |
   | prerequisites / package tests | manager 12995 (+ 12996 for the "site") |
   | offline flow (`offline-flow.sh`) | 12999 + 12997 |

3. **Container names = service names.** Installing a service whose container already exists on this Docker host
   clashes. Install only services that are not running, and override their `service_port` in the deployment map
   (`"overrides": {"<svc>": {"service_port": <free port>}}`).
4. **Release path is read only.** Tests may read `SM_RELEASE_SOURCE`; only `make_prerequisites.py` (the engineer
   step) writes `pre_requisite_installer/` there.
5. **Shell hygiene.** Put multi-line commands in a script file under `tests/`, convert it to LF if written from
   Windows, and never `pkill -f` a pattern that also matches your own command line (use `fuser -k <port>/tcp`).
6. **Clean up** test managers / agents / containers you started (`fuser -k <port>/tcp`, `docker compose -p <svc> down`).
7. Record every bug found in section 6 with the fix and the test that proves it.

---

## 2. Environment

| Variable | Meaning |
| --- | --- |
| `PY` | python with `software-manager/src/requirements.txt` and `sm-agent/src/requirements.txt` installed |
| `SERVICES_ROOT` | folder that holds the service folders (`software-manager/`, `sm-agent/`, ...) |
| `SM_RELEASE_SOURCE` | release path (read only), e.g. holds `V1.0.0/` and `pre_requisite_installer/1.0.0/` |
| `REGISTRY`, `REG_USER`, `REG_PASSWORD_FILE`, `REG_CA` | registry the release images are in, its login and CA file |
| `WORK` | scratch folder for a test (default under `$HOME`) |

Current lab values (one example host, change for another host): `PY=/root/smvenv/bin/python`, `SERVICES_ROOT=/mnt/c`,
`SM_RELEASE_SOURCE=/mnt/releases`, `REGISTRY=localhost:5000`, `REG_USER=ym`,
`REG_PASSWORD_FILE=/root/AlarmCenter/docker-registry/data/auth/initial-password`,
`REG_CA=/root/AlarmCenter/docker-registry/data/certs/ca.crt`. The lab Linux host is reached from the Windows PC with
`wsl -d Ubuntu-26.04 -u root -- <command>`; on a plain Ubuntu host run the commands directly.

---

## 3. Test suites

| # | Suite | How | Pass when |
| --- | --- | --- | --- |
| T1 | Manifest parser | `$PY tests/test_manifest.py` | all cases pass |
| T2 | Scripts tar formats | `$PY tests/company_format_test.py` | company + own formats accepted, broken tar rejected, `pre_start.sh` writes `<container>.env` |
| T3 | Registry image save | `SM_STATE_DIR=<state with registry saved> $PY tests/registry_save_check.py <host> <repo> <tag> <dest>` then `docker load -i <dest>` | digest equals the registry tag digest; image loads |
| T4 | API edge cases | `tests/api_edge_test.py` (manager 12991) | see 4.1 |
| T5 | Offline flow | `tests/offline-flow.sh V1.0.0 1.0.0` | release path unchanged; 16/16 stored; package built; site manager shows the release `downloaded`; image loads |
| T6 | Install / start / stop from a stored release | `tests/install_flow_test.py` (manager 12992, agent 12993) | see 4.2 |
| T7 | UI | browser on manager 12994 | see 4.3 |
| T8 | Prerequisites + package hand-over | `tests/prereq_handoff_test.sh` | see 4.4 |
| T9 | Earlier full e2e (registry-pull releases, two agents) | `tests/e2e.py` via its launcher | ALL PASSED |
| T10 | Office → branch release catalog | `tests/central-test.sh V1.0.0` (office 12991, branch 12992; same env as `ui-setup.sh`); `tests/central_relink.py` re-links them for UI checks | see 4.5 |

---

## 4. What each suite must cover

### 4.1 API edge cases (T4)
- upload YML file with absolute paths → `downloaded`; same version again → refused (immutable)
- same version, different `software_name` → refused with a clear message
- image tag missing in the registry → `scan_failed`, error names the service
- scripts tar missing / broken (no `pre_start.sh`, CRLF script, bad template placeholder) → `scan_failed` with reason
- release path folder without a YML, with a bad YML → listed with an error, Upload refused
- bundle API: unknown prerequisites path, `../` traversal, release not downloaded → 422; bad bundle name → 404
- `POST /api/releases/import-local` with: a valid folder, the same release again (removed, "already here"),
  a clashing version (skipped), a folder without `files/` (failed, left in place)
- role checks: viewer cannot upload / build packages; operator cannot delete packages

### 4.2 Install / start / stop (T6)
- add the test agent as a node; deployment from a **stored** release (image tars), 1–3 services not running on the
  host, ports overridden
- install → containers running + healthy, image loaded from the manager's tar (no registry pull)
- stop → stopped; start → running; restart one service; delete with data removal → containers gone
- install while the registry is unreachable (wrong registry login in Settings) still works (image comes from the store)

### 4.3 UI (T7)
- Release page: Release Uploader dropdown lists release-path folders; Upload shows progress to `downloaded`
- Upload a release file card works; stored releases table + detail show per-service checks
- Prerequisites package card: choose package + tick release → Build → row shows building then size; Download works
- Deployment page lists the stored release in its dropdown
- no console errors; texts readable; viewer role sees no upload / build buttons

### 4.4 Prerequisites hand-over (T8)
- `make_prerequisites.py` output contains `prereq.sh` and the three packages with LF scripts
- `bash -n prereq.sh`; `prereq.sh --help` and `--check` run without changing the host
- the hand-over block (copy `releases/<ver>` into `SM_IMPORT_DIR`) works with a fake `$HOME` and a fake
  software-manager `.env` (no real install), and a manager started on that state imports the releases

### 4.5 Office → branch catalog (T10)
- office: `/api/catalog*` 401 while publishing is off, without key, with a wrong key; lists stored releases with the key
- office: file path traversal (`../`, absolute, backslash, empty) and a bad version → 404
- branch: non-http office address refused; wrong key → "the office refused the branch key"
- branch: right key → auto download → release `downloaded` with the office's manifest sha256, in every dropdown
- tampered file at the office (same size) → `failed`, md5 named, nothing registered; download again after the fix works
- office turns publishing off → old key 401
- UI: Release page card "Releases from the office"; Settings → Release catalog (branch form + office key / turn off)

---

## 5. Order for a full run

T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T10, then T9 if deploy code changed. After any fix, re-run the suite that found
the bug and every suite that touches the changed file.

---

## 6. Bug log

| Date | Found by | Bug | Fix | Proved by |
| --- | --- | --- | --- | --- |
| 2026-10-10 | T3 | httpx `build_request()` given `auth=` (TypeError) | pass `auth` to `send()` | T3 |
| 2026-10-10 | T3 | streamed httpx `Response` used as context manager | close in `finally` | T3 |
| 2026-10-10 | T5 | background check died on an unexpected error, release stuck at "scanning 0/16" | job wrapper sets `scan_failed` / `download_failed` + error | T5 |
| 2026-10-10 | T5 | `run-local.sh serve-only` only recognised as 2nd argument | parse all arguments | T5 |
| 2026-10-10 | T2 | scan's `docker compose config` failed when `env_file` is created by `pre_start.sh` | scan creates placeholder env files inside its temp data folder | T2 |
| 2026-10-10 | T8 | `prereq.sh`: CRLF manager `.env` made the import dir `import\r` | strip `\r` when reading `SM_IMPORT_DIR` | T8 A4 |
| 2026-10-10 | T8 | `prereq.sh`: duplicate `SM_IMPORT_DIR` lines gave a path with a newline | last line wins (like `--env-file`), `${HOME}` expanded | T8 A4 |
| 2026-10-10 | T8 | `prereq.sh`: interrupted copy left a folder that was imported as `downloaded`; failures silent | copy to `.<ver>.part` then rename; warn + exit 1 on failure | T8 A1/A2/A5 |
| 2026-10-10 | T8 | `import_local()`: `release.json` that is a list / non-string version crashed manager start | validate object, catch all per folder → `failed` | T8 B4 |
| 2026-10-10 | T8 | `import_local()`: version `../..` reached paths outside the store | version checked with `manifest.VER_RE` | T8 B4 |
| 2026-10-10 | T8 | `import_local()`: DB insert failure left files orphaned in the store | move files back before reporting | T8 |
| 2026-10-10 | T4 | `DELETE /api/releases/%2E%2E` would `rmtree` the store's parent (state DB); unknown version gave 200 | version format + existence check, 404 | T4 traversal-delete |
| 2026-10-10 | T4 | one malformed template (list / string service / non-UTF-8 / bad `wire` label / httpx error) aborted the whole scan | explicit checks, per-service error capture, `fetch()` wraps HTTP errors | T4 23-service bad release |
| 2026-10-10 | T4 | release could be deleted / re-uploaded during scan, download or bundle build (re-upload stuck at `uploaded`) | `busy_reason()`: delete 409, upload 422 | T4 |
| 2026-10-10 | T4 | deleting a release while a bundle with it was building broke the bundle silently | same busy check; building bundles track versions | T4 |
| 2026-10-10 | T4 | prereq path check by string prefix (sibling `pre_requisite_installer_x`); duplicate versions; same-second temp name clash | real parent check + `.tar`, dedupe, unique temp name | T4 bundles |
| 2026-10-10 | T4 | names with `%2F` gave 405 on bundle / import routes | `{name:path}` routes, validation gives 404/422 | T4 |
| 2026-10-10 | T4 | `import_local` trusted `release.json` version / sha | must match parsed manifest | T4 import-local |
| 2026-10-10 | T4 | `file_path()` did not validate version / service | validated (defence in depth) | T4 |
| 2026-10-10 | review | viewer saw empty prereq list / release ticks; `usePoll` swallowed errors so 403/502 looked like "empty" | lists shown read-only to viewers; pages show load errors; stale poll responses ignored | build + T7 rerun |
| 2026-10-10 | review | frontend: hardcoded alarm base path; destructive deployment actions without confirm; unencoded URL parts; live tail duplicates | data root from settings; confirms; `encodeURIComponent`; tail fixed | build + T7 rerun |
| 2026-10-10 | review | sm-logger: `..` as node/service name escaped the log store; bad timestamp → 500 for the whole day; "download all" zip in memory; WARNING stored as INFO | names sanitised; bad lines skipped, bad from/to 422; temp-file stream; level map | py_compile + in-process test |
| 2026-10-10 | review | viewers could read `pod_inspect` container env (DB passwords); `pod` path param reached other agent endpoints | output masked with `support.mask_obj`; `NAME_RE` check | T4 + extra checks |
| 2026-10-10 | review | `?token=` written to access log; session token forwarded to sm-logger; logger 401 logged the user out | log filter masks; token stripped; 502 | extra checks |
| 2026-10-10 | review | upload/delete busy check not atomic (stuck `uploaded`, delete racing download) | `_reserve` / `_unreserve` | T4 |
| 2026-10-10 | review | non-UTF-8 YML in release path → 500 on the whole list; `.partial` files never cleaned; health poll thread died on bad agent reply | per-folder error; `cleanup_partials()` at start; poll wrapper | extra checks |
| 2026-10-10 | review | hardcoded session hours, offline/skew limits, MQTT port, agent port | `SM_SESSION_HOURS`, `SM_OFFLINE_AFTER_SEC`, `SM_CLOCK_SKEW_SEC`, `SM_MQTT_DEFAULT_PORT`, `SM_AGENT_PORT` | T4 |
| 2026-10-10 | T6 | agent `prepare` 500 on a fresh node (`disk_usage` of missing `SM_DATA_ROOT`) | agent creates it, 422 if it cannot | T6 |
| 2026-10-10 | T6 | agent rejected every valid scripts tar (required all four accepted names at once) | exactly one template + one pre-start of the accepted names; extract to temp first | T6 + script check |
| 2026-10-10 | T6 | deployment map had to place all pods (no partial installs) | `"partial": true` in the map → warning instead of error | T6 |
| 2026-10-10 | review | update compensation rolled back pods the agent never switched (to an even older version) | ask agent which release the pod is on first | `deploy_unit_check.py` |
| 2026-10-10 | review | deployment state set before job lock → stuck `installing` on 409; stuck states after restart | `create_job(dep_state=)` after lock; startup marks `interrupted` | `deploy_unit_check.py` |
| 2026-10-10 | review | `KeyError` / `HTTPException` inside job steps (missing pod, unknown version) | `JobError` / `_load()` → clear failure, 404/409 | `deploy_unit_check.py` + T6 |
| 2026-10-10 | review | failed backup left empty folder counted in `SM_BACKUP_KEEP`; agent never checked pg_dump exit / stopped DB | `.partial` + rename; retention counts `meta.json`; agent checks first + aborts stream | `deploy_unit_check.py` |
| 2026-10-10 | review | manager→agent fell back to plain http with node token; no TLS verify | `SM_AGENT_HTTP_FALLBACK` (off), `SM_AGENT_CA_FILE` / `SM_AGENT_TLS_VERIFY` | `deploy_unit_check.py` |
| 2026-10-10 | review | agent: unvalidated names in URL paths; one bad pod stopped healing of all; torn log line killed log shipping; failed `compose down` left container | name check; per-pod heal; skip bad lines; force remove | T6 x2 |
| 2026-10-10 | review | hardcoded heavy/analytics pod sets, waits, self-update health URLs (127.0.0.1 + fixed ports) | `SM_HEAVY_PODS`, `SM_ANALYTICS_PODS`, `SM_*_WAIT_SEC`, node IP + configured ports | T6 + unit |
| 2026-10-10 | T8 | `make_prerequisites.py`: bad version could write outside `--out`; `.partial` left on interrupt | validate version, clean up partial | T8 C |
