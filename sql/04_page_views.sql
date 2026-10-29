-- Visual page search. Additive only: new tables, nothing existing changes.
-- One row per "view" of a page: the whole page, plus (for large drawing sheets) 12 tiles and a title-block crop,
-- plus photos. Each view has an image embedding from the local vision service (vision/), so pages
-- are found by what they look like. page_text holds the page's own text layer (exact on CAD exports) or a
-- Tesseract read of scans, for the identifier/keyword branch ("sheet C-08", "Chart 39").
-- Undo: drop table page_views, page_index_status; drop role rag_vision;
-- :dims is set by the runner (psql -v dims=768|1536|2048) to match the chosen embedding model (768 for gemma).
-- Run as postgres:  docker exec -i supabase-db psql -U postgres -v ON_ERROR_STOP=1 -v dims=768 < sql/04_page_views.sql
-- Then give the vision service's login a password (it goes into vision/.env PG_DSN, never into git):
--   docker exec -it supabase-db psql -U postgres -c '\password rag_vision'

create table if not exists public.page_views (
  id           bigserial primary key,
  file_id      text not null,
  file_name    text not null,
  page_no      int  not null,                       -- 1-based page in the (converted) PDF; 1 for photos
  kind         text not null check (kind in ('page', 'tile', 'title', 'photo')),
  tile_ix      int,                                 -- 0..11 for tiles (row-major 4x3), null otherwise
  bbox         real[],                              -- view's box as fractions of the page: x0, y0, x1, y1
  rotation     int  not null default 0,             -- degrees we rotated a scanned page to make it upright
  page_w_in    real,
  page_h_in    real,
  image_path   text not null,                       -- relative to the vision service's pages/ folder
  page_text    text,                                -- only on kind page/photo rows
  page_tsv     tsvector generated always as (to_tsvector('english', coalesce(file_name, '') || ' ' || coalesce(page_text, ''))) stored,
  embed_model  text not null,
  embedding    halfvec(:dims) not null,
  created_at   timestamptz not null default now()
);
create index if not exists page_views_embedding_hnsw on public.page_views using hnsw (embedding halfvec_cosine_ops);
create index if not exists page_views_file_page on public.page_views (file_id, page_no);
create index if not exists page_views_tsv on public.page_views using gin (page_tsv);

create table if not exists public.page_index_status (
  file_id      text primary key,
  file_name    text not null,
  content_hash text,                                -- matches document_metadata.content_hash when indexed
  status       text not null,                       -- queued | ok | skipped | failed
  pages        int,
  views        int,
  message      text,
  seconds      real,
  updated_at   timestamptz not null default now()
);

alter table public.page_views enable row level security;
alter table public.page_index_status enable row level security;

-- The agent's read-only login may read page views (same pattern as documents / document_rows).
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'rag_reader') then
    grant select on public.page_views, public.page_index_status to rag_reader;
    if not exists (select 1 from pg_policies where tablename = 'page_views' and policyname = 'rag_reader_select') then
      create policy rag_reader_select on public.page_views for select to rag_reader using (true);
    end if;
  end if;
end $$;

-- The vision service's own login: write page views, read the catalog. Password is set separately (never in git).
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'rag_vision') then
    create role rag_vision login;
  end if;
end $$;
grant usage on schema public to rag_vision;
grant select, insert, update, delete on public.page_views, public.page_index_status to rag_vision;
grant usage, select on sequence public.page_views_id_seq to rag_vision;
grant select on public.document_metadata to rag_vision;
do $$ begin
  if not exists (select 1 from pg_policies where tablename = 'page_views' and policyname = 'rag_vision_all') then
    create policy rag_vision_all on public.page_views for all to rag_vision using (true) with check (true);
  end if;
  if not exists (select 1 from pg_policies where tablename = 'page_index_status' and policyname = 'rag_vision_all') then
    create policy rag_vision_all on public.page_index_status for all to rag_vision using (true) with check (true);
  end if;
  if not exists (select 1 from pg_policies where tablename = 'document_metadata' and policyname = 'rag_vision_select') then
    create policy rag_vision_select on public.document_metadata for select to rag_vision using (true);
  end if;
end $$;
alter role rag_vision set statement_timeout = '60s';
-- pgvector lives in the "extensions" schema on Supabase: the service login needs it on its search path (halfvec type, <=> operator).
grant usage on schema extensions to rag_vision;
alter role rag_vision set search_path = public, extensions;
-- Crash-loop guard: count indexing attempts; a file that kills the service twice is marked failed.
alter table public.page_index_status add column if not exists attempts int not null default 0;
-- Keep each file's source (vision/sources/<file_id>.<ext>) so zooms render at full resolution from the original.
alter table public.page_index_status add column if not exists has_source boolean not null default false;
-- Page numbers on passages: the vision service fills documents.metadata page_start/page_end.
grant select, update (metadata) on public.documents to rag_vision;
do $$ begin
  if not exists (select 1 from pg_policies where tablename='documents' and policyname='rag_vision_select') then
    create policy rag_vision_select on public.documents for select to rag_vision using (true); end if;
  if not exists (select 1 from pg_policies where tablename='documents' and policyname='rag_vision_update') then
    create policy rag_vision_update on public.documents for update to rag_vision using (true) with check (true); end if;
end $$;
