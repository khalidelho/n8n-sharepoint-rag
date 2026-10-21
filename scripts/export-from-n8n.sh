#!/usr/bin/env bash
# Pull the latest workflows out of the running n8n, clean them, and leak-check the result.
# Then review `git diff` and commit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
container="${N8N_CONTAINER:-rag-n8n-n8n-main-1}"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

docker exec "$container" sh -c 'rm -rf /tmp/wf-export && n8n export:workflow --all --separate --output=/tmp/wf-export' >/dev/null
docker cp "$container:/tmp/wf-export/." "$tmp/" >/dev/null
docker exec "$container" rm -rf /tmp/wf-export
python3 scripts/sanitize_workflows.py "$tmp" n8n/workflows
scripts/check-leaks.sh
