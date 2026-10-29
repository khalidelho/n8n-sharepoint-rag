-- The library's own vocabulary, for reading messy messages.
-- People type fast: "Fall stabilisation" meant "outfall stabilization", "complant" = "complaint", "NPEDS" = "NPDES".
-- library_lookup(message) returns words in the message that look like a clipped or misspelled library term, plus
-- the files whose names match the message (to tell when a named document is not in the library). The chat agent's
-- router reads these candidates IN CONTEXT and decides; nothing here rewrites the question by itself.
-- Run as postgres:  docker exec -i supabase-db psql -U postgres -v ON_ERROR_STOP=1 < sql/05_library_vocabulary.sql

create extension if not exists pg_trgm with schema extensions;
create extension if not exists fuzzystrmatch with schema extensions;

create table if not exists public.library_terms (
  term  text primary key,
  files int not null                       -- number of files whose text or name contains the word
);
create table if not exists public.library_terms_state (
  id int primary key default 1 check (id = 1),
  signature text,                          -- documents count/max id + file count: rebuild only when it changes
  terms int,
  built_at timestamptz
);
alter table public.library_terms enable row level security;
alter table public.library_terms_state enable row level security;   -- no policies: only the functions below touch them
revoke all on public.library_terms, public.library_terms_state from anon, authenticated;

-- Rebuild the vocabulary when the library changed. Generated text is left out: image descriptions and their captions
-- were written by a model, not by the documents' authors.
create or replace function public.refresh_library_terms(force boolean default false)
returns jsonb language plpgsql security definer set search_path = public, extensions, pg_temp as $$
declare
  sig text;
  n int;
begin
  select (select count(*) || ':' || coalesce(max(id), 0) from documents) || ':' || (select count(*) from document_metadata) into sig;
  -- also once a day: the signature misses renames and in-place edits (found in review)
  if not force and exists (select 1 from library_terms_state where id = 1 and signature is not distinct from sig
                           and built_at > now() - interval '1 day') then
    return jsonb_build_object('rebuilt', false);
  end if;
  create temp table _terms on commit drop as
    select w as term, count(distinct fid)::int as files
    from (select metadata->>'file_id' as fid,
                 regexp_replace(regexp_replace(lower(content), '!\[[^\]]*\]\([^)]*\)', ' ', 'g'),
                                '\*?image, page \d+:[^\n]*', ' ', 'g') as txt
          from documents
          union all
          select file_id, lower(file_name) from document_metadata) s,
         regexp_split_to_table(s.txt, '[^a-z0-9]+') w
    where length(w) between 3 and 30 and w !~ '^[0-9]+$'
    group by w;
  delete from library_terms;
  insert into library_terms select * from _terms;
  get diagnostics n = row_count;
  insert into library_terms_state (id, signature, terms, built_at) values (1, sig, n, now())
    on conflict (id) do update set signature = excluded.signature, terms = excluded.terms, built_at = excluded.built_at;
  return jsonb_build_object('rebuilt', true, 'terms', n);
end $$;

-- Candidates for one message. kind 'clipped': the typed word is the start or end of a longer, common library term
-- (fall -> outfall / rainfall, geotech -> geotechnical): listed generously, the router judges from the sentence.
-- kind 'spelling': within 1-2 edits of a library term that is far more common than the word as typed.
create or replace function public.library_lookup(msg text)
returns jsonb language sql stable security definer set search_path = public, extensions, pg_temp as $$
with n as (select greatest(count(*), 1)::float as files from document_metadata),
words as (
  select distinct w from regexp_split_to_table(lower(coalesce(msg, '')), '[^a-z0-9]+') w
  where length(w) between 3 and 30 and w !~ '^[0-9]+$'
    and w <> all (array['the','and','for','with','what','whats','how','does','our','are','this','that','from','into',
      'about','please','can','you','any','all','its','was','were','will','would','should','could','there','their','them',
      'they','then','than','when','where','which','who','why','have','has','had','been','also','only','just','like',
      'make','give','tell','show','need','want','use','get','new','one','two','last','year','years','each','per',
      'etc','via','not','but','out','off','over','under','more','most','some','such','very','include','including',
      'update','develop','summarize','explain','describe','list','find','help','let','lets','did','dont','doesnt'])
),
typed as (select w, coalesce(t.files, 0) as files from words left join library_terms t on t.term = w),
cand as (
  select distinct on (ty.w, c.term) ty.w as said, c.term, c.files, c.kind
  from typed ty cross join n
  cross join lateral (
    select term, files, kind from (
      select t.term, t.files, 'clipped' as kind from library_terms t
      where ty.files <= 0.15 * n.files                                  -- common words are not clipped words
        and length(t.term) between length(ty.w) + 2 and length(ty.w) + 6
        and (t.term like '%' || ty.w or t.term like ty.w || '%')
        and t.files >= greatest(5, ty.files)
      union all
      select t.term, t.files, 'spelling' from library_terms t
      where length(ty.w) >= 4 and ty.files <= 2                         -- a typo is rare in the library as typed
        and ty.w !~ '[0-9]'                                             -- identifiers (PR1234, C08) are never "corrected"
        and length(t.term) >= 4 and length(t.term) between length(ty.w) - 2 and length(ty.w) + 2
        and t.files >= greatest(3, 3 * ty.files) and t.term <> ty.w
        and levenshtein_less_equal(t.term, ty.w, case when length(ty.w) <= 5 then 1 else 2 end)
            <= case when length(ty.w) <= 5 then 1 else 2 end
      union all                                                          -- swapped letters (NPEDS -> NPDES)
      select t.term, t.files, 'spelling' from library_terms t
      where length(ty.w) >= 4 and ty.files <= 2 and ty.w !~ '[0-9]' and length(t.term) = length(ty.w) and t.term <> ty.w
        and t.files >= greatest(3, 3 * ty.files)
        and levenshtein_less_equal(t.term, ty.w, 2) <= 2               -- a swap is 2 edits; rules out anagrams of unrelated words
        and (select string_agg(c, '' order by c) from regexp_split_to_table(t.term, '') c)
          = (select string_agg(c, '' order by c) from regexp_split_to_table(ty.w, '') c)
    ) x order by files desc limit 3
  ) c
  order by ty.w, c.term
),
-- file names are matched on the words as typed plus SPELLING fixes only: a clipped-word guess (fall -> rainfall) is
-- too speculative to steer which files are listed
expanded as (select w from typed union select term from cand where kind = 'spelling'),
name_words as (
  select e.w, count(*)::float as nf from expanded e
  join document_metadata m on lower(m.file_name) ~ ('(^|[^a-z0-9])' || e.w || 's?([^a-z0-9]|$)')   -- whole words: "fall" is not "Outfall"
  group by e.w),
names as (
  select m.file_name, sum(ln(1 + n.files / nw.nf)) as score, bool_or(nw.nf <= 6) as rare_word
  from document_metadata m cross join n join name_words nw on lower(m.file_name) ~ ('(^|[^a-z0-9])' || nw.w || 's?([^a-z0-9]|$)')
  group by m.file_name),
-- best overall, plus every file matching a RARE word of the message (NPDES: 4 files), so a named document's nearest
-- namesakes are always visible even when generic words ("stream restoration") match more files
shown as (
  (select file_name, score from names where score >= 3 order by score desc limit 5)
  union
  (select file_name, score from names where rare_word order by score desc limit 5))
select jsonb_build_object(
  'vocab', coalesce((select jsonb_agg(jsonb_build_object('said', said, 'library_term', term, 'files', files, 'kind', kind)
                                      order by said, files desc) from cand), '[]'::jsonb),
  'files', coalesce((select jsonb_agg(file_name order by score desc)
                     from shown), '[]'::jsonb))
$$;

revoke all on function public.refresh_library_terms(boolean), public.library_lookup(text) from public, anon, authenticated, service_role;
grant execute on function public.library_lookup(text) to rag_reader;
grant execute on function public.refresh_library_terms(boolean) to postgres;
select public.refresh_library_terms(true);
