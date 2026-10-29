# Runbook

What to do when the stack tells you something is wrong. Every Telegram alert from **RAG – Error reporter** ends with
`Runbook: <name>`, and `<name>` is a section below. The 8:00 digest (**RAG – Daily health**) points here too when it
warns. Commands assume the container names from `stack/` and are run from the repo root.

Sections: Chat agent errors · Ingestion errors · Workflow errors · Eval drop · Service down · Backup failed · Restore ·
Operating notes.

## How the alerts work

```
🔴 RAG incident · Chat agent (vision) · node "AI Agent" · Tue Oct 27 14:03 PT
Azure timeout after 60000 ms
ref 48213 · http://localhost:5680/workflow/ragChatVision001/executions/48213  (+3 more in the last 30 min)
Runbook: Chat agent errors
```

- **Line 1:** what failed (workflow, node, kind of error for incidents that don't come from n8n's error workflow) and when.
- **Line 2:** the error text, cut before anything that could quote the user's question, so it can end early.
- **Line 3:** the ref and a link to the failed run in the editor. The ref is the number the user saw; pipe refs look
  like `p-7f3a`.
- **"+N more":** the same thing failed N more times. At most one message per workflow, node and kind every 30
  minutes; the rest are counted. The digest counts every incident of the last 24 hours and says "N not alerted on
  Telegram" for any that never reached you.

Every incident is a row in `incident_log` (kept 365 days):

```sh
docker exec supabase-db psql -U postgres -d postgres -c "select id, at, source, workflow, node, kind, ref, left(message, 100)
  from incident_log where at > now() - interval '24 hours' order by at desc"
```

The digest counts only unresolved incidents. Mark the ones you've dealt with:

```sh
docker exec supabase-db psql -U postgres -d postgres -c "update incident_log set resolved_at = now() where id in (101, 102)"
```

## Chat agent errors

A chat agent run failed, or the agent, *Look at pages* or the Open WebUI pipe reported a problem of its own.

| Kind | Means |
|---|---|
| an error class such as `NodeApiError` | the run failed at the named node (reported by n8n's error workflow) |
| `zoom_failed`, `truncated_pass2` | *Look at pages* couldn't read the close-up; the first look went out, labelled approximate |
| `truncated_pass1`, `empty_reply`, `vision_error` | *Look at pages* couldn't read the pages at all; the agent was told to say so, not to guess |
| `offline`, `timeout`, `http`, `empty`, `forbidden`, `error` (source pipe) | Open WebUI couldn't get an answer from n8n (see `open-webui/functions/README.md`) |

**Check first.**
1. Open the link on line 3: the failed node is red, with its error.
2. A user quotes a `p-` ref you have no alert for: the pipe doesn't report n8n's 5xx answers (n8n's error workflow
   already alerted under the execution id), and a `forbidden` report can't get through. Look in Open WebUI's log:
   `docker logs rag-openwebui-open-webui-1 2>&1 | grep 'p-7f3a'`.
3. Several alerts at once from different nodes: something the agent needs is down. See "Service down".
4. Azure 429: the deployment's tokens-per-minute limit. An eval run or a bulk ingestion at the same time is the usual cause.

**Fix.**
- A single timeout, 429 or Azure 5xx: nothing to do; the user asks again.
- Azure rejects the key: put a working key into the n8n credential (n8n → Credentials), not into a file.
- `forbidden`: the pipe's token no longer matches `RAG_WEBHOOK_TOKEN` in `stack/n8n/.env`. Compare them without
  showing either one (prints only "tokens match" or "tokens DIFFER"):
  ```sh
  a=$(docker exec rag-openwebui-open-webui-1 python3 -c "import sqlite3,json,hashlib; c=sqlite3.connect('file:/app/backend/data/webui.db?mode=ro', uri=True); v=json.loads(c.execute(\"select valves from function where id='knowledge_assistant'\").fetchone()[0] or '{}'); print(hashlib.sha256(str(v.get('n8n_token','')).encode()).hexdigest())")
  b=$(sed -n 's/^RAG_WEBHOOK_TOKEN=//p' stack/n8n/.env | tr -d '"\r\n' | shasum -a 256 | cut -d' ' -f1)
  [ "$a" = "$b" ] && echo "tokens match" || echo "tokens DIFFER"; unset a b
  ```
  If they differ, copy the token into the valve with a key-only update inside the container. It reads the token from
  `.env` on stdin, changes only `n8n_token`, prints key names only, and needs no restart:
  ```sh
  sed -n 's/^RAG_WEBHOOK_TOKEN=//p' stack/n8n/.env | tr -d '"\r\n' |
    docker exec -i rag-openwebui-open-webui-1 python3 -c 'import json,sqlite3,sys; t=sys.stdin.read() or sys.exit("no token read: nothing changed"); con=sqlite3.connect("/app/backend/data/webui.db", timeout=30); row=con.execute("select valves from function where id=?", ("knowledge_assistant",)).fetchone() or sys.exit("no knowledge_assistant function: nothing changed"); v=json.loads(row[0] or "{}"); v["n8n_token"]=t; con.execute("update function set valves=? where id=?", (json.dumps(v), "knowledge_assistant")); con.commit(); print("valve keys:", sorted(v))'
  ```
- The errors started right after a change: roll it back (Operating notes).

**Resolves itself when** the passing problem passes; while something keeps failing you get one message per 30 minutes.

## Ingestion errors

A run of **RAG – Ingestion** (every 15 minutes) failed as a whole, so that batch of files wasn't finished. Problems
with a single file don't alert: they are logged in `ingestion_log` and shown in the digest ("now failing", "retrying").

**Check first.** The link on line 3. Graph or SharePoint nodes with 401/403: the Microsoft app credential (expired
client secret?). Mistral OCR, embeddings or chunking with 429/5xx: Azure. A Postgres node: Supabase. A Gotenberg node:
`docker logs --tail 50 rag-n8n-gotenberg-1`. What ingestion did lately:

```sh
docker exec supabase-db psql -U postgres -d postgres -c "select logged_at, step, status, left(file_name, 40), left(message, 80)
  from ingestion_log order by id desc limit 20"
```

**Fix.** Renew an expired credential and wait for the next run. If one file breaks every run, fix or rename it in
SharePoint (`OLD_` files and "Do Not Use" folders are skipped). **Resolves itself when** the next run succeeds: a
half-done batch is simply redone, and every run clears a file's old passages first, so nothing is duplicated.

## Workflow errors

Any other RAG workflow failed: Web search, Search (reranked), Page search, Look at pages, Page index, Vision health,
Daily health or Judge. The search tools failing means the agent answered without that tool (if the agent failed too,
there's also a "Chat agent errors" alert); the scheduled ones are invisible to users. If several fail within minutes,
they share something that is down: Supabase, Azure, the vision service (port 18500) or the reranker (port 18501).
See "Service down". Scheduled workflows try again on their next run; tools on the next question.

## Eval drop

Sent by the optional `evals` service after its weekly run (or when a run couldn't finish). The digest's Evals line
also says "⚠️ below floor" until a run of that set passes, and "⚠️ overdue" when the latest run is over 8 days old.
Every miss is asked once more before the floor is checked.

**Check first.** The results file named in the alert (which ids failed, and what the answers said); what changed
since the last good run (a workflow change, changed documents, an Azure model update); the recent runs:

```sh
docker exec supabase-db psql -U postgres -d postgres -c "select at, set_name, passed, total, failed_ids from eval_run order by at desc limit 9"
```

**Fix.** Roll back a change that caused it. If a document really changed, update that question's expected answer.
Then run the set again by hand: `docker compose -f stack/n8n/compose.yml run --rm evals python /evals/scheduled.py messy`.

## Service down

Nothing in this repo watches the stack from outside n8n, so **a missing 8:00 digest is the signal** that n8n, Docker
or Supabase is down. Otherwise a user's report or a chat agent alert is the first sign.

**Effects.** n8n down: the pipe's "offline" line. Supabase or Azure down: the agent fails with a ref line. The vision
service down: answers about drawings say visual search was unavailable. The reranker down: search still works, with
GPT reranking. Open WebUI down: the site doesn't load.

**Check first.**
1. `docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'rag-|supabase'`: unhealthy, restarting or exited?
2. `docker logs --tail 100 <container>` for the one that is down.
3. Host services: `launchctl list | grep -E 'rag-vision|rag-rerank'` (a number in the first column means it runs);
   logs in `~/Library/Logs/`.
4. Disk space: `df -h ~` and Docker Desktop's disk usage.

**Fix.** A stopped stack: `docker compose up -d` in its folder (never with `--remove-orphans` unless you know every
container it would remove). A host service: `launchctl kickstart -k gui/$(id -u)/com.example.rag-vision` (or
`com.example.rag-rerank`). Docker Desktop not running: start it; the containers come back on their own.

## Backup failed

Sent by `ops/backup.sh` (nightly, 03:40) when a run fails. The digest's Backup line also warns once the last good
backup is more than 36 hours old. Nothing was pruned: older sets are intact.

**Check first.** `tail -50 ~/Library/Logs/rag-backup.log`; the failed attempt is kept as
`~/rag-backups/<stamp>.partial/` with the error in its `last_backup.json`;
`launchctl print gui/$(id -u)/com.example.rag-backup | grep -E 'state|last exit'`; Docker running, disk space.

**Fix** the cause, then run it once by hand (about 10 s; it posts "ok", so the digest picks it up):
`/bin/bash ops/backup.sh; echo "exit $?"`. Exit 3 means the set is good but the "ok" report didn't reach n8n.

## Restore

### Before you ever need it

Backups hold **no `.env` files or keys**, by design. Without them a restore comes back unusable, so keep a current copy
of each in a password manager (or an encrypted disk image whose passphrase is there), and update it when a file changes:

| File | Why a restore needs it |
|---|---|
| `stack/n8n/.env` | `N8N_ENCRYPTION_KEY`: without it every credential in `n8n.dump` is unreadable. Also the webhook token and DB password |
| Supabase's `.env` | `POSTGRES_PASSWORD`, JWT secret, API keys, `COMPOSE_FILE` (the macOS storage override) |
| `stack/open-webui/.env` | `WEBUI_SECRET_KEY` (sessions, stored tokens) |
| `vision/.env` | `PG_DSN` with the `rag_vision` password |

Also keep the `rag_reader` password: Postgres roles are cluster-wide, so they're not in `supabase.dump`.

### What a backup set holds

`~/rag-backups/<YYYYMMDD-HHMM>/` (14 kept; `~/rag-backups/last_backup.json` describes the newest good one):

| File | Holds | Restores into |
|---|---|---|
| `n8n.dump` | n8n Postgres: workflows, credentials (encrypted), executions | `rag-n8n-postgres-1`, db `n8n` |
| `supabase.dump` | Supabase Postgres, every schema: documents, page views, rows, chat memory, incidents, `auth`, `storage.objects` rows | `supabase-db`, db `postgres` |
| `workflows.json` | `n8n export:workflow --all`, to bring back one workflow without a full restore | n8n import |
| `open-webui-data.tgz` | `webui.db` (an online-backup snapshot), `uploads/`, `vector_db/` (not `cache/`) | volume `rag-openwebui_open-webui-data` |
| `supabase-storage.tgz` | the `document-images` bucket files | volume `supabase_storage-data` |

**Treat every set as sensitive:** encrypted n8n credentials (the key is one leaked `.env` away), password hashes, chat
history. Sets are mode 700/600; if you set `BACKUP_REMOTE`, point it at an **rclone crypt** remote. Not backed up: the
`.env` files, the vision page images (rebuilt by the vision service from the source documents), Supabase's analytics
internals.

### Full restore onto a fresh host

Each database is restored **before** the services that migrate it start.

1. **Code and secrets:** clone this repo, put the `.env` files back.
2. **Supabase database.** Start the Supabase stack once (so every service creates its schemas and roles), then stop
   everything but the database: `docker compose up -d --wait && docker compose stop && docker compose start db` (in
   your Supabase folder). Recreate the app's login roles, which aren't in the dump:
   ```sh
   docker exec -i supabase-db psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 <<'SQL'
   create role rag_reader login; create role rag_vision login;
   alter role rag_reader set default_transaction_read_only = on; alter role rag_reader set statement_timeout = '15s';
   alter role rag_vision set statement_timeout = '60s'; alter role rag_vision set search_path = public, extensions;
   SQL
   docker exec -it supabase-db psql -U supabase_admin -d postgres -c '\password rag_reader'
   docker exec -it supabase-db psql -U supabase_admin -d postgres -c '\password rag_vision'
   ```
   Restore as `supabase_admin` (`postgres` isn't a superuser and can't replace `auth`/`storage` objects):
   ```sh
   SET=~/rag-backups/<stamp>
   docker cp "$SET/supabase.dump" supabase-db:/tmp/supabase.dump
   docker exec supabase-db pg_restore -U supabase_admin -d postgres --clean --if-exists /tmp/supabase.dump
   docker exec supabase-db rm /tmp/supabase.dump
   ```
   An error that only says `role "…" does not exist` means a role was missing: create it and run the restore again.
3. **Storage files** (Supabase still stopped):
   `docker run --rm -v supabase_storage-data:/d -v "$SET":/b:ro alpine:3.20 tar xzf /b/supabase-storage.tgz -C /d`,
   then `docker compose start` in the Supabase folder.
4. **n8n.** Start only its Postgres and restore before n8n runs its migrations:
   ```sh
   cd stack/n8n && docker compose up -d --wait postgres
   docker cp "$SET/n8n.dump" rag-n8n-postgres-1:/tmp/n8n.dump
   docker exec rag-n8n-postgres-1 pg_restore -U n8n -d n8n --exit-on-error /tmp/n8n.dump
   docker exec rag-n8n-postgres-1 rm /tmp/n8n.dump
   docker compose up -d
   ```
5. **Open WebUI.** Create the volume without starting the app, fill it, start. Always delete a leftover
   `webui.db-wal`/`-shm` first: a stale WAL next to a restored `webui.db` corrupts it.
   ```sh
   cd stack/open-webui && docker compose create
   docker run --rm -v rag-openwebui_open-webui-data:/d -v "$SET":/b:ro alpine:3.20 \
     sh -c 'rm -f /d/webui.db /d/webui.db-wal /d/webui.db-shm && tar xzf /b/open-webui-data.tgz -C /d'
   docker compose up -d
   ```
6. **Host services:** load the vision, reranker and backup LaunchAgents again.

**One workflow** (an edit went wrong): import it from `workflows.json` (the import overwrites the workflow with the same
id, and unpublishes it: publish it again and restart n8n).

### After any restore

```sh
docker exec supabase-db psql -U postgres -d postgres -tAc \
  "select (select count(*) from documents), (select count(*) from page_views), (select count(*) from storage.objects)"
docker exec rag-n8n-postgres-1 psql -U n8n -d n8n -tAc "select count(*) from workflow_entity"
```

Then open any credential in n8n (if it can't decrypt, the `.env` key is wrong), ask one how-to question and click a
screenshot, and run Daily health by hand to see the Telegram message arrive. Practise this once on throwaway
containers (`--network none`) before you need it.

## Operating notes

- **After any import or rollback, check the error workflow.** Every active RAG workflow except the reporter must point
  at it, or its failures stop alerting without a word. Expect no output:
  ```sh
  docker exec rag-n8n-postgres-1 psql -U n8n -d n8n -At -c "select id, name from workflow_entity where active and id <> 'ragErrorReport01' and coalesce(settings->>'errorWorkflow', '') <> 'ragErrorReport01'"
  ```
- **Deploy between ingestion ticks** (:00/:15/:30/:45 start runs) and when n8n is idle; restarting n8n takes the chat
  webhook down for a minute or two.
- **Rollbacks by version id expire.** n8n prunes non-current workflow versions after about a day on an unlicensed
  instance, so keep rollback files, and check that a backup file is the *published* version (`n8n export:workflow`
  saves the editor draft; `scripts/published_versions.py` shows how to read the published one).
- **After `n8n execute`** from the CLI, the execution row stays `running` (the CLI exits before n8n's cleanup). Delete
  that one row, or the next restart marks it crashed and you get a false alert.
- **Open WebUI valves:** never open the pipe's valves with the gear icon (it shows the token in clear text); use the
  comparison and key-only update under "Chat agent errors".
- **Secrets:** never print environments or `.env` files (`env`, `docker exec … env`, `docker inspect` without a
  `--format`, `docker compose config`, `cat .env`). Ask for one named variable and mask it.
