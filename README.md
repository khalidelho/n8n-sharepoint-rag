# Private RAG for SharePoint, built with n8n

A self-hosted "chat with your documents" assistant for a SharePoint document library.
Ask a question in a ChatGPT-style window; get an answer grounded in your own files, with the
source cited and, for how-to questions, the relevant screenshots from the documents shown inline.

- **Your data stays in your stack:** documents, vectors, chats and images live in your own Postgres
  (Supabase) — only the AI calls leave, to Azure AI Foundry in a US data zone (no training on your data).
- **Keeps itself in sync:** every 15 minutes it compares SharePoint with what it has indexed and
  processes only what's new, changed, renamed or deleted.
- **Reads everything:** PDFs, Word, PowerPoint (incl. scanned pages, via OCR), images inside documents
  (described by the OCR model so screenshots are searchable), and spreadsheets (queried with SQL, not
  guessed from text).
- **Built for operations:** queue-mode n8n, overlap guard, per-file retry, a daily health message,
  90-day retention, a one-command right-to-erasure script, and usage counts that never store questions.

Based on the blueprint from the M365.FM podcast episode *"Building Private RAG: A Blueprint for SharePoint
& n8n"*, built end to end and hardened for real-world documents. Everything below is what actually runs.

## Architecture

```mermaid
flowchart LR
    SP[(SharePoint<br/>document library)]

    subgraph n8n["n8n (queue mode: main + 2 workers + Redis)"]
        ING[Ingestion<br/>every 15 min]
        AGT[Chat agent<br/>+ memory]
        SRCH[Search<br/>vector → rerank]
        HLT[Daily health]
    end

    subgraph AZ["Azure AI Foundry (US data zone)"]
        OCR[Mistral Document AI<br/>OCR + image descriptions]
        EMB[text-embedding-3-small]
        GPT[gpt-5.4-mini<br/>chunking · reranking · answers]
    end

    subgraph SB["Supabase (self-hosted)"]
        PG[(Postgres + pgvector<br/>chunks · metadata · rows · memory)]
        ST[(Storage<br/>document images)]
    end

    GOT[Gotenberg<br/>Office→PDF, split]
    OWU[Open WebUI<br/>chat front end]
    TG[Telegram]

    SP -- Graph API --> ING
    ING --> GOT --> OCR
    ING --> GPT
    ING --> EMB
    ING --> PG
    ING --> ST
    OWU -- webhook + token --> AGT
    AGT --> SRCH --> EMB
    SRCH --> GPT
    SRCH --> PG
    AGT --> GPT
    AGT -- read-only SQL --> PG
    HLT --> PG
    HLT --> TG
```

### Ingestion (the `RAG – Ingestion` workflow)

| Step | What happens |
|---|---|
| ① Start & safety check | Runs every 15 min; skips if another run is still going (asks n8n's own DB). |
| ② What's in SharePoint now | Lists the library through Microsoft Graph, following `@odata.nextLink` pages. |
| ③ What changed | Compares against `document_metadata` using Graph's `quickXorHash` content fingerprint → new / updated / renamed-only / deleted / unchanged. Outdated files (`OLD_…`, "Do Not Use") count as absent. |
| ④ Batch & download | Takes 10 files, clears any half-finished data for them first (every run is idempotent). |
| ⑤ Spreadsheets → SQL | Excel/CSV rows go into `document_rows` as JSON, so the agent can count and filter exactly. |
| ⑥ Prepare for OCR | Gotenberg converts Office files to PDF and splits long PDFs into 10-page parts. |
| ⑦ Read the pages | Mistral Document AI returns Markdown per page plus a description of every image. |
| ⑧ Images → Storage | Images are uploaded to Supabase Storage; links in the text point at them. |
| ⑨ Cut, embed, store | GPT proposes chunk boundaries over numbered paragraphs ("agentic chunking"); code validates and repairs the plan, merges tiny chunks, caps size. Chunks are embedded (1536-d) into pgvector. File metadata is written **last**, so a crash means "redo", never "half-indexed but marked done". |

### Answering (the `RAG – Chat agent` and `RAG – Search (reranked)` workflows)

1. Open WebUI's **Knowledge Assistant** pipe forwards the newest message to an n8n webhook with a bearer
   token, labelling the memory session `<user-id>:<chat-id>`.
2. A usage row is written with **hashes only** (who/which chat, never the question).
3. A Tools Agent (gpt-5.4-mini) with Postgres chat memory decides which tools to call:
   - `search_documents` — vector search for 25 candidates → GPT reranks them and keeps up to 4
     (it may keep fewer). In side-by-side tests this beat plain vector order clearly: cover pages and
     duplicate copies of a deck stop crowding out the passage that actually answers the question.
   - `get_full_document` — every chunk of one file, in order, when a passage isn't enough.
   - `query_spreadsheets` — SQL over `document_rows`, through a **read-only** Postgres login
     with a 15-second timeout.
4. The answer comes back with citations and inline screenshots.

### Privacy & operations

- **Row-level security** on every table; Supabase's public API sees nothing. The agent's login can only read.
- **Retention:** chat memory 90 days (pg_cron inside Postgres), Open WebUI chats 90 days (a sidecar
  container using Open WebUI's own delete code), logs 1 year.
- **Right to erasure:** `ops/forget-user.sh someone@example.com [--dry-run] [--delete-account]` removes a
  person's chats, agent memory, n8n run records and usage rows, and logs *that* it happened (hash + counts).
- **Usage without surveillance:** active users and question counts from hashes, never question text.
- **Daily health message** (Telegram): documents, chunks, images, spreadsheets, last-24 h ingestion,
  files still failing, usage.
- **n8n hygiene:** ingestion and health runs are saved only when they fail; all executions are pruned after 72 h / 500 runs.

## Tech stack

| Component | Version | Role |
|---|---|---|
| n8n | 2.43.8 | Orchestration: ingestion, agent, tools. Queue mode (main + 2 workers) |
| Redis | 7.4 | Job queue between n8n main and workers |
| Postgres (n8n) | 17.6 | n8n's own workflows, credentials, run history |
| Supabase (self-hosted) | docker v0.8.3, Postgres 17.6 | pgvector (HNSW) store, metadata, spreadsheet rows, chat memory, Storage for images |
| Open WebUI | 0.12.3 | Chat front end, accounts, chat history |
| Gotenberg | 8.37.0 | LibreOffice conversion (PPTX/DOCX → PDF) and PDF splitting |
| Microsoft Graph | v1.0 | Reading the SharePoint library (delegated OAuth) |
| Telegram Bot API | — | Daily health message |

**Models** (Azure AI Foundry, Data Zone Standard US):

| Model | Used for |
|---|---|
| `mistral-document-ai-2512` | OCR: page text as Markdown + image descriptions |
| `text-embedding-3-small` (1536-d) | Embeddings for chunks and questions |
| `gpt-5.4-mini` | Agentic chunking, reranking, answering |

## What it costs (approximate, pay-as-you-go)

| | |
|---|---|
| Per question | ~$0.04 (agent + rerank calls) |
| Indexing a few-hundred-document library once | ~$15–30 (OCR with image descriptions dominates) |
| Hosting on the machine you already have | $0 |
| Hosting on a cloud VM (16 GB+ RAM, managed disk, backups) | ~$130–190 / month |

## Repository layout

```
n8n/workflows/        the four workflows, with placeholders (__SHAREPOINT_SITE_ID__ …) instead of real IDs
sql/                  database schema, the agent's read-only role, retention/usage/privacy tables
stack/n8n/            Docker Compose: n8n main + 2 workers + Redis + Postgres + Gotenberg
stack/open-webui/     Docker Compose: Open WebUI + retention sidecar
stack/supabase/       macOS override for Supabase Storage (named volume; see setup)
open-webui/           the Knowledge Assistant pipe function
ops/                  forget-user.sh (right to erasure)
scripts/              configure / sanitize workflows, leak check
docs/                 setup guide, lessons learned
```

## Getting started

See **[docs/setup.md](docs/setup.md)**. In short: start Supabase, n8n and Open WebUI with Docker Compose,
run the three SQL files, create the n8n credentials, fill `workflows.env`, run
`scripts/configure_workflows.py`, import and publish the workflows, paste the pipe into Open WebUI.

## Lessons learned

The build hit a long list of real-world problems — OCR page and size limits, a credential that silently
didn't attach, comma-splitting SQL parameters, 240 MB execution records, a stale browser tab that
overwrote a newer workflow, and more. They're collected, with fixes, in
**[docs/lessons-learned.md](docs/lessons-learned.md)**.

## Roadmap

- Share safely: HTTPS + company sign-in in front of Open WebUI; per-person document permissions
- Read-only SharePoint access scoped to one site (`Sites.Selected`)
- Signed (private) image links instead of a public bucket
- Meeting recordings: index transcripts saved next to videos; later, automatic transcription
- Skip duplicate documents (same deck as PDF and PPTX); spreadsheet header-row detection
- Honour Graph `Retry-After` exactly; shorter rerank snippets to cut cost per question

## Keeping it clean

The workflows are exported from a live instance, so `scripts/check-leaks.sh` runs before every commit and
refuses tenant names, IDs, e-mail addresses and secrets (plus a private deny list kept outside the repo).
Enable the hook once per clone:

```sh
git config core.hooksPath .githooks
```

## License

MIT — see [LICENSE](LICENSE).
