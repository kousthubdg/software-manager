#!/bin/bash
# T8 - prerequisites hand-over. Never runs the prereq.sh install path and never touches a running deployment:
#   A. prereq.sh hand-over (PREREQ_ONLY_HANDOFF=1, scratch $HOME): releases/<ver> -> SM_IMPORT_DIR
#   B. a test Software Manager imports what was handed over (new / same again / clashing / broken)
#   C. make_prerequisites.py (--help, --out to a scratch folder, no --out and no SM_RELEASE_SOURCE)
#   prereq_handoff_test.sh
# env: PY (python with software-manager requirements), SERVICES_ROOT (default: two folders up), WORK (default
#      $HOME/sm-t8), SM_TEST_PORT (12995), SM_TEST_PW (admin-test-pw), SM_RELEASE_SOURCE (read only, only compared),
#      T8_SKIP_MANAGER=1, T8_SKIP_MAKE=1, T8_SKIP_CHECK=1 (skip prereq.sh --check), T8_KEEP=1 (leave manager running)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVICES_ROOT="${SERVICES_ROOT:-$(cd "$HERE/../.." && pwd)}"
PREREQ_SRC="${PREREQ_SH:-$SERVICES_ROOT/sm-agent/build/prereq.sh}"
MAKE_PY="$SERVICES_ROOT/platform/scripts/make_prerequisites.py"
PY="${PY:-python3}"
WORK="${WORK:-$HOME/sm-t8}"
PORT="${SM_TEST_PORT:-12995}"
PW="${SM_TEST_PW:-admin-test-pw}"
PASS=0; FAIL=0
t() {  # t NAME COMMAND...  (pass when COMMAND succeeds)
  local n="$1"; shift
  if "$@"; then echo "  PASS $n"; PASS=$((PASS + 1)); else echo "  FAIL $n"; FAIL=$((FAIL + 1)); fi
}
no() { ! "$@"; }
has() { grep -qF -- "$2" <<<"$1"; }
no_part() { [[ -z "$(find "$1" -maxdepth 1 -name '.*.part' 2>/dev/null)" ]]; }

rm -rf "$WORK" && mkdir -p "$WORK" || exit 1

# Fake package: <dir>/prereq.sh (LF) + releases/<ver>/{release.json, files/}
mkrel() {  # mkrel PKG VERSION TAG [JSON_OVERRIDE | BADSHA]
  local d="$1/releases/$2"
  mkdir -p "$d/files/svc-a" "$d/files/svc b"
  printf 'scripts %s\n' "$3" > "$d/files/svc-a/scripts.tar"
  printf 'image %s\n' "$3" > "$d/files/svc-a/image.tar"
  printf 'data %s\n' "$3" > "$d/files/svc b/my data.txt"
  chmod 640 "$d/files/svc-a/image.tar"
  if [[ -n "${4:-}" && "$4" != BADSHA ]]; then printf '%s' "$4" > "$d/release.json"; return; fi
  "$PY" - "$HERE/../src" "$d/release.json" "$2" "$3" "${4:-}" <<'EOF'
import json, sys, time
src, p, v, tag, mode = sys.argv[1:6]
sys.path.insert(0, src)
from app import manifest
text = (f"software_name: T8_SOFTWARE\nsoftware_version: {v}\ndescription: T8 {tag}\ninstallers:\n"
        f"  - name: svc-a\n    image: registry.test:5000/svc-a\n    image_ver: 1.0.0\n    scripts_ver: 1.0.0\n"
        f"    scripts_path: svc-a/svc-a_scripts.tar\n# {tag}\n")
m, errors, _ = manifest.parse(text)
assert m, errors
json.dump({"version": v, "software_name": m.software_name, "description": m.description, "manifest_text": text,
           "manifest_sha256": "0" * 64 if mode == "BADSHA" else m.sha256, "uploaded_by": "t8",
           "uploaded_at": time.time(), "scan_json": "{}", "resolved_json": "{}"}, open(p, "w"))
EOF
}
mkpkg() {  # mkpkg PKG
  mkdir -p "$1/releases" && sed 's/\r$//' "$PREREQ_SRC" > "$1/prereq.sh"
}
handoff() {  # handoff HOME PKG  -> output in $OUT, exit code in $RC
  OUT="$(env HOME="$1" PREREQ_ONLY_HANDOFF=1 bash "$2/prereq.sh" 2>&1)"; RC=$?
  echo "$OUT" | sed 's/^/      /'
}
menv() { mkdir -p "$1/AlarmCenter/software-manager" && printf "$2" > "$1/AlarmCenter/software-manager/.env"; }

echo "== A. prereq.sh hand-over ($PREREQ_SRC)"
PKG="$WORK/site pkg/sm_pre_requisites"
mkpkg "$PKG"
mkrel "$PKG" 1.0.0 A
mkrel "$PKG" 1.1.0 A
mkdir -p "$PKG/releases/no-json/files" && echo x > "$PKG/releases/no-json/files/x"
echo notes > "$PKG/releases/notes.txt"
t "bash -n prereq.sh" bash -n "$PKG/prereq.sh"
HELP="$(bash "$PKG/prereq.sh" --help 2>&1)"
t "prereq.sh --help" has "$HELP" "--manager"
t "manager install calls handoff_releases before restarting software-manager" \
  grep -qzP 'handoff_releases\n\s*docker restart software-manager' "$PKG/prereq.sh"

echo "-- A1 SM_IMPORT_DIR from the env file (spaces in HOME, package and import paths)"
H1="$WORK/home one"; IMP1="$WORK/import dir/one"
menv "$H1" "SERVICE_PORT=2999\nSM_IMPORT_DIR=$IMP1\nBRANCH_NAME=t8\n"
handoff "$H1" "$PKG"
t "exit 0" [ "$RC" -eq 0 ]
t "1.0.0 copied identically" diff -r "$PKG/releases/1.0.0" "$IMP1/1.0.0"
t "1.1.0 copied identically" diff -r "$PKG/releases/1.1.0" "$IMP1/1.1.0"
t "file modes kept" [ "$(stat -c %a "$IMP1/1.0.0/files/svc-a/image.tar")" = 640 ]
t "folder without release.json skipped" [ ! -e "$IMP1/no-json" ]
t "plain file under releases/ skipped" [ ! -e "$IMP1/notes.txt" ]
t "no partial copies left" no_part "$IMP1"
t "default folder not used" [ ! -e "$H1/AlarmCenter/software-manager/data/import" ]

echo "-- A2 re-run is idempotent (stale file in the copy is replaced)"
echo stale > "$IMP1/1.0.0/files/stale"
handoff "$H1" "$PKG"
t "exit 0" [ "$RC" -eq 0 ]
t "1.0.0 identical to package again (stale file gone)" diff -r "$PKG/releases/1.0.0" "$IMP1/1.0.0"
t "import folder holds exactly 1.0.0 and 1.1.0" [ "$(ls -A "$IMP1" | tr '\n' ' ')" = "1.0.0 1.1.0 " ]
t "no partial copies left" no_part "$IMP1"

echo "-- A3 default folder when SM_IMPORT_DIR is not set"
H2="$WORK/home two"
handoff "$H2" "$PKG"
t "no env file: copied to \$HOME/AlarmCenter/software-manager/data/import" \
  diff -r "$PKG/releases/1.0.0" "$H2/AlarmCenter/software-manager/data/import/1.0.0"
H3="$WORK/home three"
menv "$H3" "SM_IMPORT_DIR=\nSERVICE_PORT=2999\n"
handoff "$H3" "$PKG"
t "empty SM_IMPORT_DIR=: default folder" diff -r "$PKG/releases/1.0.0" "$H3/AlarmCenter/software-manager/data/import/1.0.0"

echo "-- A4 env file edge cases"
H4="$WORK/home four"; IMP4="$WORK/import four"
menv "$H4" "SM_IMPORT_DIR=$IMP4\r\nSERVICE_PORT=2999\r\n"
handoff "$H4" "$PKG"
t "CRLF env file: no carriage return in the folder name" diff -r "$PKG/releases/1.0.0" "$IMP4/1.0.0"
t "CRLF env file: no stray folder" [ -z "$(find "$WORK" -maxdepth 1 -name $'*\r*')" ]
H5="$WORK/home five"
menv "$H5" "SM_IMPORT_DIR=$WORK/import five-old\nSM_IMPORT_DIR=$WORK/import five\n"
handoff "$H5" "$PKG"
t "duplicate key: last value wins (as docker --env-file)" diff -r "$PKG/releases/1.0.0" "$WORK/import five/1.0.0"
H6="$WORK/home six"
menv "$H6" 'SM_IMPORT_DIR=${HOME}/imp\n'
handoff "$H6" "$PKG"
t "literal \${HOME} expanded" diff -r "$PKG/releases/1.0.0" "$H6/imp/1.0.0"

echo "-- A5 failures and no-ops"
echo file > "$WORK/a-file"
H7="$WORK/home seven"
menv "$H7" "SM_IMPORT_DIR=$WORK/a-file/import\n"
handoff "$H7" "$PKG"
t "import folder cannot be created: warning + exit 1" [ "$RC" -eq 1 ]
t "warning printed" has "$OUT" "[warn]"
PKG0="$WORK/pkg-no-releases/sm_pre_requisites"; mkdir -p "$PKG0" && sed 's/\r$//' "$PREREQ_SRC" > "$PKG0/prereq.sh"
H8="$WORK/home eight"
handoff "$H8" "$PKG0"
t "package without releases/: exit 0, nothing created" [ "$RC" -eq 0 -a ! -e "$H8/AlarmCenter" ]

if [[ "${T8_SKIP_CHECK:-0}" != 1 ]]; then
  echo "-- A6 prereq.sh --check changes nothing"
  snap() { { md5sum /etc/docker/daemon.json 2>/dev/null; ls /etc/systemd/system/docker.service.d 2>/dev/null; \
             docker ps -a --format '{{.Names}} {{.State}}' 2>/dev/null | sort; } | md5sum; }
  s1="$(snap)"
  OUT="$(bash "$PKG/prereq.sh" --check --no-firewall --data-root "$WORK/check-root" 2>&1)"; RC=$?
  echo "$OUT" | tail -3 | sed 's/^/      /'
  t "--check finished" has "$OUT" "Check finished"
  t "--check: daemon.json, systemd drop-ins and containers unchanged" [ "$s1" = "$(snap)" ]
  t "--check: data root not created" [ ! -e "$WORK/check-root" ]
fi

if [[ "${T8_SKIP_MANAGER:-0}" != 1 ]]; then
  echo "== B. Software Manager import (port $PORT)"
  MROOT="$WORK/mgr"; IMPM="$MROOT/ac/data/import"; HM="$WORK/home mgr"
  menv "$HM" "SM_IMPORT_DIR=$IMPM\n"
  mgr() { SM_TEST_ROOT="$MROOT" SM_TEST_PORT="$PORT" SM_TEST_PW="$PW" PY="$PY" bash "$HERE/run-local.sh" "$@" serve-only \
            >> "$WORK/run-local.log" 2>&1; }
  api() {
    "$PY" - "$PORT" "$PW" "$@" <<'EOF'
import sys, httpx
port, pw, cmd = sys.argv[1], sys.argv[2], sys.argv[3]
c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"username": "admin", "password": pw}).json()["token"]
if cmd == "list":
    for r in c.get("/api/releases").json():
        print(r["version"], r["status"], r.get("software_name"))
elif cmd == "import":
    for x in c.post("/api/releases/import-local").json()["result"]:
        print(x["version"], "|", x["result"])
elif cmd == "manifest":
    print(c.get(f"/api/releases/{sys.argv[4]}").json()["manifest_text"].splitlines()[-1])
EOF
  }
  startlog() { grep 'release import' "$MROOT/sm.log" | sed 's/^/      log: /'; }
  mgr --fresh
  t "fresh manager up" curl -sf -m 3 "http://127.0.0.1:$PORT/health" -o /dev/null
  t "fresh manager has no releases" [ -z "$(api list)" ]

  echo "-- B1 new release -> downloaded"
  PA="$WORK/pkg-a/sm_pre_requisites"; mkpkg "$PA"; mkrel "$PA" 1.0.0 A
  handoff "$HM" "$PA"
  t "handed over into the manager's import folder" [ -f "$IMPM/1.0.0/release.json" ]
  mgr; startlog
  L="$(api list)"; echo "$L" | sed 's/^/      /'
  t "1.0.0 downloaded" has "$L" "1.0.0 downloaded T8_SOFTWARE"
  t "import folder emptied" [ ! -e "$IMPM/1.0.0" ]
  t "files moved into the store" diff -r "$PA/releases/1.0.0/files" "$MROOT/releases/1.0.0"

  echo "-- B2 same release (same sha) again -> removed from import folder, no duplicate"
  handoff "$HM" "$PA"
  mgr; startlog
  L="$(api list)"
  t "start log says already here" grep -q 'release import 1.0.0: already here' "$MROOT/sm.log"
  t "removed from import folder" [ ! -e "$IMPM/1.0.0" ]
  t "exactly one 1.0.0" [ "$(grep -c '^1.0.0 ' <<<"$L")" -eq 1 ]
  t "store unchanged" diff -r "$PA/releases/1.0.0/files" "$MROOT/releases/1.0.0"

  echo "-- B3 same version, different content -> skipped, left in place"
  PB="$WORK/pkg-b/sm_pre_requisites"; mkpkg "$PB"; mkrel "$PB" 1.0.0 B
  handoff "$HM" "$PB"
  mgr; startlog
  t "start log says skipped" grep -q 'release import 1.0.0: skipped' "$MROOT/sm.log"
  t "left in import folder" diff -r "$PB/releases/1.0.0" "$IMPM/1.0.0"
  t "stored release still the original" [ "$(api manifest 1.0.0)" = "# A" ]
  t "store files still the original" diff -r "$PA/releases/1.0.0/files" "$MROOT/releases/1.0.0"
  R="$(api import)"; echo "$R" | sed 's/^/      api: /'
  t "import-local API: skipped again" has "$R" "1.0.0 | skipped"
  rm -rf "${IMPM:?}/1.0.0"

  echo "-- B4 broken hand-overs -> failed, left in place, manager still starts"
  PC="$WORK/pkg-c/sm_pre_requisites"; mkpkg "$PC"
  mkrel "$PC" 2.0.0 C '{"version": "2.0.0", broken'
  mkrel "$PC" 3.0.0 C '[]'
  mkrel "$PC" 4.0.0 C '{"version": "../../escape", "software_name": "x", "manifest_text": "x", "manifest_sha256": "x"}'
  mkrel "$PC" 5.0.0 C '{"version": 5, "software_name": "x", "manifest_text": "x", "manifest_sha256": "x"}'
  mkrel "$PC" 6.0.0 C; rm -rf "$PC/releases/6.0.0/files"
  mkrel "$PC" 7.0.0 C BADSHA
  handoff "$HM" "$PC"
  mgr; startlog
  t "manager starts with broken imports" curl -sf -m 3 "http://127.0.0.1:$PORT/health" -o /dev/null
  R="$(api import)"; echo "$R" | sed 's/^/      api: /'
  for v in 2.0.0 3.0.0 4.0.0 5.0.0 6.0.0 7.0.0; do
    t "$v failed" has "$R" "$v | failed"
    t "$v left in place" diff -r "$PC/releases/$v" "$IMPM/$v"
  done
  L="$(api list)"
  t "only 1.0.0 registered" [ "$(echo "$L" | awk '{print $1}' | tr '\n' ' ')" = "1.0.0 " ]
  t "no folder escaped the store" [ ! -e "$MROOT/escape" -a ! -e "$WORK/escape" ]
  t "7.0.0 files not left in the store" [ ! -e "$MROOT/releases/7.0.0" ]
  [[ "${T8_KEEP:-0}" == 1 ]] || fuser -k "$PORT/tcp" >/dev/null 2>&1
fi

if [[ "${T8_SKIP_MAKE:-0}" != 1 ]]; then
  echo "== C. make_prerequisites.py"
  rs_snap() { [[ -n "${SM_RELEASE_SOURCE:-}" ]] && find "$SM_RELEASE_SOURCE/pre_requisite_installer" -printf '%p %s %T@\n' 2>/dev/null | sort | md5sum; }
  rs0="$(rs_snap)"
  OUT="$(python3 "$MAKE_PY" --help 2>&1)"; RC=$?
  t "--help exit 0" [ "$RC" -eq 0 ]
  t "--help documents --out" has "$OUT" "--out"
  OUT="$(env -u SM_RELEASE_SOURCE python3 "$MAKE_PY" t8-0.0.1 2>&1)"; RC=$?
  echo "$OUT" | tail -1 | sed 's/^/      /'
  t "no --out and no SM_RELEASE_SOURCE: exit 2" [ "$RC" -eq 2 ]
  t "error names --out" has "$OUT" "--out"
  OUT="$(env -u SM_RELEASE_SOURCE python3 "$MAKE_PY" ../t8-escape --out "$WORK/mkout" 2>&1)"; RC=$?
  t "bad version refused" [ "$RC" -eq 2 -a ! -e "$WORK/t8-escape" -a ! -e "$WORK/mkout" ]
  OUT="$(env -u SM_RELEASE_SOURCE SERVICES_ROOT="$SERVICES_ROOT" python3 "$MAKE_PY" t8-0.0.1 --out "$WORK/mkout" 2>&1)"; RC=$?
  echo "$OUT" | sed 's/^/      /'
  TAR="$WORK/mkout/pre_requisite_installer/t8-0.0.1/sm_pre_requisites.tar"
  t "--out without SM_RELEASE_SOURCE: exit 0" [ "$RC" -eq 0 -a -f "$TAR" ]
  if [[ -f "$TAR" ]]; then
    LIST="$(tar -tf "$TAR")"
    for m in prereq.sh sm-agent/sm-agent.tar sm-agent/install.sh sm-logger/sm-logger.tar sm-logger/install.sh \
             software-manager/software-manager.tar software-manager/install.sh; do
      t "tar has sm_pre_requisites/$m" grep -qx "sm_pre_requisites/$m" <<<"$LIST"
    done
    X="$WORK/mkx"; mkdir -p "$X" && tar -xf "$TAR" -C "$X" --wildcards '*.sh'
    t "scripts are LF" [ -z "$(grep -l $'\r' "$X"/sm_pre_requisites/*.sh "$X"/sm_pre_requisites/*/install.sh)" ]
    t "scripts executable" [ -x "$X/sm_pre_requisites/prereq.sh" -a -x "$X/sm_pre_requisites/sm-agent/install.sh" ]
    t "packaged prereq.sh passes bash -n" bash -n "$X/sm_pre_requisites/prereq.sh"
    t "packaged prereq.sh not dated 1970" [ "$(stat -c %Y "$X/sm_pre_requisites/prereq.sh")" -gt 86400 ]
  fi
  OUT="$(env -u SM_RELEASE_SOURCE python3 "$MAKE_PY" t8-0.0.1 --out "$WORK/mkout" 2>&1)"; RC=$?
  t "existing version without --force refused" [ "$RC" -eq 1 ]
  OUT="$(SM_RELEASE_SOURCE="$WORK/env-src" python3 "$MAKE_PY" t8-0.0.1 --out "$WORK/mkout" --force 2>&1)"; RC=$?
  t "--out wins over SM_RELEASE_SOURCE" [ "$RC" -eq 0 -a ! -e "$WORK/env-src" ]
  t "no partial file left" [ -z "$(find "$WORK/mkout" -name '*.partial')" ]
  t "release path untouched" [ "$rs0" = "$(rs_snap)" ]
fi

echo
echo "T8: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
