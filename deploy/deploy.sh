#!/usr/bin/env bash
# Deploy (or roll back to) a git ref on the production box. Run as the deploy user in /opt/agents.
#
#   deploy/deploy.sh                 # deploy origin/main
#   deploy/deploy.sh 0cd0577         # deploy a specific commit — this is also how you roll back
#
# Steps: fetch → check out the ref → build images tagged with its short sha → build the dashboard
# in a throwaway Node container → back up the database (pre-deploy) → migrate → restart →
# wait for /health → print what is running. Any failure stops the script before the restart, so
# the old containers keep serving.
#
# Rolling back across a migration: `deploy.sh <old sha>` restarts the old code but does NOT
# downgrade the schema. Old code runs fine on a schema that only gained tables or columns; before
# rolling back past a migration that dropped or renamed anything, read RUNBOOK.md "Rollback".
set -euo pipefail

REF="${1:-origin/main}"
cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.yml)
# a smaller box adds a sizing override, e.g. COMPOSE_OVERRIDE=docker-compose.aws.yml (DEPLOY_GUIDE.md Appendix A)
OVERRIDE=$(grep -E '^COMPOSE_OVERRIDE=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
if [ -n "$OVERRIDE" ]; then
  [ -f "$OVERRIDE" ] || { echo "!! COMPOSE_OVERRIDE=$OVERRIDE in .env, but the file is missing" >&2; exit 1; }
  COMPOSE+=(-f "$OVERRIDE")
fi

echo "== fetch and check out $REF"
git fetch --quiet origin
git -c advice.detachedHead=false checkout --quiet "$REF"
SHA=$(git rev-parse --short HEAD)
export IMAGE_TAG="$SHA" GIT_SHA="$SHA"
echo "   now at $SHA: $(git log -1 --format=%s)"

echo "== build images ($SHA)"
"${COMPOSE[@]}" build --quiet api worker scheduler caddy

echo "== build dashboard (into dist.new; swapped in only after a clean build)"
# HOME=/tmp: a uid other than 1000 (`node` in the image) has no home, and npm's cache would go to /.npm
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/dashboard:/app" -w /app node:22-alpine \
  sh -c "npm ci --silent && npx tsc -b && npx vite build --outDir dist.new --emptyOutDir"

if grep -qE '^BACKUP_BUCKET=.+' .env; then
  echo "== pre-deploy backup"
  "${COMPOSE[@]}" run --rm --no-deps scheduler python -m api.scripts.backup run
else
  echo "!! BACKUP_BUCKET not set in .env: deploying WITHOUT a pre-deploy backup" >&2
fi

echo "== migrate"
"${COMPOSE[@]}" run --rm --no-deps api alembic upgrade head

echo "== restart"
# copy INTO the existing directory: Caddy bind-mounts it, and a moved directory would stay pinned
# to the old inode. Old hashed assets stay behind, which keeps already-open tabs working.
mkdir -p dashboard/dist
cp -a dashboard/dist.new/assets/. dashboard/dist/assets/ 2>/dev/null || cp -a dashboard/dist.new/. dashboard/dist/
cp -a dashboard/dist.new/. dashboard/dist/   # index.html last-ish: assets are already in place
rm -rf dashboard/dist.new
"${COMPOSE[@]}" up -d --remove-orphans

echo "== wait for /health"
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T api python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)" \
      2>/dev/null; then
    echo "   healthy: $SHA"
    "${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'
    echo "$SHA $(date -u +%FT%TZ)" >> deploy/history.log
    exit 0
  fi
  sleep 2
done
echo "!! /health did not come up within 60 s — check: docker compose logs --tail 100 api" >&2
echo "!! roll back with: deploy/deploy.sh $(tail -1 deploy/history.log 2>/dev/null | cut -d' ' -f1)" >&2
exit 1
