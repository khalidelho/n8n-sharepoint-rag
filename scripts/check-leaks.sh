#!/usr/bin/env bash
# Refuses to let anything private into the repo. Used as the pre-commit hook (.githooks/pre-commit).
#
#   scripts/check-leaks.sh            scan the working tree (tracked + new files)
#   scripts/check-leaks.sh --staged   scan exactly what is about to be committed
#
# Two lists:
#   1. generic secret/identifier shapes (below) — fine to publish
#   2. a private deny list of words and IDs (employer, tenant, site, resource names, chat IDs)
#      in ${PRIVATE_RAG_DENYLIST:-~/.config/private-rag/denylist.txt} — kept OUTSIDE the repo,
#      because the list itself is what must not be published
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

scope=(--untracked); [ "${1:-}" = "--staged" ] && scope=(--cached)
deny="${PRIVATE_RAG_DENYLIST:-$HOME/.config/private-rag/denylist.txt}"
found=0

guid='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
generic=(
  'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'              # JWTs (Supabase anon/service keys)
  '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'                        # Telegram bot tokens
  'sk-[A-Za-z0-9_-]{20,}'                                   # API keys
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  '[A-Za-z0-9-]+\.sharepoint\.com'                          # a real tenant
  '[a-z0-9-]+\.(services\.ai|openai|cognitiveservices)\.azure\.com'   # a real Azure resource
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z.]{2,}'          # e-mail addresses
  '(^|[^0-9a-fA-F])[0-9a-fA-F]{32}([^0-9a-fA-F]|$)'                  # 32-hex keys
  ".{0,14}$guid"                                            # GUIDs (tenant, site, list, drive IDs)
)
# Allowed: Microsoft's sample tenant, example domains, GitHub noreply, and n8n's own random
# node / webhook IDs (a GUID that is the whole value of an "id" or "webhookId" key).
allow="contoso\\.sharepoint\\.com|example\\.(com|org)|users\\.noreply\\.github\\.com|\"(id|webhookId)\": \"$guid\$"

for pat in "${generic[@]}"; do
  # -o: judge only the matched text, so an allowed value elsewhere on a long line can't hide a leak
  if hits=$(git grep "${scope[@]}" -noIE -e "$pat" -- . ':!scripts/check-leaks.sh' | grep -vE "$allow"); then
    echo "✗ looks like a secret or identifier ($pat):"; echo "$hits" | sed 's/^/    /'; found=1
  fi
done

if [ -f "$deny" ]; then
  if hits=$(grep -vE '^\s*(#|$)' "$deny" | git grep "${scope[@]}" -nIiE -f /dev/stdin -- .); then
    echo "✗ matches the private deny list:"; echo "$hits" | sed 's/^/    /'; found=1
  fi
elif [ "${ALLOW_NO_DENYLIST:-}" != "1" ]; then
  echo "✗ private deny list not found at $deny — refusing (set ALLOW_NO_DENYLIST=1 to skip it)"; found=1
fi

[ $found -eq 0 ] && echo "✓ no leaks found" || { echo; echo "Fix the lines above (or use placeholders) and try again."; exit 1; }
