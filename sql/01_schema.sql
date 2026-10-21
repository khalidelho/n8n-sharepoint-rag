-- Supabase storage for the private RAG stack.
-- Run once (Supabase Studio → SQL Editor, or psql). Safe to re-run (IF NOT EXISTS everywhere).
--
-- Embedding model: Azure OpenAI text-embedding-3-small → 1536 dimensions.
-- The vector column size MUST equal the model's output size; changing models later
-- means recreating `documents` and re-embedding everything.


-- 1) pgvector: lets Postgres store embeddings and search them by similarity.
create extension if not exists vector with schema extensions;


-- 2) Vector table — one row per chunk.
--    Shape is what n8n's "Supabase Vector Store" node expects (content + metadata + embedding).
--    The blueprint's file_id / file_name / chunk_index live inside `metadata`.
create table if not exists public.documents (
  id        bigserial primary key,
  content   text,                 -- the chunk text
  metadata  jsonb,                -- {"file_id": "...", "file_name": "...", "chunk_index": 0, ...}
  embedding extensions.vector(1536)
);

-- HNSW index (the blueprint's choice): fast approximate nearest-neighbour search.
create index if not exists documents_embedding_hnsw
  on public.documents using hnsw (embedding extensions.vector_cosine_ops);

-- Fast lookups by file — used when a file is edited/deleted (delete all its chunks)
-- and by Tool 2 (fetch every chunk of one file, in order).
create index if not exists documents_file_id_idx
  on public.documents ((metadata->>'file_id'));

-- Similarity search function the n8n Supabase Vector Store node calls by name.
create or replace function public.match_documents (
  query_embedding extensions.vector(1536),
  match_count int default null,
  filter jsonb default '{}'
) returns table (id bigint, content text, metadata jsonb, similarity float)
language plpgsql
set search_path = public, extensions
as $$
#variable_conflict use_column
begin
  return query
  select id, content, metadata,
         1 - (documents.embedding <=> query_embedding) as similarity
  from documents
  where metadata @> filter
  order by documents.embedding <=> query_embedding
  limit match_count;
end;
$$;


-- 3) Metadata table — the system's memory of what SharePoint looked like last run.
--    Blueprint: file ID, file name, last modified. content_hash is from the blueprint's
--    failure-modes section (catch edits whose timestamp didn't change).
create table if not exists public.document_metadata (
  file_id       text primary key,
  file_name     text not null,
  last_modified timestamptz not null,
  content_hash  text,
  updated_at    timestamptz not null default now()
);


-- 4) document_rows — spreadsheet data (Excel/CSV), one record per row, stored as JSON.
--    Not chunked, not embedded: the agent queries this with SQL (Tool 3).
create table if not exists public.document_rows (
  id        bigserial primary key,
  file_id   text not null,
  file_name text not null,
  row_data  jsonb not null
);

create index if not exists document_rows_file_id_idx
  on public.document_rows (file_id);


-- 5) Ingestion log — the blueprint's "dedicated table" for document IDs and chunk counts,
--    written after OCR, after the vector write, and after SQL operations.
create table if not exists public.ingestion_log (
  id          bigserial primary key,
  logged_at   timestamptz not null default now(),
  file_id     text,
  file_name   text,
  step        text not null,       -- e.g. 'ocr', 'vector_write', 'rows_write', 'delete', 'error'
  status      text not null,       -- 'ok' | 'failed' | 'retry'
  chunk_count int,
  message     text
);


-- 6) Lock the tables away from the public API.
--    Supabase publishes every table in `public` over its REST API, and the anon key is
--    meant to be public. RLS on + no policies = anon gets nothing; n8n uses the
--    service_role key / postgres login, which bypass RLS.
alter table public.documents         enable row level security;
alter table public.document_metadata enable row level security;
alter table public.document_rows     enable row level security;
alter table public.ingestion_log     enable row level security;
