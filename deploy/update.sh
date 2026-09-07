#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/ozon-agent}"
BRANCH="${BRANCH:-main}"
LOCK_FILE="${LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/ozon-agent-update.lock}"

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Update is already running"; exit 0; }

cd "$APP_DIR"

git fetch --prune origin "$BRANCH"
LOCAL_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
REMOTE_COMMIT="$(git rev-parse "origin/$BRANCH")"

if [[ "$LOCAL_COMMIT" == "$REMOTE_COMMIT" ]]; then
    echo "Already up to date: ${LOCAL_COMMIT:0:12}"
    docker compose up -d --remove-orphans
    exit 0
fi

if [[ ! -f .env ]]; then
    echo "ERROR: $APP_DIR/.env is missing; refusing to deploy" >&2
    exit 1
fi

# Only tracked source files are replaced. .env, the named Docker volume,
# and all runtime data remain outside the Git checkout.
git checkout --force "$BRANCH"
git reset --hard "origin/$BRANCH"

docker compose config --quiet
docker compose up --build -d --remove-orphans

docker image prune -f >/dev/null || true

echo "Deployed ${REMOTE_COMMIT:0:12}; runtime volume and .env preserved"
