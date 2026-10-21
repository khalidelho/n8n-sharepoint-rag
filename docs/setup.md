# Setup

Tested on macOS (Docker Desktop, 12 GB memory for Docker). Linux works the same; skip the macOS-only step.

**You need:** Docker with Compose · a Microsoft 365 tenant with a SharePoint document library · an Azure
subscription · optionally a Telegram bot for the daily health message.

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
```

`RAG_READER_PASSWORD` is the value you put in `stack/n8n/.env` (next step).

## 3. n8n and Open WebUI

```sh
cd stack/n8n        && cp .env.example .env && $EDITOR .env && docker compose up -d   # http://localhost:5680
cd ../open-webui    && cp .env.example .env && $EDITOR .env && docker compose up -d   # http://localhost:3000
```

Create the owner account in n8n and the admin account in Open WebUI.

## 4. Azure

1. **Azure AI Foundry:** create a resource and deploy `text-embedding-3-small`, `gpt-5.4-mini` and
   `mistral-document-ai-2512` (Data Zone Standard keeps processing in the US). Name each deployment after
   its model, or change the names in the workflows. Note the resource host
   (`<resource>.services.ai.azure.com`) and key.
2. **App registration** (Microsoft Entra ID) for reading SharePoint:
   - Redirect URI: `http://localhost:5680/rest/oauth2-credential/callback`
   - Delegated permissions: `Sites.Read.All`, `Files.Read.All`, `offline_access`, `openid`.
     The workflows only read; this build was tested with the ReadWrite variants, but read-only is enough.
   - n8n's Microsoft credential signs in through the `/common` endpoint, so either make the app
     multitenant or replace `common` with your tenant ID in the credential's URLs.

## 5. Credentials in n8n

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

## 6. Workflows

```sh
cp workflows.env.example workflows.env && $EDITOR workflows.env
python3 scripts/configure_workflows.py          # writes build/workflows/*.json with your values
```

Import them (n8n → Workflows → Import from file) in this order: `search-reranked`, `web-search-allow-listed`, `chat-agent`,
`ingestion`, `daily-health`. Open each one, check every node shows its credential, and **publish** it.
The search and web-search workflows must be published before the agent can call them.

If you import with the CLI instead (`n8n import:workflow`), imports arrive unpublished: run
`n8n publish:workflow --id=<id>` for each and restart the n8n containers.

## 6b. Web search (optional — to skip it, delete the `search_web` node from the chat agent)

`RAG – Web search (allow-listed)` calls the Azure OpenAI Responses API (`/openai/v1/responses`) with the
`web_search` tool, using the same **Azure AI – RAG** credential. Edit `ALLOWED_DOMAINS` in its *Build web search
request* node (max 20 domains): add your state environmental agency, your state's regulations site and your
local public-works site to the federal defaults. Only a short model-written search query leaves Azure; the agent
is instructed never to put document text, file names, project names or people in a query. Note: `web_search_preview` does not accept domain filters — use `web_search`.

## 7. Open WebUI pipe

Admin Panel → Functions → **+** → paste `open-webui/knowledge_assistant_pipe.py` → save. Open its
settings (valves) and set `n8n_token` to your `RAG_WEBHOOK_TOKEN`, then enable it. "Knowledge Assistant"
now appears in the model picker.

## 8. First run

Run **RAG – Ingestion** once with *Execute workflow*, then leave the schedule on: each run processes up to
10 files. Watch progress with:

```sql
select step, status, count(*) from ingestion_log group by 1, 2;
```

## Operating it

- **Daily health message** at 08:00 (edit the schedule in `RAG – Daily health`).
- **Erase a person:** `ops/forget-user.sh someone@example.com --dry-run`, then without `--dry-run`
  (add `--delete-account` to remove their login too).
- **Retention:** 90 days for chats and agent memory, 1 year for logs — change `RETENTION_DAYS` in
  `stack/open-webui/compose.yml` and the intervals in `sql/03_retention_usage.sql`.
- **Updating this repo from a running instance:** `scripts/export-from-n8n.sh` exports, cleans and
  leak-checks the workflows; review `git diff`, then commit.
