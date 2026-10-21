#!/bin/bash
# forget-user.sh — erase one person's conversations everywhere this system keeps them
# (GDPR Art. 17 "right to erasure"). Records THAT it happened in privacy_log (hashes + counts), never content.
#
#   ./forget-user.sh someone@example.com --dry-run          show what would be removed, change nothing
#   ./forget-user.sh someone@example.com                    remove their conversations, keep their login
#   ./forget-user.sh someone@example.com --delete-account   also remove their Open WebUI login
#
# Removes: Open WebUI chats (+ messages, shared links) · n8n chat memory · n8n run records that contain
# their sessions · their rows in usage_log. Not reachable from here: Azure's abuse-monitoring copies of
# prompts (kept up to 30 days on Microsoft's side, under the Microsoft agreement).
set -euo pipefail
EMAIL="${1:?usage: forget-user.sh <email> [--dry-run] [--delete-account]}"; shift || true
DRY=false; DEL_ACCOUNT=false
for a in "$@"; do case "$a" in --dry-run) DRY=true;; --delete-account) DEL_ACCOUNT=true;; *) echo "unknown option: $a" >&2; exit 2;; esac; done

OWU=rag-openwebui-open-webui-1; SUPA=supabase-db; N8NDB=rag-n8n-postgres-1

# 1) Open WebUI — find the person, list their chat IDs (needed for older memory), then delete
OWU_JSON=$(docker exec -e PYTHONPATH=/app/backend -e EMAIL="$EMAIL" -e DRY="$DRY" -e DEL_ACCOUNT="$DEL_ACCOUNT" \
  -w /app/backend -i "$OWU" python - 2>/dev/null <<'PY' | tail -1
import asyncio, json, os
from sqlalchemy import select
from open_webui.internal.db import get_async_db_context
from open_webui.models.auths import Auths
from open_webui.models.chats import Chat, Chats
from open_webui.models.users import Users
async def main():
    user = await Users.get_user_by_email(os.environ['EMAIL'].strip().lower())
    if not user:
        print(json.dumps({'found': False})); return
    async with get_async_db_context() as s:
        chat_ids = (await s.execute(select(Chat.id).where(Chat.user_id == user.id))).scalars().all()
    if os.environ['DRY'] != 'true':
        await Chats.delete_chats_by_user_id(user.id)
        if os.environ['DEL_ACCOUNT'] == 'true':
            await Auths.delete_auth_by_id(user.id)
    print(json.dumps({'found': True, 'user_id': user.id, 'chat_ids': list(chat_ids)}))
asyncio.run(main())
PY
)
[ "$(echo "$OWU_JSON" | jq -r .found)" = "true" ] || { echo "No Open WebUI account for $EMAIL — nothing to do."; exit 1; }
USER_ID=$(echo "$OWU_JSON" | jq -r .user_id)
CHAT_IDS=$(echo "$OWU_JSON" | jq -r '.chat_ids | join(",")')
N_CHATS=$(echo "$OWU_JSON" | jq -r '.chat_ids | length')
[[ "$USER_ID" =~ ^[0-9a-f-]{36}$ ]] || { echo "unexpected user id format: $USER_ID" >&2; exit 1; }

# Sessions are "<user id>:<chat id>" (current pipe) or just "<chat id>" (chats from before that change)
SQL_SESSIONS="session_id like '${USER_ID}:%'"
quote_list() { echo "'$(echo "$1" | sed "s/,/','/g")'"; }   # a,b,c -> 'a','b','c' (ids are uuids)
[ -n "$CHAT_IDS" ] && SQL_SESSIONS="$SQL_SESSIONS or session_id in ($(quote_list "$CHAT_IDS"))"
ID_REGEX="${USER_ID}${CHAT_IDS:+|${CHAT_IDS//,/|}}"
USER_HASH=$(printf %s "$USER_ID" | shasum -a 256 | cut -c1-16)
EMAIL_HASH=$(printf %s "$(echo "$EMAIL" | tr 'A-Z' 'a-z' | xargs)" | shasum -a 256 | cut -c1-64)

q_supa() { docker exec "$SUPA" psql -U postgres -d postgres -tAc "$1"; }
q_n8n()  { docker exec "$N8NDB" psql -U n8n -d n8n -tAc "$1"; }

N_MEM=$(q_supa "select count(*) from n8n_chat_histories where $SQL_SESSIONS")
N_USAGE=$(q_supa "select count(*) from usage_log where user_hash = '$USER_HASH'")
EXEC_IDS=$(q_n8n "select coalesce(string_agg(\"executionId\"::text, ','), '') from execution_data where data ~ '($ID_REGEX)'")
N_EXEC=$( [ -n "$EXEC_IDS" ] && echo "$EXEC_IDS" | tr ',' '\n' | wc -l | xargs || echo 0)

if [ "$DRY" = "true" ]; then
  echo "DRY RUN for $EMAIL (nothing changed):"
else
  q_supa "delete from n8n_chat_histories where $SQL_SESSIONS" >/dev/null
  q_supa "delete from usage_log where user_hash = '$USER_HASH'" >/dev/null
  if [ -n "$EXEC_IDS" ]; then
    q_n8n "delete from binary_data where \"sourceType\"='execution' and \"sourceId\" in ($(quote_list "$EXEC_IDS"))" >/dev/null
    q_n8n "delete from execution_entity where id in (${EXEC_IDS})" >/dev/null
  fi
  q_supa "insert into privacy_log (action, subject, details) values ('forget_user', '$EMAIL_HASH',
          jsonb_build_object('open_webui_chats', $N_CHATS, 'memory_messages', $N_MEM, 'n8n_runs', $N_EXEC,
                             'usage_rows', $N_USAGE, 'account_deleted', $DEL_ACCOUNT))" >/dev/null
  echo "Forgot $EMAIL:"
fi
echo "  Open WebUI chats ........ $N_CHATS$( [ "$DEL_ACCOUNT" = true ] && echo ' (+ login removed)')"
echo "  n8n chat memory ......... $N_MEM message(s)"
echo "  n8n run records ......... $N_EXEC"
echo "  usage_log rows .......... $N_USAGE"
[ "$DRY" = "true" ] || echo "  logged in privacy_log (email hash + counts only)"
