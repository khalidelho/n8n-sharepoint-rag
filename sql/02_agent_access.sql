-- Database access for the chat agent (the blueprint: "grant read access through the SQL tool configuration").
-- The agent writes its own SQL (Tool 3), so it gets a login that physically cannot change anything.
-- Password is passed in at run time (psql -v pw=...), never stored in this file.

-- 1) Read-only login for the agent's tools
do $$ begin
  if not exists (select from pg_roles where rolname = 'rag_reader') then
    create role rag_reader login;
  end if;
end $$;
alter role rag_reader password :'pw';
alter role rag_reader set default_transaction_read_only = on;   -- every transaction is read-only
alter role rag_reader set statement_timeout = '15s';             -- a runaway query can't hang the chat
grant usage on schema public to rag_reader;
grant select on public.documents, public.document_rows, public.document_metadata to rag_reader;

-- 2) Our tables have RLS on with no rules (= only admin logins see anything). Let the agent read them.
drop policy if exists "agent can read" on public.documents;
create policy "agent can read" on public.documents         for select to rag_reader using (true);
drop policy if exists "agent can read" on public.document_rows;
create policy "agent can read" on public.document_rows     for select to rag_reader using (true);
drop policy if exists "agent can read" on public.document_metadata;
create policy "agent can read" on public.document_metadata for select to rag_reader using (true);

-- 3) Chat memory table, created up front WITH row level security. (If n8n created it, it would be
--    exposed through Supabase's public API with no RLS — everyone's chat history readable with the anon key.)
create table if not exists public.n8n_chat_histories (
  id         serial primary key,
  session_id varchar(255) not null,
  message    jsonb not null
);
create index if not exists n8n_chat_histories_session_idx on public.n8n_chat_histories (session_id);
alter table public.n8n_chat_histories enable row level security;
-- (no policies: only n8n's admin login reads/writes it; the agent's read-only login can't see it)
