"""Page numbers for text passages: match each passage of `documents` to the pages of the same file in `page_views`.

Why: no passage carried a page number, so answers couldn't cite "page 14" and a text hit couldn't lead to the page
image (r/Rag advice, 2026-10-22: keep page + document id on every chunk). The passages came from Mistral OCR of the
same (converted) PDF the page index rendered, so their words reappear in that page's text layer / Tesseract read.

Method: IDF-weighted word-bigram and word coverage of the passage by each page; the passage spans the contiguous pages
around the best match that score at least half as well. Image descriptions are generated text, not on the page, so
`![...](...)` blocks and "*Image, page N: ...*" captions are stripped before matching (and the captions' page numbers
are used only to measure accuracy).
"""
from __future__ import annotations

import math
import re
from collections import Counter

IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)", re.S)
IMG_CAPTION = re.compile(r"\*?Image, page (\d+):[^\n]*", re.I)
TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    text = IMG_CAPTION.sub(" ", IMG_MD.sub(" ", text or ""))
    text = text.lower().replace("­", "").replace("-\n", "")
    return [t for t in TOKEN.findall(text) if len(t) >= 2 or t.isdigit()]


def caption_pages(text: str) -> set[int]:
    return {int(n) for n in IMG_CAPTION.findall(text or "")}


class FilePages:
    """The pages of one file, ready for matching."""

    def __init__(self, pages: dict[int, str]):
        self.pages = sorted(pages)
        self.uni = {p: set(tokens(t)) for p, t in pages.items()}
        self.bi = {p: set(zip(tk, tk[1:])) for p, tk in ((p, tokens(t)) for p, t in pages.items())}
        n = max(1, len(pages))
        df_u = Counter(w for s in self.uni.values() for w in s)
        df_b = Counter(b for s in self.bi.values() for b in s)
        self.idf_u = {w: math.log(1 + n / c) for w, c in df_u.items()}
        self.idf_b = {b: math.log(1 + n / c) for b, c in df_b.items()}

    def containment(self, passage: str) -> dict[int, float]:
        """For each page: how much of THE PAGE appears in the passage (IDF-weighted, bigrams + words).
        Passages are contiguous spans of the document, so their pages are mostly contained in them. (Scoring how much
        of the passage each page covers let one text-heavy "hub" slide win every passage of a deck.)"""
        tk = tokens(passage)
        uni, bi = set(tk), set(zip(tk, tk[1:]))
        out = {}
        for p in self.pages:
            pu, pb = self.uni[p], self.bi[p]
            if len(pu) < 4:                      # nearly empty page (title slide, blank scan): unknown
                out[p] = -1.0
                continue
            cu = sum(self.idf_u[w] for w in pu & uni) / (sum(self.idf_u[w] for w in pu) or 1)
            cb = sum(self.idf_b[b] for b in pb & bi) / (sum(self.idf_b[b] for b in pb) or 1) if pb else cu
            out[p] = 0.6 * cb + 0.4 * cu
        return out

    def match(self, passage: str, after: int | None = None) -> tuple[int | None, int | None, int | None, float]:
        """(page_start, page_end, best page, confidence 0-1). (None, None, None, score) when no page matches well.
        The best page is what look_at_pages opens: passages average ~5 pages, and the figure is rarely on the first.
        `after`: the previous passage's start page. Passages are in reading order, so prefer a best page at or after it
        (decks repeat agenda/divider slides, and a duplicate 40 slides away could otherwise win)."""
        if len(self.pages) == 1:
            return self.pages[0], self.pages[0], self.pages[0], 1.0
        if len(tokens(passage)) < 5:
            return None, None, None, 0.0
        cont = self.containment(passage)
        best = max(cont, key=cont.get)
        if after is not None:
            later = [p for p in self.pages if p >= after]
            if later:
                b2 = max(later, key=cont.get)
                if cont[b2] >= 0.8 * cont[best]:
                    best = b2
        if cont[best] < 0.25:
            return None, None, None, round(max(cont[best], 0), 3)
        # grow a CONTIGUOUS block outward from the best page (passages are contiguous spans); step over near-empty
        # pages (-1) but stop at the first real page that doesn't match. Taking min..max of every matching page
        # anywhere in the file gave 26-page ranges.
        thr = max(0.5 * cont[best], min(0.3, cont[best]))
        i = self.pages.index(best)
        lo = hi = i

        def grow(j, step):
            """Extend while pages match; bridge up to 2 weak pages (picture slides carry little text) when a matching
            page follows."""
            last, k, weak = j, j + step, 0
            while 0 <= k < len(self.pages):
                c = cont[self.pages[k]]
                if c == -1.0 or c < thr:
                    weak += 1
                    if weak > 2:
                        break
                else:
                    last, weak = k, 0
                k += step
            return last
        lo, hi = grow(i, -1), grow(i, +1)
        # a passage starts/ends part-way through a page: include a partly contained direct neighbour
        if lo > 0 and cont[self.pages[lo - 1]] >= 0.2:
            lo -= 1
        if hi < len(self.pages) - 1 and cont[self.pages[hi + 1]] >= 0.2:
            hi += 1
        return self.pages[lo], self.pages[hi], best, round(cont[best], 3)


PAGE_KEYS = ("page_start", "page_end", "page_best", "page_confidence", "page_source")


def assign(conn, file_id: str | None = None, only_missing: bool = True, write: bool = True) -> dict:
    """Compute page_start/page_end/page_best for passages (all files, or one) and store them in documents.metadata.
    Returns counts plus accuracy against image-caption page numbers (passages that contain captions).
    Only files whose page index is current are matched (page_index_status hash = the file's text hash): an edited
    file's new passages must not be matched against its old pages (found in review). Rows are written only when
    their values change, and a passage that no longer matches loses stale page keys."""
    cond = ["s.status = 'ok'", "s.content_hash is not distinct from m.content_hash"]
    args: list = []
    if file_id:
        cond.append("m.file_id = %s")
        args.append(file_id)
    if only_missing:            # cheap every-15-minutes call: only files that have a passage without pages
        cond.append("exists (select 1 from documents d2 where d2.metadata->>'file_id' = m.file_id and not d2.metadata ? 'page_start')")
    files = [r[0] for r in conn.execute(f"""select m.file_id from document_metadata m join page_index_status s on s.file_id = m.file_id
                                            where {' and '.join(cond)}""", args).fetchall()]
    stats = Counter()
    if file_id and not files:
        stats["page_index_not_current"] += 1
    for fid in files:
        passages = conn.execute("""select id, content, metadata->>'page_start', metadata->>'page_end', metadata->>'page_best',
                                          metadata->>'page_source'
                                   from documents where metadata->>'file_id' = %s
                                   order by (metadata->>'chunk_index')::int nulls last""", (fid,)).fetchall()
        todo = [p for p in passages if not (only_missing and p[2] is not None)]
        if not todo:
            continue
        pages = dict(conn.execute("select page_no, page_text from page_views where file_id = %s and kind in ('page', 'photo')",
                                  (fid,)).fetchall())
        if not pages:
            stats["no_page_index"] += len(todo)
            continue
        fp = FilePages(pages)
        prev_start = None
        for pid, content, old_start, old_end, old_best, old_source in todo:
            start, end, best, conf = fp.match(content, after=prev_start)
            caps = {c for c in caption_pages(content) if fp.pages[0] <= c <= fp.pages[-1]}   # ignore off-the-end captions
            if caps:                                 # blind check of the text match before captions are merged in
                stats["checked"] += 1
                if start is not None and all(start <= c <= end for c in caps):
                    stats["caption_inside"] += 1
            # Image captions ("Image, page N") are Mistral's own page numbers: they anchor the range. If the text match
            # is far from them (> 2 pages) it's wrong (repeated slide, chart page with no text) and captions win.
            source = "text"
            if caps:
                near = start is not None and min(min(abs(c - start), abs(c - end)) for c in caps) <= 2
                if near:
                    start, end, source = min(start, min(caps)), max(end, max(caps)), "text+caption"
                else:
                    # captions mark the PICTURES; a table or notes before the first / after the last picture sit on
                    # the neighbouring page (a rock-sizing table: p44, its pictures p45-46) -> widen by one page
                    # when there is real text outside the captioned part
                    found = list(IMG_CAPTION.finditer(content))
                    text_before = len(tokens(content[:found[0].start()]))
                    text_after = len(tokens(content[found[-1].end():]))
                    start = max(fp.pages[0], min(caps) - (1 if text_before > 150 else 0))
                    end = min(fp.pages[-1], max(caps) + (1 if text_after > 150 else 0))
                    best, conf, source = min(caps), 0.5, "caption"
            if start is None:
                stats["unmatched"] += 1
                if write and old_start is not None:  # matched before, not now: drop the stale page numbers
                    conn.execute("update documents set metadata = metadata - %s::text[] where id = %s", (list(PAGE_KEYS), pid))
                    stats["cleared"] += 1
                continue
            prev_start = start
            stats["matched"] += 1
            stats[source] += 1
            stats["span_pages"] += end - start + 1
            if (old_start, old_end, old_best, old_source) == (str(start), str(end), str(best), source):
                stats["unchanged"] += 1
                continue
            if write:
                conn.execute("""update documents set metadata = metadata || jsonb_build_object('page_start', %s::int, 'page_end', %s::int,
                                'page_best', %s::int, 'page_confidence', %s::real, 'page_source', %s::text) where id = %s""",
                             (start, end, best, conf, source, pid))
                stats["written"] += 1
    out = dict(stats)
    if stats["checked"]:
        out["caption_accuracy"] = round(stats["caption_inside"] / stats["checked"], 3)
    if stats["matched"]:
        out["avg_span"] = round(stats["span_pages"] / stats["matched"], 2)
    return out


if __name__ == "__main__":      # dry run / evaluation:  .venv/bin/python pages.py [--write]
    import os
    import sys
    import psycopg
    for line in open(os.path.join(os.path.dirname(__file__), ".env")):
        k, _, v = line.strip().partition("=")
        os.environ.setdefault(k, v)
    with psycopg.connect(os.environ["PG_DSN"], autocommit=True) as c:
        print(assign(c, only_missing=False, write="--write" in sys.argv))
