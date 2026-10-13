#!/usr/bin/env bash
#
# Consistent online snapshot of the SQLite database (sqlite3 backup API, safe while
# the collector writes). Runs daily from solar-backup.timer and before every deploy.
# The CT itself is additionally covered by Proxmox Backup Server.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/srv/solar/data}"
KEEP="${KEEP:-14}"
IMAGE_ENV="${IMAGE_ENV:-/srv/solar/image.env}"

image="$(sed -n 's/^SOLAR_IMAGE=//p' "$IMAGE_ENV")"
[[ -n "$image" ]] || { echo "no image recorded in ${IMAGE_ENV}" >&2; exit 1; }
[[ -f "${DATA_DIR}/solar.db" ]] || { echo "no database yet; nothing to back up"; exit 0; }

mkdir -p "${DATA_DIR}/backups"
chown 10001:10001 "${DATA_DIR}/backups"
name="solar-$(date +%Y%m%d-%H%M%S).db"

docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --user 10001:10001 -v "${DATA_DIR}:/data" --entrypoint python "$image" -c "
import sqlite3
src = sqlite3.connect('/data/solar.db')
dst = sqlite3.connect('/data/backups/${name}')
src.backup(dst)
dst.close()
assert sqlite3.connect('/data/backups/${name}').execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
"
echo "backup written: ${DATA_DIR}/backups/${name}"
find "${DATA_DIR}/backups" -maxdepth 1 -name "solar-*.db" -printf "%T@ %p\n" | sort -rn | tail -n +$(( KEEP + 1 )) | cut -d" " -f2- | xargs -r rm -f
