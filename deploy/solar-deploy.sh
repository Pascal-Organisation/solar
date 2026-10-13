#!/usr/bin/env bash
#
# Pull-based deployer, run on the host by solar-deploy.timer.
#
# Why pull: CI holds no credential to this host. The host only reaches out to a
# public registry and refuses anything not signed by this repo's release workflow.
#
#   1. resolve the digest behind :latest
#   2. cosign-verify it was signed by .github/workflows/release.yml on main
#   3. refuse anything not built strictly later than what runs (replay protection)
#   4. sync docker-compose.yml from the verified image
#   5. back up the database, switch, wait for health
#   6. roll back image and compose file if the new release is not healthy
#
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/pascal05/solar}"
DEPLOY_DIR="${DEPLOY_DIR:-/srv/solar}"
IMAGE_ENV="${DEPLOY_DIR}/image.env"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"
COMPOSE_PREV="${COMPOSE_FILE}.prev"
CERT_IDENTITY="${CERT_IDENTITY:-https://github.com/pascal05/solar/.github/workflows/release.yml@refs/heads/main}"
OIDC_ISSUER="https://token.actions.githubusercontent.com"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"
MAX_RESTARTS="${MAX_RESTARTS:-3}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-/usr/local/bin/solar-backup.sh}"
SERVICES=(collector web)

COMPOSE=(docker compose --project-name solar --project-directory "$DEPLOY_DIR" -f "$COMPOSE_FILE" --env-file "$IMAGE_ENV")

log() { printf '%s solar-deploy: %s\n' "$(date -Is)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

label() {
  local v=""
  v="$(docker image inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null)" || v=""
  [[ "$v" == "<no value>" ]] && v=""
  printf '%s' "$v"
}
to_epoch() { date -d "$1" +%s 2>/dev/null || true; }

write_image_env() {
  local tmp
  tmp="$(mktemp "${IMAGE_ENV}.XXXXXX")"
  printf 'SOLAR_IMAGE=%s\nSOLAR_BUILT_AT=%s\n' "$1" "$2" > "$tmp"
  chmod 0644 "$tmp"
  mv "$tmp" "$IMAGE_ENV"
}

compose_synced=0
sync_compose() {
  local cid tmp
  tmp="$(mktemp "${COMPOSE_FILE}.XXXXXX")"
  cid="$(docker create "$candidate")" || { rm -f "$tmp"; die "docker create failed"; }
  if docker cp "${cid}:/app/deploy/docker-compose.yml" "$tmp" 2>/dev/null; then
    docker rm "$cid" >/dev/null
    if [[ -f "$COMPOSE_FILE" ]] && cmp -s "$tmp" "$COMPOSE_FILE"; then
      rm -f "$tmp"
    else
      [[ -f "$COMPOSE_FILE" ]] && cp "$COMPOSE_FILE" "$COMPOSE_PREV"
      chmod 0644 "$tmp"
      mv "$tmp" "$COMPOSE_FILE"
      compose_synced=1
      log "compose file updated from the verified image"
    fi
  else
    docker rm "$cid" >/dev/null 2>&1 || true
    rm -f "$tmp"
    die "image carries no deploy/docker-compose.yml"
  fi
}

rollback() {
  log "$1; rolling back to ${current:-<none>}"
  if [[ "$compose_synced" == 1 && -f "$COMPOSE_PREV" ]]; then
    cp "$COMPOSE_PREV" "$COMPOSE_FILE"
  fi
  if [[ -n "$current" ]]; then
    write_image_env "$current" "$cur_built"
    "${COMPOSE[@]}" up -d --remove-orphans || log "WARNING: rollback 'compose up' failed too"
    die "rolled back to ${current}"
  fi
  die "no previous image to roll back to; left in place for inspection"
}

prune_old_images() {
  local keep id
  keep="$(docker image inspect -f '{{.Id}}' "$candidate" "${current:-$candidate}" 2>/dev/null | sort -u)" || true
  while read -r id; do
    [[ -n "$id" ]] || continue
    grep -qxF "$id" <<<"$keep" && continue
    docker rmi "$id" >/dev/null 2>&1 || true
  done < <(docker image ls -aq --no-trunc --filter "reference=${IMAGE}" | sort -u)
}

exec 9>"${DEPLOY_DIR}/.deploy.lock"
flock -n 9 || { log "another deploy is running; skipping"; exit 0; }

current=""; cur_built=""
if [[ -f "$IMAGE_ENV" ]]; then
  current="$(sed -n 's/^SOLAR_IMAGE=//p' "$IMAGE_ENV")"
  cur_built="$(sed -n 's/^SOLAR_BUILT_AT=//p' "$IMAGE_ENV")"
fi

docker pull --quiet "${IMAGE}:latest" >/dev/null || die "could not pull ${IMAGE}:latest"
candidate="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${IMAGE}:latest" \
  | grep "^${IMAGE}@" | head -1)"
[[ -n "$candidate" ]] || die "could not resolve a digest for ${IMAGE}:latest"
digest="${candidate#*@}"

if [[ "$candidate" == "$current" ]]; then
  exit 0
fi

# Gate 1: authenticity, against the digest, before anything runs.
log "verifying signature for ${digest} ..."
if ! cosign verify --certificate-identity "$CERT_IDENTITY" --certificate-oidc-issuer "$OIDC_ISSUER" \
      "$candidate" >/dev/null; then
  docker rmi "$candidate" >/dev/null 2>&1 || true
  die "signature verification FAILED for ${candidate}; refusing to deploy"
fi

# Gate 2: freshness. A signature says who built it, not which build it is.
cand_built="$(label "$candidate" org.opencontainers.image.created)"
cand_epoch="$(to_epoch "$cand_built")"
[[ -n "$cand_epoch" ]] || die "candidate has no usable build timestamp; refusing"
if [[ -n "$cur_built" ]]; then
  cur_epoch="$(to_epoch "$cur_built")"
  if [[ -n "$cur_epoch" ]] && (( cand_epoch <= cur_epoch )); then
    die "candidate built ${cand_built} is not newer than running ${cur_built}; refusing (replayed digest?)"
  fi
fi

log "signature OK; deploying ${digest} (commit $(label "$candidate" org.opencontainers.image.revision | cut -c1-7), built ${cand_built})"
sync_compose

if [[ -n "$current" && "${SKIP_BACKUP:-0}" != 1 && -x "$BACKUP_SCRIPT" ]]; then
  "$BACKUP_SCRIPT" || die "pre-deploy backup failed; refusing to deploy (SKIP_BACKUP=1 overrides)"
fi

write_image_env "$candidate" "$cand_built"
"${COMPOSE[@]}" up -d --remove-orphans || rollback "compose up failed"

log "waiting up to ${HEALTH_TIMEOUT}s for ${SERVICES[*]} ..."
deadline=$(( SECONDS + HEALTH_TIMEOUT ))
while (( SECONDS < deadline )); do
  healthy=0
  for svc in "${SERVICES[@]}"; do
    cid="$("${COMPOSE[@]}" ps -q "$svc")"
    [[ -n "$cid" ]] || rollback "${svc} container was not created"
    state="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)"
    (( restarts >= MAX_RESTARTS )) && rollback "${svc} is crash-looping"
    [[ "$state" == unhealthy ]] && rollback "${svc} is unhealthy"
    [[ "$state" == healthy ]] && healthy=$(( healthy + 1 ))
  done
  if (( healthy == ${#SERVICES[@]} )); then
    log "healthy; deployed ${digest}"
    prune_old_images
    exit 0
  fi
  sleep 5
done
rollback "new release did not become healthy in time"
