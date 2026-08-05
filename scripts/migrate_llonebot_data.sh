#!/usr/bin/env bash
set -euo pipefail

# Copies existing container-only LLOneBot 8.x data into HiveDeploy's persistent
# host directory. It is intentionally dry-run by default; set DRY_RUN=0 to copy.
DRY_RUN="${DRY_RUN:-1}"

# Prefer the host path mounted into the running panel. DATA_ROOT can still be
# supplied explicitly for non-standard container names or offline migration.
if [ -z "${DATA_ROOT:-}" ]; then
  DATA_ROOT="$(docker inspect bot_panel --format '{{range .Mounts}}{{if eq .Destination "/data/instances"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)" || DATA_ROOT=""
fi
DATA_ROOT="${DATA_ROOT:-/data/instances}"

containers="$(docker ps -a --format '{{.Names}}' | awk '/^llonebot_/')"
if [ -z "${containers}" ]; then
  echo "No LLOneBot containers found."
  exit 0
fi

printf '%s\n' "${containers}" | while IFS= read -r container; do
  username="${container#llonebot_}"
  destination="${DATA_ROOT}/${username}/llonebot/.llonebot-data"
  if [ "${DRY_RUN}" != "0" ]; then
    echo "Would copy ${container}:/root/llonebot/data/. to ${destination}/"
    continue
  fi
  install -d -m 0755 "${destination}"
  if docker cp "${container}:/root/llonebot/data/." "${destination}/"; then
    echo "Migrated persistent LLOneBot data for ${username}."
  else
    echo "Skip ${container}: /root/llonebot/data does not exist or cannot be copied." >&2
  fi
done

if [ "${DRY_RUN}" != "0" ]; then
  echo "Dry run only. Re-run with DRY_RUN=0 after reviewing the paths above."
fi
