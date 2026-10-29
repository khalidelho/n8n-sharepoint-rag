-- Error flow: incidents, answer checks, eval runs.
-- Additive only: new tables, one new column on usage_log, one function, three retention jobs. Safe to re-run.
-- RLS on, no policies (as usage_log in 03_retention_usage.sql): only n8n's admin login reads and writes; the agent's
-- read-only login (rag_reader) cannot see these tables. No question or answer text is stored anywhere here.
-- Run as postgres:  docker exec -i supabase-db psql -U postgres -v ON_ERROR_STOP=1 < sql/06_qa_incidents.sql
-- Undo: drop table incident_log, qa_check, eval_run; drop function is_internal_session(text);
--       alter table usage_log drop column internal; select cron.unschedule('rag-incident-log-retention'),
--       cron.unschedule('rag-qa-check-retention'), cron.unschedule('rag-eval-run-retention');

-- 1) Incidents: one row per failure anywhere (n8n error workflow, the incident webhook, the pipe, backups).
--    Backups also write a kind 'ok' row on success (the digest's "last backup"). message is scrubbed and
--    truncated by the writer before insert; notified_at is set when the Telegram alert went out.
create table if not exists public.incident_log (
  id           bigserial primary key,
  at           timestamptz not null default now(),
  source       text,            -- error_workflow | chat_agent | look_at_pages | pipe | watch | backup | ...
  workflow     text,
  node         text,
  kind         text,
  message      text,
  execution_id text,
  ref          text,            -- the ref the user sees ("ref 12345", "p-...")
  data         jsonb,
  notified_at  timestamptz,
  resolved_at  timestamptz
);
create index if not exists incident_log_at_idx on public.incident_log (at);
create index if not exists incident_log_throttle_idx on public.incident_log (source, workflow, node, kind, notified_at);
alter table public.incident_log enable row level security;

-- 2) Answer checks: one row per real answer. Layer 1 (before send) writes the first five columns; the grader
--    (after send) fills route onward. Claim fragments only in flags/verdict_reasons, never the question or answer.
create table if not exists public.qa_check (
  id                       bigserial primary key,
  at                       timestamptz not null default now(),
  execution_id             text,
  session_id               text,
  tools_used               text[],
  seconds                  numeric,
  flags                    jsonb,
  verdict                  text,      -- grounded | partial | unsupported | not_graded
  route                    text,
  needed_library           boolean,
  library_consulted        boolean,
  named_document           text,
  named_document_respected boolean,
  verdict_reasons          jsonb,
  grader_ms                int,
  alerted_at               timestamptz
);
create index if not exists qa_check_at_idx on public.qa_check (at);
create index if not exists qa_check_verdict_alerted_idx on public.qa_check (verdict, alerted_at);
alter table public.qa_check enable row level security;

-- 3) Scheduled eval runs: one row per set per run (demo, messy, visual).
create table if not exists public.eval_run (
  id           bigserial primary key,
  at           timestamptz not null default now(),
  set_name     text,
  passed       int,
  total        int,
  failed_ids   text[],
  seconds      numeric,
  results_path text
);
create index if not exists eval_run_set_at_idx on public.eval_run (set_name, at);
alter table public.eval_run enable row level security;

-- 4) Internal sessions (canary, evals, demos, deploy checks) are not people: not usage-counted, not graded.
--    The one definition of the prefixes; Python/JS mirror it as INTERNAL_PREFIXES. A null id is not internal.
create or replace function public.is_internal_session(session_id text)
returns boolean language sql immutable parallel safe as $$
  select coalesce(session_id like any (array['canary-%', 'eval-%', 'demo-%', 'messy-eval-%', 'visual-eval-%',
                                              'deploy-check-%']), false)
$$;
-- Existing rows stay false: usage_log holds only hashes, so past internal sessions cannot be told apart.
alter table public.usage_log add column if not exists internal boolean not null default false;

-- 5) Retention, enforced inside the database every night (pg_cron; re-scheduling by name updates the job).
create extension if not exists pg_cron;
select cron.schedule('rag-incident-log-retention', '30 3 * * *',
  $$delete from public.incident_log where at < now() - interval '365 days'$$);
select cron.schedule('rag-qa-check-retention', '35 3 * * *',
  $$delete from public.qa_check where at < now() - interval '90 days'$$);
select cron.schedule('rag-eval-run-retention', '40 3 * * *',
  $$delete from public.eval_run where at < now() - interval '365 days'$$);
