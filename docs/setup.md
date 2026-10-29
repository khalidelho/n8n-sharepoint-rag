# Setup

Tested on an Apple Silicon Mac (Docker Desktop, 12 GB memory for Docker). The containers work the same on Linux; the
two local services (vision and reranker) are written for macOS with launchd and the Apple GPU, see step 5.

**You need:** Docker with Compose · a Microsoft 365 tenant with a SharePoint document library · an Azure
subscription · Homebrew (`tesseract`, `llama.cpp`) and Python 3.12 for the local services · optionally a Telegram bot
for the daily digest and alerts.

## 1. Supabase

1. Follow Supabase's [self-hosting with Docker](https://supabase.com/docs/guides/self-hosting/docker)
   guide: copy the `docker/` folder, create `.env` from `.env.example`, generate the secrets, start it.
   Studio and the API gateway are on `http://localhost:8000`; the Postgres pooler on port 5432.
2. **macOS only:** copy `stack/supabase/docker-compose.macos-storage.yml` into the Supabase folder and add
   it to the stack (`docker compose -f docker-compose.yml -f docker-compose.macos-storage.yml up -d`, or
   `sh run.sh config add macos-storage` in newer bundles). Storage needs a filesystem with extended
   attributes, which Mac bind mounts don't have.
3. In Studio → Storage, create a **public** bucket named `document-images`.

## 2. Database

```sh
docker exec -i supabase-db psql -U postgres -d postgres < sql/01_schema.sql
docker exec -i supabase-db psql -U postgres -d postgres -v pw="$RAG_READER_PASSWORD" < sql/02_agent_access.sql
docker exec -i supabase-db psql -U postgres -d postgres < sql/03_retention_usage.sql
docker exec -i supabase-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -v dims=768 < sql/04_page_views.sql
docker exec -i supabase-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 < sql/05_library_vocabulary.sql
docker exec -i supabase-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 < sql/06_qa_incidents.sql
docker exec -it supabase-db psql -U postgres -d postgres -c '\password rag_vision'    # the vision service's login
```

`RAG_READER_PASSWORD` is the value you put in `stack/n8n/.env` (next step). `dims=768` matches the default vision
embedding model (`gemma`); use 1536 for `vdr` or 2048 for `qwen`. 04 adds the page index for visual search, 05 the
library vocabulary the question router uses to read typos, 06 the incident, answer-check and eval tables of the error
flow.

## 3. n8n and Open WebUI

```sh
cd stack/n8n        && cp .env.example .env && $EDITOR .env && docker compose up -d   # http://localhost:5680
cd ../open-webui    && cp .env.example .env && $EDITOR .env && docker compose up -d   # http://localhost:3000
```

Create the owner account in n8n and the admin account in Open WebUI. The n8n stack also defines an optional
`evals` service (compose profile `evals`, see "Operating it"); plain `docker compose up -d` leaves it off.

## 4. Azure

1. **Azure AI Foundry:** create a resource and deploy `text-embedding-3-small`, `gpt-5.4-mini` and
   `mistral-document-ai-2512` (Data Zone Standard keeps processing in the US). Name each deployment after
   its model, or change the names in the workflows. Note the resource host
   (`<resource>.services.ai.azure.com`) and key. `gpt-5.4-mini` also reads page images for visual search.
2. **App registration** (Microsoft Entra ID) for reading SharePoint:
   - Redirect URI: `http://localhost:5680/rest/oauth2-credential/callback`
   - Delegated permissions: `Sites.Read.All`, `Files.Read.All`, `offline_access`, `openid`.
     The workflows only read; this build was tested with the ReadWrite variants, but read-only is enough.
   - n8n's Microsoft credential signs in through the `/common` endpoint, so either make the app
     multitenant or replace `common` with your tenant ID in the credential's URLs.

## 5. Local services: vision and reranker

Both run natively on the Mac (not in Docker, so they can use the GPU), listen on 127.0.0.1 only, and are reached
from n8n at `host.docker.internal`. Docker Desktop forwards that to the Mac's loopback; on Linux, bind them to the
Docker bridge address instead.

**Vision service** (`vision/`, port 18500): renders and embeds every page for visual search, serves page images and
crops to the *Look at pages* workflow, and matches text passages to their page numbers.

```sh
brew install tesseract
python3.12 -m venv vision/.venv && vision/.venv/bin/pip install -r vision/requirements.txt
cp vision/.env.example vision/.env && chmod 600 vision/.env && $EDITOR vision/.env   # PG_DSN with the rag_vision password
(cd vision && .venv/bin/python service.py)    # first run downloads the model; stop it with Ctrl-C once it listens
sed -e "s#__REPO__#$PWD#g" -e "s#__HOME__#$HOME#g" vision/com.example.rag-vision.plist \
  > ~/Library/LaunchAgents/com.example.rag-vision.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.rag-vision.plist
curl -s localhost:18500/status                # {"model": "gemma", "dims": 768, "queued": 0, ...}
```

If the model's Hugging Face page asks you to accept its terms, do that and sign in (`hf auth login`) before the first
run. The service refuses requests that carry a browser `Origin` header, so no web page can call it.

**Reranker** (port 18501): `bge-reranker-v2-m3` in llama.cpp's `llama-server`. *Search (reranked)* sends it the 25
vector-search candidates and keeps the best four; GPT decides instead when it is down or keeps nothing.

```sh
brew install llama.cpp
# put a Q8_0 GGUF conversion of BAAI/bge-reranker-v2-m3 (~600 MB, from Hugging Face) at:
#   rerank/bge-reranker-v2-m3-Q8_0.gguf
sed -e "s#__REPO__#$PWD#g" -e "s#__HOME__#$HOME#g" rerank/com.example.rag-rerank.plist \
  > ~/Library/LaunchAgents/com.example.rag-rerank.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.rag-rerank.plist
curl -s localhost:18501/v1/rerank -d '{"query":"pipe","documents":["pipe sizing","lunch menu"]}'
```

## 6. Credentials in n8n

Create these with **exactly these names** — the workflows refer to credentials by name.

| Name | Type | Settings |
|---|---|---|
| Microsoft Graph – RAG | Microsoft OAuth2 API | client ID + secret from the app registration; scope `Files.Read.All Sites.Read.All offline_access openid` |
| Azure AI – RAG | Azure OpenAI | endpoint type **Foundry**, endpoint `https://<resource>.services.ai.azure.com/openai/v1`, Foundry key |
| Mistral OCR – RAG | Header Auth | name `Authorization`, value `Bearer <Foundry key>` |
| Supabase API – RAG | Supabase API | host `http://host.docker.internal:8000`, `SERVICE_ROLE_KEY` from Supabase's `.env` |
| Supabase DB – RAG | Postgres | host `host.docker.internal`, port 5432, db `postgres`, user `postgres.<POOLER_TENANT_ID>`, Supabase's `POSTGRES_PASSWORD` |
| Supabase DB – read-only (agent) | Postgres | same, user `rag_reader.<POOLER_TENANT_ID>`, `RAG_READER_PASSWORD` |
| RAG webhook token | Header Auth | name `Authorization`, value `Bearer <RAG_WEBHOOK_TOKEN>` |
| n8n internal DB (run checks) | Postgres | host `postgres`, db `n8n`, user `n8n`, `N8N_DB_PASSWORD` |
| Telegram – health bot | Telegram API | token from @BotFather |

## 7. Workflows

```sh
cp workflows.env.example workflows.env && $EDITOR workflows.env
python3 scripts/configure_workflows.py          # writes build/workflows/*.json with your values
```

Import them (n8n → Workflows → Import from file) in this order, then open each one, check every node shows its
credential, and **publish** it:

1. `error-reporter`: every other workflow names it as its *error workflow* (Settings → Error workflow), and the pipe,
   the backup script, *Look at pages* and the evals post to its incident webhook.
2. `judge` (grades eval answers), `search-reranked`, `web-search-allow-listed`, `page-search`, `look-at-pages`: the
   agent's tools must be published before the agent can call them.
3. `chat-agent-vision`: the agent, on `POST /webhook/rag-chat`.
4. `ingestion`, `page-index`, `vision-health`, `daily-health`: the scheduled ones.
5. Optional: `chat-agent`, the earlier text-only agent, on `/webhook/rag-chat-v1`. Keep it as a fallback; to switch,
   swap the two webhook paths.

If you import with the CLI instead (`n8n import:workflow`), imports arrive unpublished: run
`n8n publish:workflow --id=<id>` for each and restart the n8n containers. After any import, check that every active
workflow still points at the error reporter ([runbook](runbook.md), Operating notes).

## 7b. Web search (optional — to skip it, delete the `search_web` node from the chat agent)

`RAG – Web search (allow-listed)` calls the Azure OpenAI Responses API (`/openai/v1/responses`) with the
`web_search` tool, using the same **Azure AI – RAG** credential. Edit `ALLOWED_DOMAINS` in its *Build web search
request* node (max 20 domains): add your state environmental agency, your state's regulations site and your
local public-works site to the federal defaults. Only a short model-written search query leaves Azure; the agent
is instructed never to put document text, file names, project names or people in a query. The same workflow serves two
scopes: `verified` (the allow-list, used by the agent's `search_web`) and `open` (no filter, `search_open_web`); its last
step sorts every result bullet into `verified_findings` / `wider_findings` by its cited link, and the chat agent's
*Append web sections* step renders them. The chat agent also runs a small *question router* before the agent (four
nodes before the agent, including the library-vocabulary lookup from `sql/05`) — no setup beyond the Azure credential.
Note: `web_search_preview` does not accept domain filters — use `web_search`.

## 8. Open WebUI functions

See [open-webui/functions/README.md](../open-webui/functions/README.md):

- **Knowledge Assistant** (pipe): Admin Panel → Functions → **+** → paste `open-webui/functions/knowledge_assistant.py`,
  keep the id `knowledge_assistant`, save, set its `n8n_token` valve to your `RAG_WEBHOOK_TOKEN` once, and enable it.
  Later updates go through `open-webui/ops/install_function.py`, which leaves the valves alone.
- **Download as Word** (action): paste `open-webui/functions/download_word.py` the same way, enable it and make it
  Global.

## 9. First run

Run **RAG – Ingestion** once with *Execute workflow*, then leave the schedule on: each run processes up to
4 files (set the schedule to every 2 minutes by hand to clear a big backlog). Watch progress with:

```sql
select step, status, count(*) from ingestion_log group by 1, 2;
```

**RAG – Page index** follows on its own every 15 minutes (6 files per run) and fills `page_views` for visual search;
`curl -s localhost:18500/status` shows the vision service's queue.

## Operating it

- **Alerts and the daily digest:** every failure becomes one Telegram message with a ref (throttled to one per
  workflow, node and kind per 30 minutes) and an `incident_log` row; the user sees a plain line with the same ref. The
  08:00 digest (`RAG – Daily health`) adds incidents, eval scores and backup age to the library and usage numbers.
  *Vision health* checks the vision service every 2 hours. What to do for each alert: [docs/runbook.md](runbook.md).
- **Backups:** `ops/backup.sh` (nightly via `ops/launchd/com.example.rag-backup.plist`, same `sed` install as in
  step 5) writes one validated set to `~/rag-backups/` and keeps 14. Keep the `.env` files in a password manager:
  they are deliberately not in the backup. Restore steps are in the runbook.
- **Scheduled evals (optional):** bring your own question sets and runner into `evals/` (git-ignored; see the comment on
  the `evals` service in `stack/n8n/compose.yml`), then `docker compose --profile evals up -d evals` in `stack/n8n`.
- **Is everything indexed?** `GRAPH_CREDENTIAL_ID=<id> python3 ops/check-completeness.py -v` lists the library through
  Graph and compares it with `document_metadata`.
- **Erase a person:** `ops/forget-user.sh someone@example.com --dry-run`, then without `--dry-run`
  (add `--delete-account` to remove their login too).
- **Retention:** 90 days for chats, agent memory and answer checks, 1 year for logs, incidents and eval runs — change
  `RETENTION_DAYS` in `stack/open-webui/compose.yml` and the intervals in `sql/03_retention_usage.sql` and
  `sql/06_qa_incidents.sql`.
- **Tests** (no live stack needed): `node --test n8n/js/` · `python3 -m unittest ops/tests/test_backup.py` ·
  the two Open WebUI suites in `open-webui/functions/README.md`.
- **Updating this repo from a running instance:** `scripts/export-from-n8n.sh` exports the workflows, swaps each
  editor draft for the **published** version (read-only queries on n8n's database), drops inactive workflows, test
  copies and anything listed in `scripts/export-skip.txt`, cleans them and leak-checks; review `git diff`, then commit.
