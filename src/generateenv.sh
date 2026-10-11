#!/usr/bin/env bash
set -euo pipefail
MODULE_NAME="${MODULE_NAME:-software-manager}"
TARGET_DIR="${HOME}/AlarmCenter/${MODULE_NAME}"
mkdir -p "${TARGET_DIR}"
ENV_FILE="${TARGET_DIR}/.env"
NON_INTERACTIVE=false
[[ "${1:-}" == "--non-interactive" ]] && NON_INTERACTIVE=true
touch "${ENV_FILE}"

# --- seed from env.defaults ---
# Missing/empty keys are filled from env.defaults (shipped next to this script).
# ${HOME} and ${SYS_IP} in defaults expand at install time. Existing non-empty values are kept.
DEFAULTS_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.defaults"
seed_defaults() {
  [[ -f "${DEFAULTS_FILE}" ]] || return 0
  local line key val cur tmp
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    val="${val//\$\{HOME\}/${HOME}}"
    val="${val//\$\{SYS_IP\}/${SYS_IP:-127.0.0.1}}"
    if ! grep -qE "^${key}=" "${ENV_FILE}"; then
      printf '%s=%s\n' "${key}" "${val}" >> "${ENV_FILE}"
    elif [[ -n "${val}" ]]; then
      cur="$(grep -E "^${key}=" "${ENV_FILE}" | head -n1 | cut -d= -f2-)"
      if [[ -z "${cur}" ]]; then
        tmp="$(mktemp)"
        awk -v k="${key}" -v v="${val}" 'BEGIN{FS=OFS="="} $1==k {$0=k"="v} {print}' "${ENV_FILE}" > "${tmp}"
        cat "${tmp}" > "${ENV_FILE}" && rm -f "${tmp}"
      fi
    fi
  done < "${DEFAULTS_FILE}"
}
seed_defaults
# --- end seed ---
echo "Wrote ${ENV_FILE}"
