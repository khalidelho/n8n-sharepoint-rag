#!/bin/bash
# backup.sh — nightly backup of the RAG stack. Each run writes one set, $BACKUP_DIR/<YYYYMMDD-HHMM>/:
#   n8n.dump              n8n Postgres (workflows, credentials encrypted with N8N_ENCRYPTION_KEY, executions)
#   supabase.dump         Supabase Postgres, every schema the postgres role dumps (documents, page_views, auth, storage rows)
#   workflows.json        n8n export:workflow --all: a readable copy, to re-import one workflow without a full restore
#   open-webui-data.tgz   Open WebUI volume: webui.db (an online-backup snapshot), uploads, vector_db; not cache/ (model
#                         downloads, ~1 GB, fetched again on first use)
#   supabase-storage.tgz  Supabase storage volume: the document-images bucket every how-to answer links
#   last_backup.json      {at, ok, bytes, files, path, ...}; a copy of the newest good one sits in $BACKUP_DIR too
# A set is built in <stamp>.partial/ and renamed only once every file validated, so a <stamp>/ directory is always a
# whole set; a failed run's .partial stays (for diagnosis) until a run starts more than an hour later.
# Not in the backup, by design: the .env files and keys (among them N8N_ENCRYPTION_KEY: keep them in the password
# manager, docs/runbook.md "Restore") and the vision page images (4.4 GB, rebuilt by the vision service). The sets are
# still sensitive (encrypted n8n credentials, password hashes, chat history): mode 700, and an rclone crypt remote.
#
# Env: BACKUP_DIR (default ~/rag-backups) · BACKUP_KEEP (sets kept, default 14) · BACKUP_REMOTE (rclone remote:path; unset
#      = local only) · BACKUP_NOTIFY (0 = log only, and the webhook token is not read; anything else, the default, =
#      POST ok/failed to the incident webhook) · RAG_ENV_FILE (where RAG_WEBHOOK_TOKEN lives) · RAG_INCIDENT_URL
# Exit: 0 ok · 3 the set is good but the "ok" POST failed · any other non-zero = failed (incident "failed" posted unless
#       BACKUP_NOTIFY=0). Incident texts avoid the words the incident scrubber cuts at (content, messages, chatInput).
# Scheduled by ops/launchd/com.example.rag-backup.plist (03:40); on the VM, one cron line.
# macOS /bin/bash is 3.2: no mapfile, no associative arrays, no ${x,,}.
set -Eeuo pipefail
umask 077
# launchd's PATH has neither Docker Desktop's CLI (~/.docker/bin) nor Homebrew (jq/rclone may live there)
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BACKUP_DIR="${BACKUP_DIR:-$HOME/rag-backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
BACKUP_NOTIFY="${BACKUP_NOTIFY:-1}"
ENV_FILE="${RAG_ENV_FILE:-$(cd "$(dirname "$0")/.." && pwd)/stack/n8n/.env}"
INCIDENT_URL="${RAG_INCIDENT_URL:-http://localhost:5680/webhook/rag-incident}"

N8N_DB=rag-n8n-postgres-1; N8N_MAIN=rag-n8n-n8n-main-1; SUPA_DB=supabase-db; OWU=rag-openwebui-open-webui-1
OWU_VOL=rag-openwebui_open-webui-data; STORAGE_VOL=supabase_storage-data
TAR_IMAGE=alpine:3.20; PG_IMAGE=postgres:17.6-alpine
FILES="n8n.dump supabase.dump workflows.json open-webui-data.tgz supabase-storage.tgz"

log() { printf '%s backup: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { FAIL_MSG="$*"; FAIL_LINE=${BASH_LINENO[0]}; false; }   # `false` trips the ERR trap below: it reports and exits

# notify <ok|failed> <message> [bytes] [path]. The token goes to curl on stdin (-H @-), never on a command line or in a log.
notify() {
  local kind=$1 msg=$2 bytes=${3:-0} path=${4:-} token body
  if [ "$BACKUP_NOTIFY" = 0 ]; then log "notify off (BACKUP_NOTIFY=$BACKUP_NOTIFY): not posting '$kind'"; return 0; fi
  token=$(grep -E '^[[:space:]]*RAG_WEBHOOK_TOKEN=' "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d "\"' \r") || true
  [ -n "$token" ] || { log "RAG_WEBHOOK_TOKEN not found in $ENV_FILE: '$kind' not posted"; return 1; }
  body=$(jq -cn --arg kind "$kind" --arg msg "$msg" --argjson bytes "$bytes" --arg path "$path" \
    '{source: "backup", kind: $kind, message: $msg, data: {bytes: $bytes, path: $path}}') || return 1
  printf 'Authorization: Bearer %s\n' "$token" |
    curl -fsS -m 30 -H @- -H 'Content-Type: application/json' --data-binary "$body" "$INCIDENT_URL" >/dev/null
}

# write_status <dir> <ok:true|false> <bytes> <files-json> <path> [extra-json]: last_backup.json in <dir>
write_status() {
  local extra=${6:-}
  [ -n "$extra" ] || extra='{}'
  jq -n --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson ok "$2" --argjson bytes "$3" --argjson files "$4" \
    --arg path "$5" --argjson seconds "$SECONDS" --argjson extra "$extra" \
    '{at: $at, ok: $ok, bytes: $bytes, files: $files, path: $path, seconds: $seconds} + $extra' >"$1/last_backup.json"
}

STEP=start; FAIL_MSG=; FAIL_LINE=; SET=; SET_TMP=
on_err() {
  local rc=$1 line=$2 msg
  [ "${BASH_SUBSHELL:-0}" -gt 0 ] && exit "$rc"     # inside $(...): the parent shell sees the failure and reports it
  trap - ERR; set +e
  [ "$rc" -ne 0 ] || rc=1
  msg="Backup failed at '$STEP' (exit $rc, line ${FAIL_LINE:-$line})${FAIL_MSG:+: $FAIL_MSG}"
  log "$msg"
  if [ -n "$SET_TMP" ] && [ -d "$SET_TMP" ]; then        # keep the partial set for diagnosis; the next good run removes it
    write_status "$SET_TMP" false 0 '{}' "$SET_TMP" "$(jq -cn --arg e "$msg" '{error: $e}')" 2>/dev/null
  fi
  notify failed "$msg" 0 "${SET_TMP:-$BACKUP_DIR}" || log "could not post the 'failed' incident"
  exit "$rc"
}
trap 'on_err $? $LINENO' ERR

STEP=preflight
case "$BACKUP_KEEP" in ''|*[!0-9]*|0*) die "BACKUP_KEEP must be a whole number >= 1, got '$BACKUP_KEEP'" ;; esac
for c in docker jq curl tar; do command -v "$c" >/dev/null || die "$c not found on PATH"; done
[ -z "$BACKUP_REMOTE" ] || command -v rclone >/dev/null || die "BACKUP_REMOTE is set but rclone is not on PATH"
mkdir -p "$BACKUP_DIR" || die "cannot create $BACKUP_DIR"
# a failed run leaves <stamp>.partial/ for diagnosis until the next run starts, so failures never pile up on disk
# (older than an hour: a run takes seconds, so a younger one may belong to a run still going)
# shellcheck disable=SC2044  # -name admits only digit-and-dash names, so the word split is safe
for p in $(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +60 \
             -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].partial' -exec basename {} \;); do
  log "remove failed attempt $p"; rm -rf -- "${BACKUP_DIR:?}/$p"
done
STAMP=$(date +%Y%m%d-%H%M)
SET="$BACKUP_DIR/$STAMP"
[ ! -e "$SET" ] || die "$SET already exists (two runs in the same minute?)"
[ ! -e "$SET.partial" ] || die "$SET.partial exists: another backup is running, or one failed this minute"
mkdir "$SET.partial" || die "cannot create $SET.partial (another backup started this minute?)"
SET_TMP="$SET.partial"    # only now: on_err writes into SET_TMP, and it must never touch another run's set
log "start: $SET"

STEP="n8n dump"
docker exec "$N8N_DB" pg_dump -U n8n -Fc n8n >"$SET_TMP/n8n.dump"

STEP="supabase dump"      # postgres (not a superuser, but BYPASSRLS) dumps every schema; the drill checked it
docker exec "$SUPA_DB" pg_dump -U postgres -Fc postgres >"$SET_TMP/supabase.dump"

STEP="workflows export"
docker exec "$N8N_MAIN" n8n export:workflow --all --output=/tmp/rag-backup-workflows.json
docker cp -q "$N8N_MAIN:/tmp/rag-backup-workflows.json" "$SET_TMP/workflows.json"
docker exec "$N8N_MAIN" rm -f /tmp/rag-backup-workflows.json

STEP="open-webui archive"
# webui.db runs in WAL mode and is written while we read: a file copy could catch it mid-checkpoint, so take an online
# backup through SQLite in the running container and archive that in its place. Stopped container = nothing writes.
# A missing container is an error: the volume may then be in use by something we cannot see.
OWU_STATE=$(docker inspect -f '{{.State.Running}} {{.Config.Image}}' "$OWU") ||
  die "container $OWU not found (renamed or removed?): cannot take a consistent copy of webui.db"
OWU_IMAGE=${OWU_STATE#* }
if [ "${OWU_STATE%% *}" = true ]; then
  docker exec "$OWU" python3 -c '
import sqlite3, sys
src = sqlite3.connect("file:/app/backend/data/webui.db?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
ok = dst.execute("pragma quick_check").fetchone()[0]
dst.close(); src.close()
sys.exit(0 if ok == "ok" else "snapshot failed quick_check: " + ok)' /tmp/rag-backup-webui.db
  docker cp -q "$OWU:/tmp/rag-backup-webui.db" "$SET_TMP/.webui.db"
  docker exec "$OWU" rm -f /tmp/rag-backup-webui.db
else
  log "Open WebUI is not running: archiving webui.db as it is"
fi
docker run --rm --network none -v "$OWU_VOL:/d:ro" -v "$SET_TMP:/b" "$TAR_IMAGE" sh -euc '
set -o pipefail; mkdir /s; x=
[ -f /b/.webui.db ] && x="--exclude ./webui.db --exclude ./webui.db-wal --exclude ./webui.db-shm"
tar -C /d -cf - --exclude ./cache $x . | tar -C /s -xf -
[ ! -f /b/.webui.db ] || cp /b/.webui.db /s/webui.db
tar -C /s -czf /b/open-webui-data.tgz .'
rm -f "$SET_TMP/.webui.db"

STEP="storage archive"
docker run --rm --network none -v "$STORAGE_VOL:/d:ro" -v "$SET_TMP:/b" "$TAR_IMAGE" tar -C /d -czf /b/supabase-storage.tgz .

# --- validate every file before the set counts as a backup ---
toc() { docker run --rm --network none -v "$SET_TMP:/b:ro" "$PG_IMAGE" pg_restore --list "/b/$1"; }
need() { case "$1" in *"TABLE DATA $2 "*) ;; *) die "pg_restore --list of $3 shows no data for $2" ;; esac; }
STEP="validate n8n.dump"
T=$(toc n8n.dump); need "$T" "public workflow_entity" n8n.dump; need "$T" "public credentials_entity" n8n.dump
STEP="validate supabase.dump"
T=$(toc supabase.dump)
for t in "public documents" "public page_views" "storage objects"; do need "$T" "$t" supabase.dump; done
STEP="validate workflows.json"
WORKFLOWS=$(jq 'if type == "array" then length else error("not a JSON array") end' "$SET_TMP/workflows.json")
[ "$WORKFLOWS" -ge 1 ] || die "workflows.json holds no workflows"
STEP="validate open-webui-data.tgz"
tar -tzf "$SET_TMP/open-webui-data.tgz" >"$SET_TMP/.list"
grep -qx './webui.db' "$SET_TMP/.list" || die "open-webui-data.tgz has no webui.db"
mkdir "$SET_TMP/.check"; tar -xzf "$SET_TMP/open-webui-data.tgz" -C "$SET_TMP/.check" ./webui.db
# quick_check the archived copy with Open WebUI's own python (WAL-mode file: it needs a writable dir for -shm)
docker run --rm --network none -v "$SET_TMP/.check:/c" --entrypoint python3 "$OWU_IMAGE" -c '
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute("pragma quick_check").fetchone()[0]
sys.exit(0 if r == "ok" else "quick_check: " + r[:200])' /c/webui.db ||
  die "the webui.db in open-webui-data.tgz fails SQLite quick_check"
rm -rf "$SET_TMP/.check"
STEP="validate supabase-storage.tgz"
tar -tzf "$SET_TMP/supabase-storage.tgz" >"$SET_TMP/.list"
STORAGE_FILES=$(grep -c '/document-images/.*[^/]$' "$SET_TMP/.list") || die "supabase-storage.tgz has no document-images files"
rm -f "$SET_TMP/.list"

STEP=finish
BYTES=0; FILES_JSON='{}'
for f in $FILES; do
  b=$(wc -c <"$SET_TMP/$f" | tr -d ' ')
  BYTES=$((BYTES + b)); FILES_JSON=$(jq -cn --argjson o "$FILES_JSON" --arg f "$f" --argjson b "$b" '$o + {($f): $b}')
done
chmod go-rwx "$SET_TMP"/* 2>/dev/null || true      # container-written files arrive 644; the set dir is 700 regardless
write_status "$SET_TMP" true "$BYTES" "$FILES_JSON" "$SET" "{\"workflows\": $WORKFLOWS, \"storage_files\": $STORAGE_FILES}"
mv "$SET_TMP" "$SET"; SET_TMP=
cp "$SET/last_backup.json" "$BACKUP_DIR/last_backup.json"
SIZE=$(awk -v b="$BYTES" 'BEGIN { if (b >= 1073741824) printf "%.1f GB", b / 1073741824; else printf "%.0f MB", b / 1048576 }')
log "set ok: $SIZE, $WORKFLOWS workflows, $STORAGE_FILES storage files, ${SECONDS}s"

STEP=prune                # newest first; only <YYYYMMDD-HHMM> directories count, anything else in BACKUP_DIR is left alone
n=0
# shellcheck disable=SC2012  # ls is fine here: grep lets through only digit-and-dash names
for s in $(ls -1 "$BACKUP_DIR" | { grep -E '^[0-9]{8}-[0-9]{4}$' || true; } | sort -r); do
  n=$((n + 1))
  if [ "$n" -gt "$BACKUP_KEEP" ]; then log "prune $s"; rm -rf -- "${BACKUP_DIR:?}/$s"; fi
done

if [ -n "$BACKUP_REMOTE" ]; then
  STEP="remote copy"
  rclone copy "$SET" "$BACKUP_REMOTE/$STAMP"
fi

STEP=notify
trap - ERR
if ! notify ok "$SIZE in $SET" "$BYTES" "$SET"; then log "the set is good, but the 'ok' incident was not posted"; exit 3; fi
log "done in ${SECONDS}s"
