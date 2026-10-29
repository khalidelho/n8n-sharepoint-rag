#!/usr/bin/env bash
# Pull the released workflows out of the running n8n, clean them, and leak-check the result.
# Then review `git diff` and commit.
#
# Read-only on n8n: one `n8n export:workflow` (into the container's /tmp) and SELECTs on n8n's Postgres.
# The export holds editor DRAFTS; scripts/published_versions.py swaps in the PUBLISHED versions and drops
# inactive workflows, test copies and anything in scripts/export-skip.txt.
#   N8N_CONTAINER     n8n main container   (default rag-n8n-n8n-main-1)
#   N8N_PG_CONTAINER  n8n's Postgres       (default rag-n8n-postgres-1)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
container="${N8N_CONTAINER:-rag-n8n-n8n-main-1}"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/drafts" "$tmp/published"

docker exec "$container" sh -c 'rm -rf /tmp/wf-export && n8n export:workflow --all --separate --output=/tmp/wf-export' >/dev/null
docker cp "$container:/tmp/wf-export/." "$tmp/drafts/" >/dev/null
docker exec "$container" rm -rf /tmp/wf-export
python3 scripts/published_versions.py "$tmp/drafts" "$tmp/published"
# Start from an empty folder so a workflow that is no longer released disappears from the repo too
rm -f n8n/workflows/*.json
python3 scripts/sanitize_workflows.py "$tmp/published" n8n/workflows
scripts/check-leaks.sh
