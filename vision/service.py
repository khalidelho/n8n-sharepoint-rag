#!/usr/bin/env python3
"""Vision service: visual page search for the private RAG stack. Runs natively on the Mac (GPU via PyTorch MPS),
listens on 127.0.0.1 only; n8n reaches it at http://host.docker.internal:18500. Nothing leaves the machine.

  POST /index        multipart file + file_id, file_name, content_hash  -> queued; worker renders, embeds, stores
  POST /index_path   {path, file_id, file_name, content_hash}          -> same, for a file already on disk
  GET  /status       queue length, current job, model
  POST /search       {query, k}                                         -> best pages (visual + identifier + keyword, fused)
  POST /images       {pages: ["file_id:page[:tile]", ...], max_images}  -> base64 PNGs to show a vision model

Config (vision/.env, chmod 600; see .env.example): VISION_MODEL, PG_DSN, PAGES_DIR, SOURCES_DIR, VISION_PORT, VISION_TOKEN.
Run: .venv/bin/python service.py   (launchd: vision/com.example.rag-vision.plist)
"""
from __future__ import annotations

import base64
import io
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import psycopg
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel

HERE = Path(__file__).parent
for line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
MODEL = os.environ.get("VISION_MODEL", "gemma")
PG_DSN = os.environ["PG_DSN"]
PAGES = Path(os.environ.get("PAGES_DIR", HERE / "pages" / "library"))
PORT = int(os.environ.get("VISION_PORT", "18500"))

from models import Embedder  # noqa: E402  (after env, before app)
from render import render_region, views_for_file  # noqa: E402
import pages as passage_pages  # noqa: E402

app = FastAPI(title="RAG vision service")
TOKEN = os.environ.get("VISION_TOKEN", "")
STAGING = HERE / "staging"          # /index_path only reads files placed here
SOURCES = Path(os.environ.get("SOURCES_DIR", HERE / "sources"))   # originals, for full-resolution zooms


@app.middleware("http")
async def guard(request, call_next):
    """No web page may call this service: browsers send an Origin header (a multipart POST to /index needs no CORS
    preflight, so without this any site the user visits could inject pages). Optional shared token via VISION_TOKEN."""
    from fastapi.responses import JSONResponse
    if request.headers.get("origin"):
        return JSONResponse({"error": "browser requests are not allowed"}, status_code=403)
    if TOKEN and request.headers.get("x-vision-token") != TOKEN:
        return JSONResponse({"error": "missing or wrong token"}, status_code=401)
    return await call_next(request)
gpu = threading.Lock()                     # one model, one GPU: indexing and query embedding take turns
jobs: queue.Queue = queue.Queue()
state = {"current": None, "done": 0, "failed": 0, "started": time.time()}
embedder: Embedder | None = None


def db():
    return psycopg.connect(PG_DSN, autocommit=True)


def vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v.tolist()) + "]"


def set_status(file_id, file_name, content_hash, status, **kw):
    attempts_reset = status in ("queued", "ok")
    with db() as c:
        c.execute("""insert into page_index_status (file_id, file_name, content_hash, status, pages, views, message, seconds, updated_at)
                     values (%s,%s,%s,%s,%s,%s,%s,%s, now())
                     on conflict (file_id) do update set file_name=excluded.file_name, content_hash=excluded.content_hash,
                       status=excluded.status, pages=excluded.pages, views=excluded.views, message=excluded.message,
                       seconds=excluded.seconds, updated_at=now()""",
                  (file_id, file_name, content_hash, status, kw.get("pages"), kw.get("views"), kw.get("message"), kw.get("seconds")))
        if attempts_reset:
            c.execute("update page_index_status set attempts=0 where file_id=%s", (file_id,))


# ---- indexing ---------------------------------------------------------------------------------------------------
def index_file(path: str, file_id: str, file_name: str, content_hash: str | None):
    t = time.time()
    views = views_for_file(path)
    out_dir = PAGES / file_id
    tmp_dir = PAGES / f".{file_id}.new"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    rows = []
    for v in views:
        name = f"{v.page_no:04d}_{v.kind}" + (f"_t{v.tile_ix:02d}" if v.tile_ix is not None else "") + ".png"
        v.image.save(tmp_dir / name)
        rows.append((v, f"{file_id}/{name}"))
    vecs = []
    for i in range(0, len(views), 16):
        with gpu:
            vecs.append(embedder.images([v.image for v in views[i:i + 16]]))
    vecs = np.concatenate(vecs) if vecs else np.zeros((0, embedder.dims))
    with db() as c, c.transaction():
        c.execute("delete from page_views where file_id = %s", (file_id,))
        with c.cursor() as cur:
            cur.executemany(
                """insert into page_views (file_id, file_name, page_no, kind, tile_ix, bbox, rotation, page_w_in, page_h_in,
                                           image_path, page_text, embed_model, embedding)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::halfvec)""",
                [(file_id, file_name, v.page_no, v.kind, v.tile_ix, list(v.bbox) if v.bbox else None, v.rotation, v.page_w_in,
                  v.page_h_in, rel, (v.page_text or None) if v.kind in ("page", "photo", "title") else None, embedder.name,
                  vec_literal(vecs[i])) for i, (v, rel) in enumerate(rows)])
    shutil.rmtree(out_dir, ignore_errors=True)
    tmp_dir.rename(out_dir)
    pages = len({v.page_no for v in views})
    keep_source(path, file_id)
    set_status(file_id, file_name, content_hash, "ok", pages=pages, views=len(views), seconds=round(time.time() - t, 1))
    try:            # after the status: assign() only trusts page views whose hash matches the file's current text
        with db() as c:
            passage_pages.assign(c, file_id=file_id, only_missing=False)
    except Exception:
        traceback.print_exc()
    return pages, len(views)


def keep_source(path: str, file_id: str):
    """Store the (converted) original so /crop can render regions at full resolution."""
    SOURCES.mkdir(parents=True, exist_ok=True)
    for old in SOURCES.glob(f"{file_id}.*"):
        old.unlink(missing_ok=True)
    shutil.copyfile(path, SOURCES / f"{file_id}{Path(path).suffix.lower()}")
    with db() as c:
        c.execute("update page_index_status set has_source = true where file_id = %s", (file_id,))


def source_of(file_id: str) -> Path | None:
    found = sorted(SOURCES.glob(f"{file_id}.*"))
    return found[0] if found else None


def worker():
    while True:
        job = jobs.get()
        state["current"] = {k: job[k] for k in ("file_id", "file_name")}
        with db() as c:      # if this file crashes the process, the restart sees attempts and won't loop forever
            c.execute("update page_index_status set status='indexing', attempts=attempts+1, updated_at=now() where file_id=%s",
                      (job["file_id"],))
        try:
            if job.get("source_only"):
                keep_source(job["path"], job["file_id"])
                with db() as c:
                    c.execute("update page_index_status set status='ok', attempts=0, message=null, updated_at=now() where file_id=%s",
                              (job["file_id"],))
            else:
                index_file(job["path"], job["file_id"], job["file_name"], job.get("content_hash"))
            state["done"] += 1
        except Exception as e:
            state["failed"] += 1
            set_status(job["file_id"], job["file_name"], job.get("content_hash"), "failed", message=f"{type(e).__name__}: {e}"[:500])
            traceback.print_exc()
        finally:
            if job.get("cleanup"):
                Path(job["path"]).unlink(missing_ok=True)
            state["current"] = None
            jobs.task_done()


@app.on_event("startup")
def startup():
    global embedder
    PAGES.mkdir(parents=True, exist_ok=True)
    embedder = Embedder(MODEL)
    # Resume: files that were queued when the service last stopped (their path is kept in page_index_status.message)
    with db() as c:
        c.execute("""update page_index_status set status='failed', message='crashed the service twice while indexing; skipped',
                     updated_at=now() where status='indexing' and attempts >= 2""")
        for fid, fname, chash, path in c.execute("""select file_id, file_name, content_hash, message from page_index_status
                                                    where status in ('queued','indexing') order by updated_at""").fetchall():
            if path and Path(path).exists():
                jobs.put({"path": path, "file_id": fid, "file_name": fname, "content_hash": chash,
                          "cleanup": Path(path).name.startswith("vision-")})
    print(f"resumed {jobs.qsize()} queued files", flush=True)
    threading.Thread(target=worker, daemon=True).start()


@app.get("/status")
def status():
    return {"model": MODEL, "dims": embedder.dims if embedder else None, "queued": jobs.qsize(), **state}


@app.post("/index")
async def index_upload(file: UploadFile = File(...), file_id: str = Form(...), file_name: str = Form(...),
                       content_hash: str = Form(None), ext: str = Form(None), source_only: str = Form(None)):
    # ext = type of the uploaded bytes (n8n sends "pdf" for Office files it converted with Gotenberg)
    suffix = "." + ext.lower().lstrip(".") if ext else (Path(file.filename or file_name).suffix.lower() or ".pdf")
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="vision-")
    with os.fdopen(fd, "wb") as f:
        shutil.copyfileobj(file.file, f)
    only = str(source_only).lower() in ("true", "1", "yes")
    if only:   # pages already indexed and unchanged: just keep the original (no status change visible to search)
        jobs.put({"path": tmp, "file_id": file_id, "file_name": file_name, "content_hash": content_hash, "cleanup": True, "source_only": True})
        return {"queued": True, "source_only": True, "position": jobs.qsize()}
    set_status(file_id, file_name, content_hash, "queued", message=tmp)      # path kept so a restart can resume
    jobs.put({"path": tmp, "file_id": file_id, "file_name": file_name, "content_hash": content_hash, "cleanup": True})
    return {"queued": True, "position": jobs.qsize()}


class IndexPath(BaseModel):
    path: str
    file_id: str
    file_name: str
    content_hash: str | None = None


@app.post("/index_path")
def index_path(req: IndexPath):
    path = Path(req.path).resolve()
    if STAGING.resolve() not in path.parents or not path.exists():
        raise HTTPException(403, f"only files under {STAGING} can be indexed by path")
    set_status(req.file_id, req.file_name, req.content_hash, "queued", message=req.path)
    jobs.put({**req.model_dump(), "cleanup": False})
    return {"queued": True, "position": jobs.qsize()}


@app.post("/prune")
def prune():
    """Ingestion's 'forget deleted file' clears documents/document_metadata; this clears the page index to match."""
    with db() as c:
        gone = c.execute("""select file_id from page_index_status s
                            where not exists (select 1 from document_metadata m where m.file_id = s.file_id)""").fetchall()
        total = c.execute("select count(*) from page_index_status").fetchone()[0]
        if len(gone) > max(10, total // 10):   # catalog truncated/rebuilt? never wipe the index by accident
            return {"pruned": [], "refused": f"{len(gone)} of {total} files look deleted; refusing a mass prune"}
        for (fid,) in gone:
            c.execute("delete from page_views where file_id=%s", (fid,))
            c.execute("delete from page_index_status where file_id=%s", (fid,))
            shutil.rmtree(PAGES / fid, ignore_errors=True)
            for src in SOURCES.glob(f"{fid}.*"):
                src.unlink(missing_ok=True)
    return {"pruned": [g[0] for g in gone]}


class AssignReq(BaseModel):
    file_id: str | None = None
    only_missing: bool = True


@app.post("/assign_pages")
def assign_pages(req: AssignReq):
    """Page numbers for text passages (documents.metadata page_start/page_end), matched against this index's page
    text. Called by the Page index workflow every run (only passages still missing pages), and after each file is
    indexed. Ingestion may write a file's passages before or after its pages are indexed; either order converges."""
    with db() as c:
        return passage_pages.assign(c, file_id=req.file_id, only_missing=req.only_missing)


# ---- search -----------------------------------------------------------------------------------------------------
ID_PATTERNS = [
    r"\b[A-Z]{1,4}-\d{1,3}[A-Z]?\b",                                              # sheet numbers: C-08, GN-01, C-03
    r"\b(?:figure|fig\.?|chart|table|plate|exhibit|sheet|detail)\s*[A-Z]?\d+(?:[-–.]\s?[A-Z0-9]+)?\b",  # Figure 4-1, Chart 39
    r"\b\d{1,3}\+\d{2}(?:\.\d+)?\b",                                                  # stations: 52+33.4
]
STOP = set("the a an of for to in on at by with and or what which how does is are was were from this that these those "
           "show shows shown sheet plan set page document documents give tell me about using use per according".split())


def identifiers(q: str) -> list[str]:
    found = []
    for p in ID_PATTERNS:
        for m in re.finditer(p, q, re.I):
            s = re.sub(r"\s+", " ", m.group(0)).strip()
            if s.lower().split(" ")[0] in ("sheet", "detail") and not re.search(r"\d", s.split(" ", 1)[-1]):
                continue
            found.append(s)
    return list(dict.fromkeys(found))


def id_regex(ident: str) -> str:
    """Postgres regex for an identifier: case-insensitive, flexible spacing/dash, word-bounded."""
    parts = re.split(r"[\s\-–.]+", ident)
    return r"\m" + r"[\s\-–.]*".join(re.escape(p) for p in parts if p) + r"\M"


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower().replace("_", " ")) if (len(w) >= 3 or w.isdigit()) and w not in STOP}


FILE_STOP = {"pdf", "docx", "doc", "pptx", "jpg", "png", "final", "version", "draft", "city", "page", "pages", "plans", "plan"}


def named_files(c, query: str) -> list[tuple[str, str, int]]:
    """Files whose name shares 2+ meaningful tokens with the question (e.g. "hydrology manual ... vol 2" ->
    'Agency_Manual Vol 2 Hydrology.pdf'). Returns up to 2 best (file_id, file_name, matched tokens)."""
    q = _tokens(query) - FILE_STOP
    # document codes (PR1234, XY-418, ZW5062) name a file on their own: compare with separators removed
    codes = {re.sub(r"[^a-z0-9]", "", m.lower()) for m in re.findall(r"\b[A-Za-z]{1,5}[-\s]?\d{2,6}\b", query)}
    scored = []
    for fid, fname in c.execute("select file_id, file_name from page_index_status where status = 'ok'").fetchall():
        common = q & (_tokens(fname) - FILE_STOP)
        flat = re.sub(r"[^a-z0-9]", "", fname.lower())
        score = len(common) + 3 * sum(1 for code in codes if len(code) >= 4 and code in flat)
        if score >= 2 and (any(not w.isdigit() for w in common) or score >= 3):  # numbers alone can match dates
            scored.append((score, fid, fname))
    scored.sort(reverse=True)
    if not scored:
        return []
    best = scored[0][0]
    return [(fid, fname, m) for m, fid, fname in scored[:2] if m >= max(2, best - 1)]


class SearchReq(BaseModel):
    query: str
    k: int = 6


@app.post("/search")
def search(req: SearchReq):
    t = time.time()
    with gpu:
        qv = embedder.queries([req.query])[0]
    lists: dict[str, list[tuple]] = {}
    weights_extra: dict[str, float] = {}
    id_rx: list[tuple[str, str]] = []
    info: dict[tuple, dict] = {}
    with db() as c:
        c.execute("set hnsw.ef_search = 200")
        rows = c.execute("""select file_id, file_name, page_no, kind, tile_ix, 1 - (embedding <=> %s::halfvec) as sim
                            from page_views order by embedding <=> %s::halfvec limit 120""",
                         (vec_literal(qv), vec_literal(qv))).fetchall()
        seen = []
        for fid, fname, pno, kind, tix, sim in rows:
            key = (fid, pno)
            if key not in info:
                info[key] = {"file_id": fid, "file_name": fname, "page_no": pno, "best_view": kind, "tile_ix": tix,
                             "visual_sim": round(float(sim), 3), "why": []}
                seen.append(key)
        lists["visual"] = seen
        for ident in identifiers(req.query):
            # "Figure 4-1" on a scanned title block often reads as "FIGURE" and "4-1" on separate lines, so for
            # word+number identifiers require both anywhere on the page; figure pages (short text) rank above text
            # pages that merely cite the figure.
            m = re.match(r"(?i)(figure|fig\.?|chart|table|plate|exhibit|sheet|detail)\s*(.+)", ident)
            if m:   # word and number within ~120 chars, either order (the 1972 manual numbers its pages "2-26", too)
                w, num = r"\m" + m.group(1).rstrip(".")[:3] + r"\w*\.?", id_regex(m.group(2))
                num_rx, word_rx = f"({w}.{{0,120}}{num})|({num}.{{0,120}}{w})", r"."
            else:
                num_rx, word_rx = id_regex(ident), r"."
            id_rx.append((num_rx, word_rx))
            hits = c.execute("""select file_id, file_name, page_no, bool_or(kind = 'title') as in_title, min(length(page_text))
                                from page_views where kind in ('page', 'photo', 'title') and page_text ~* %s and page_text ~* %s
                                group by file_id, file_name, page_no order by bool_or(kind = 'title') desc, min(length(page_text) + 400 * greatest(regexp_count(page_text, '\\m(fig(ure)?|chart|table|plate)\\.?\\s*[A-Z]?\\d', 1, 'i') - 1, 0)) asc
                                limit 30""", (num_rx, word_rx)).fetchall()
            ranked = []
            for fid, fname, pno, in_title, _ in hits:
                key = (fid, pno)
                info.setdefault(key, {"file_id": fid, "file_name": fname, "page_no": pno, "best_view": "page", "tile_ix": None,
                                      "visual_sim": None, "why": []})
                info[key]["why"].append(f"{ident} in {'title block' if in_title else 'page text'}")
                ranked.append(key)
            lists[f"id:{ident}"] = ranked
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", req.query) if w.lower() not in STOP]
        if words:
            tsq = " | ".join(dict.fromkeys(w.lower() for w in words))
            kw = c.execute("""select file_id, file_name, page_no, max(ts_rank_cd(page_tsv, q)) as r
                              from page_views, to_tsquery('english', %s) q
                              where kind in ('page', 'photo', 'title') and page_tsv @@ q
                              group by file_id, file_name, page_no order by r desc limit 40""", (tsq,)).fetchall()
            lists["keywords"] = []
            for fid, fname, pno, _ in kw:
                key = (fid, pno)
                info.setdefault(key, {"file_id": fid, "file_name": fname, "page_no": pno, "best_view": "page", "tile_ix": None,
                                      "visual_sim": None, "why": []})
                lists["keywords"].append(key)
        # The question names a document ("the hydrology manual (Vol 2)", "PR1234 plan set", "XY-418"):
        # also search inside that file, so its own page beats copies of the figure in other decks.
        for fid, fname, strength in named_files(c, req.query):
            ranked = []
            # pages of this file carrying the named figure/sheet number, figure pages (little other text) first
            for num_rx, word_rx in id_rx:
                for (pno,) in c.execute("""select page_no from page_views where file_id = %s and kind in ('page','title','photo')
                                           and page_text ~* %s and page_text ~* %s
                                           group by page_no order by bool_or(kind='title') desc, min(length(page_text) + 400 * greatest(regexp_count(page_text, '\\m(fig(ure)?|chart|table|plate)\\.?\\s*[A-Z]?\\d', 1, 'i') - 1, 0)) asc limit 5""",
                                        (fid, num_rx, word_rx)).fetchall():
                    key = (fid, pno)
                    info.setdefault(key, {"file_id": fid, "file_name": fname, "page_no": pno, "best_view": "page", "tile_ix": None,
                                          "visual_sim": None, "why": []})
                    if key not in ranked:
                        ranked.append(key)
            rows = c.execute("""select page_no, kind, tile_ix, 1 - (embedding <=> %s::halfvec) from page_views where file_id = %s
                                order by embedding <=> %s::halfvec limit 40""", (vec_literal(qv), fid, vec_literal(qv))).fetchall()
            for pno, kind, tix, sim in rows:
                key = (fid, pno)
                if key not in info:
                    info[key] = {"file_id": fid, "file_name": fname, "page_no": pno, "best_view": kind, "tile_ix": tix,
                                 "visual_sim": round(float(sim), 3), "why": []}
                if key not in ranked:
                    ranked.append(key)
            for key in ranked[:3]:
                info[key]["why"].append("in the document named in the question")
            lists[f"file:{fid}"] = ranked
            weights_extra[f"file:{fid}"] = 1.0 + 0.5 * min(strength, 3)
        weights = {"visual": 1.0, "keywords": 0.5, **weights_extra}
        fused: dict[tuple, float] = {}
        for name, keys in lists.items():
            w = weights.get(name, 2.0)          # identifier hits (sheet/figure numbers) weigh most
            for rank, key in enumerate(keys):
                fused[key] = fused.get(key, 0) + w / (60 + rank + 1)
        # Show duplicate pages once: the same slide often sits in several copies of a deck (7 Rational Method decks
        # each reproduce Figure 4-1). Same page text = same page; keep the best-ranked copy.
        top, seen_text = [], set()
        live = {r[0] for r in c.execute("select file_id from document_metadata where file_id = any(%s)",
                                        (list({k[0] for k in fused}),)).fetchall()}
        for key in sorted(fused, key=fused.get, reverse=True):
            if key[0] not in live:
                continue
            t_ = c.execute("""select left(regexp_replace(coalesce(page_text, ''), '\\s+', ' ', 'g'), 400) from page_views
                              where file_id=%s and page_no=%s and kind in ('page','photo') limit 1""", key).fetchone()
            sig = (t_[0] if t_ else "").strip().lower()
            if len(sig) >= 40 and sig in seen_text:
                continue
            seen_text.add(sig)
            top.append(key)
            if len(top) >= req.k:
                break
        out = []
        for key in top:
            r = info[key]
            snippet = c.execute("""select coalesce((select page_text from page_views where file_id=%s and page_no=%s and kind='title' limit 1),
                                                   (select page_text from page_views where file_id=%s and page_no=%s and kind in ('page','photo') limit 1))""",
                                (key[0], key[1], key[0], key[1])).fetchone()[0] or ""
            r["snippet"] = " ".join(snippet.split())[:200]
            r["ref"] = f"{r['file_id']}:{r['page_no']}" + (f":{r['tile_ix']}" if r["best_view"] == "tile" and r["tile_ix"] is not None else "")
            r["score"] = round(fused[key] * 1000, 2)
            out.append(r)
    return {"query": req.query, "identifiers": identifiers(req.query), "results": out, "ms": int((time.time() - t) * 1000)}


# ---- images for the vision model ---------------------------------------------------------------------------------
class ImagesReq(BaseModel):
    pages: list[str]                # "file_id:page" or "file_id:page:tile"
    max_images: int = 16
    max_side: int = 2048            # whole pages / photos
    tile_max_side: int = 1100       # sheet tiles (~110 dpi: small notes and dimensions stay readable)
    question: str | None = None     # kept for logging / future use


def _png_b64(path: Path, max_side: int) -> tuple[str, tuple[int, int]]:
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode(), im.size


@app.post("/images")
def images(req: ImagesReq):
    """Page images for the vision model. A whole 34x22 sheet shrinks to ~60 dpi in one image, and choosing tiles by
    embedding similarity picked the wrong area (it can't map "end of the profile" to a position). So the first sheet
    looked at comes as overview + all 12 tiles in grid order; other refs come as the page (+ the tile search matched)."""
    out, sheet_done = [], False
    refs = [r for r in req.pages if r.count(":") >= 1][:3]
    with db() as c:
        for ref in refs:
            parts = ref.strip().split(":")
            fid, pno = parts[0], int(parts[1])
            tile = int(parts[2]) if len(parts) > 2 and parts[2] != "" else None
            rows = c.execute("""select kind, tile_ix, image_path, file_name from page_views
                                where file_id=%s and page_no=%s and kind in ('page','photo') limit 1""", (fid, pno)).fetchall()
            tiles = c.execute("""select kind, tile_ix, image_path, file_name from page_views
                                 where file_id=%s and page_no=%s and kind='tile' order by tile_ix""", (fid, pno)).fetchall()
            if tiles and not sheet_done:
                rows += tiles                     # the whole grid, so the model can find any area of the sheet
                sheet_done = True
            elif tile is not None:
                rows += [t for t in tiles if t[1] == tile]
            for kind, tix, rel, fname in rows:
                if len(out) >= req.max_images:
                    break
                b64, size = _png_b64(PAGES / rel, req.tile_max_side if kind == "tile" else req.max_side)
                out.append({"ref": ref, "file_name": fname, "page_no": pno, "kind": kind, "tile_ix": tix, "size": size, "png_b64": b64})
    return {"images": out}


class CropReq(BaseModel):
    file_id: str
    page_no: int
    kind: str = "page"              # page | photo | tile | title
    tile_ix: int | None = None
    box: list[float]                # x0, y0, x1, y1 as fractions of that image
    max_side: int = 1800


@app.post("/crop")
def crop(req: CropReq):
    """Zoom for the vision model: a region of a page image, enlarged. Reading a value off a dense chart (8 similar
    curves on a 0.2 in/hr grid) was unstable from the whole page; vision models read fine detail better up close."""
    if len(req.box) != 4:
        raise HTTPException(400, "box must be [x0, y0, x1, y1]")
    with db() as c:
        row = c.execute("""select image_path, bbox, rotation from page_views where file_id=%s and page_no=%s and kind=%s
                           and tile_ix is not distinct from %s limit 1""", (req.file_id, req.page_no, req.kind, req.tile_ix)).fetchone()
    if not row:
        raise HTTPException(404, "no such page view")
    im = Image.open(PAGES / row[0]).convert("RGB")
    W, H = im.size
    x0, y0, x1, y1 = (min(max(float(v), 0.0), 1.0) for v in req.box)
    x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    pad = 0.03                                                    # a little context around the requested box
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(1, x1 + pad), min(1, y1 + pad)
    if x1 - x0 < 0.08:
        cx = (x0 + x1) / 2; x0, x1 = max(0, cx - 0.04), min(1, cx + 0.04)
    if y1 - y0 < 0.08:
        cy = (y0 + y1) / 2; y0, y1 = max(0, cy - 0.04), min(1, cy + 0.04)
    src, source_used = source_of(req.file_id), False
    if src is not None:
        try:   # map the box from this view (tile/title/page) to the whole page, then render from the original
            bx = row[1] or [0, 0, 1, 1]
            pb = (bx[0] + x0 * (bx[2] - bx[0]), bx[1] + y0 * (bx[3] - bx[1]), bx[0] + x1 * (bx[2] - bx[0]), bx[1] + y1 * (bx[3] - bx[1]))
            region = render_region(str(src), req.page_no, pb, view_rotation=row[2] or 0, max_side=req.max_side)
            source_used = True
        except Exception:
            traceback.print_exc()
    if not source_used:
        region = im.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
        s = req.max_side / max(region.size)
        region = region.resize((max(1, int(region.width * s)), max(1, int(region.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    region.save(buf, format="PNG", optimize=True)
    return {"box": [round(v, 3) for v in (x0, y0, x1, y1)], "size": region.size, "from_source": source_used,
            "zoom": round(max(region.size) / max(1, max((x1 - x0) * W, (y1 - y0) * H)), 2),
            "png_b64": base64.b64encode(buf.getvalue()).decode()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
