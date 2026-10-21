-- Operations & privacy: retention, usage counts without content, an erasure log.

-- 1) Chat memory gets a date, so it can expire. Existing rows count from today.
alter table public.n8n_chat_histories add column if not exists created_at timestamptz not null default now();
create index if not exists n8n_chat_histories_created_idx on public.n8n_chat_histories (created_at);

-- 2) Usage log: one row per question, NO question text. Users are pseudonymous hashes
--    (enough to count active people, not to see who asked what).
create table if not exists public.usage_log (
  id        bigserial primary key,
  asked_at  timestamptz not null default now(),
  user_hash text,
  chat_hash text
);
create index if not exists usage_log_asked_idx on public.usage_log (asked_at);
alter table public.usage_log enable row level security;

-- 3) Privacy log: a record THAT an erasure happened (when, a hash of who, what was removed) — never content.
create table if not exists public.privacy_log (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  action      text not null,
  subject     text,            -- sha256 of the email, not the email itself
  details     jsonb
);
alter table public.privacy_log enable row level security;

-- 4) Retention, enforced inside the database every night (pg_cron).
create extension if not exists pg_cron;
select cron.schedule('rag-chat-memory-retention', '15 3 * * *',
  $$delete from public.n8n_chat_histories where created_at < now() - interval '90 days'$$);
select cron.schedule('rag-usage-log-retention', '20 3 * * *',
  $$delete from public.usage_log where asked_at < now() - interval '365 days'$$);
select cron.schedule('rag-ingestion-log-retention', '25 3 * * *',
  $$delete from public.ingestion_log where logged_at < now() - interval '365 days'$$);
