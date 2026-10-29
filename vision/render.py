"""Turn a source file into the images we embed for visual search ("views").

Every page becomes one whole-page view. Large drawing sheets (long side > 20 in) also get a 4x3 grid of
overlapping tiles plus a title-block crop, because embedders shrink a whole 34x22 in sheet to ~50 dpi, where
notes and dimensions are unreadable. Scanned pages are turned upright with Tesseract's orientation detection.
Each view keeps the page's own text layer (CAD exports carry exact text; OCR'd scans carry rough text).
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 300_000_000      # big sheets are legitimate here

LARGE_SHEET_IN = 20          # long side above this many inches = drawing sheet -> tiles + title block
SHEET_DPI = 150              # master render for sheets; tiles are cut from it (~1.6-1.8 MP each)
PAGE_MAX_PX = 1_800_000      # whole-page view budget (the embedders' native input is ~1.8 MP)
PHOTO_MAX_SIDE = 2048
TILE_COLS, TILE_ROWS, TILE_OVERLAP = 4, 3, 0.12
TITLE_BLOCK = (0.70, 0.66, 1.0, 1.0)      # right/bottom corner, as fractions of the sheet (x0, y0, x1, y1)
SCAN_TEXT_CHARS = 200        # fewer native-text chars than this on a page = treat as a scan (run orientation check)
UPRIGHT_ENOUGH = 900         # word score at 0 deg above this = already upright; skip the 3 rotated reads (~4x faster)


@dataclass
class View:
    page_no: int                      # 1-based
    kind: str                         # page | tile | title | photo
    image: Image.Image
    tile_ix: int | None = None
    bbox: tuple[float, float, float, float] | None = None   # fractions of the page (x0, y0, x1, y1)
    rotation: int = 0                 # degrees we rotated the page to make it upright
    page_w_in: float | None = None
    page_h_in: float | None = None
    page_text: str = ""
    meta: dict = field(default_factory=dict)


def _ocr(img: Image.Image) -> tuple[float, str]:
    """Tesseract sparse-text read. Returns (word score, text). The score sums confidences of real-looking words
    (3+ letters, conf > 60), which is high only when the page is upright; the text is a rough read for keyword search."""
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        img.save(f.name)
        tsv = subprocess.run(["tesseract", f.name, "-", "--psm", "11", "tsv"], capture_output=True, text=True).stdout
    score, words = 0.0, []
    for line in tsv.splitlines()[1:]:
        p = line.split("\t")
        if len(p) != 12 or not p[11].strip():
            continue
        conf = float(p[10] or -1)
        if conf > 40:
            words.append(p[11].strip())
        if conf > 60 and re.fullmatch(r"[A-Za-z]{3,}", p[11].strip()):
            score += conf
    return score, " ".join(words)


def _word_score(img: Image.Image) -> float:
    return _ocr(img)[0]


def upright_rotation(img: Image.Image, with_text: bool = False):
    """Degrees (counter-clockwise) that make a scanned page upright, or 0.

    Tesseract's own orientation check (--psm 0) gives up on chart pages (confidence ~0.1 on a sideways IDF chart),
    so instead read the page in all four orientations and keep the one with the most confident real words.
    Figure 4-1 sideways: 0 deg -> 380, 90 deg -> 1937. Rotate only on a clear win (2.5x AND +400): chart books
    that print a sideways chart on an upright page (XY-418 Chart 10: 0 -> 245, 246 -> 590) stay as printed.
    """
    small = img.convert("L")
    small.thumbnail((1600, 1600))
    reads = {0: _ocr(small)}
    if reads[0][0] >= UPRIGHT_ENOUGH:          # reads well as is (e.g. a 1972 manual page: 1807): skip the other 3 reads
        return (0, reads[0][1]) if with_text else 0
    reads.update({deg: _ocr(small.rotate(deg, expand=True)) for deg in (90, 270, 180)})
    scores = {d: r[0] for d, r in reads.items()}
    best = max(scores, key=scores.get)
    clear_win = scores[best] >= 2.5 * scores[0] and scores[best] - scores[0] >= 400
    deg = best if best and clear_win else 0
    return (deg, reads[deg][1]) if with_text else deg


def _is_scan(page, w_pt: float, h_pt: float) -> bool:
    """A page whose content is one image covering most of it (a scan, even if it carries an OCR text layer)."""
    try:
        for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=2):
            l, b, r, t = obj.get_bounds()
            if (r - l) * (t - b) >= 0.6 * w_pt * h_pt:
                return True
    except Exception:
        pass
    return False


def _bounded_text(page, frac_box: tuple[float, float, float, float]) -> str:
    """Native text inside a box given as fractions of the page *as displayed* (x0, y0, x1, y1, y down).
    Maps through the page's /Rotate so it works for sheets stored portrait-with-rotation as well."""
    try:
        w0, h0 = page.get_size()          # unrotated MediaBox size, PDF coords (y up)
        r = page.get_rotation()
        u0, v0, u1, v1 = frac_box

        def to_pdf(u, v):
            if r == 90:
                return v * w0, u * h0
            if r == 180:
                return (1 - u) * w0, v * h0
            if r == 270:
                return (1 - v) * w0, (1 - u) * h0
            return u * w0, (1 - v) * h0

        (xa, ya), (xb, yb) = to_pdf(u0, v0), to_pdf(u1, v1)
        return page.get_textpage().get_text_bounded(left=min(xa, xb), bottom=min(ya, yb), right=max(xa, xb), top=max(ya, yb))
    except Exception:
        return ""


def _fit(img: Image.Image, max_px: int) -> Image.Image:
    w, h = img.size
    if w * h <= max_px:
        return img
    s = (max_px / (w * h)) ** 0.5
    return img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)


def views_for_pdf(path: str, auto_rotate: bool = True) -> list[View]:
    pdf = pdfium.PdfDocument(path)
    views: list[View] = []
    for i in range(len(pdf)):
        page = pdf[i]
        w_pt, h_pt = page.get_size()
        if page.get_rotation() in (90, 270):
            w_pt, h_pt = h_pt, w_pt
        w_in, h_in = w_pt / 72, h_pt / 72
        try:
            text = page.get_textpage().get_text_range()
        except Exception:
            text = ""
        sheet = max(w_in, h_in) > LARGE_SHEET_IN
        if sheet:
            master = page.render(scale=SHEET_DPI / 72).to_pil().convert("RGB")
        else:
            scale = min(200 / 72, (PAGE_MAX_PX / (w_pt * h_pt)) ** 0.5)
            master = page.render(scale=scale).to_pil().convert("RGB")
        rotation = 0
        # Only real scans (one image covering the page) can be sideways. Born-digital pages with little text (converted
        # slides) used to get the 4-way check too, which made decks ~2x slower to index.
        if auto_rotate and not sheet and _is_scan(page, w_pt, h_pt):
            rotation, ocr_text = upright_rotation(master, with_text=True)
            if rotation:
                master = master.rotate(rotation, expand=True)
            if len(text.strip()) < SCAN_TEXT_CHARS:      # no usable text layer: keep Tesseract's read for keyword search
                text = ocr_text
        common = dict(page_no=i + 1, rotation=rotation, page_w_in=round(w_in, 2), page_h_in=round(h_in, 2), page_text=text)
        views.append(View(kind="page", image=_fit(master, PAGE_MAX_PX), bbox=(0, 0, 1, 1), **common))
        if sheet:
            W, H = master.size
            views += _tiles(master, common)
            x0, y0, x1, y1 = TITLE_BLOCK
            title = {**common, "page_text": _bounded_text(page, TITLE_BLOCK)}   # exact title-block text (sheet no., title)
            views.append(View(kind="title", image=_fit(master.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))), PAGE_MAX_PX),
                              bbox=TITLE_BLOCK, **title))
        page.close()
    pdf.close()
    return views


def _tiles(master: Image.Image, common: dict) -> list[View]:
    W, H = master.size
    tw, th = W / (TILE_COLS - (TILE_COLS - 1) * TILE_OVERLAP), H / (TILE_ROWS - (TILE_ROWS - 1) * TILE_OVERLAP)
    out = []
    for r in range(TILE_ROWS):
        for c in range(TILE_COLS):
            x0, y0 = c * tw * (1 - TILE_OVERLAP), r * th * (1 - TILE_OVERLAP)
            box = (int(x0), int(y0), int(min(W, x0 + tw)), int(min(H, y0 + th)))
            out.append(View(kind="tile", tile_ix=r * TILE_COLS + c, image=master.crop(box),
                            bbox=(box[0] / W, box[1] / H, box[2] / W, box[3] / H), **common))
    return out


def views_for_image(path: str) -> list[View]:
    """Photos -> one view. A large, mostly-white image is a scanned drawing sheet -> whole view + tiles + title block."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    w, h = img.size
    if w * h >= 6_000_000:
        small = img.convert("L").resize((400, 300))
        white = sum(1 for p in small.getdata() if p > 200) / (400 * 300)
        if white >= 0.55:
            common = dict(page_no=1, rotation=0, page_text="")
            x0, y0, x1, y1 = TITLE_BLOCK
            return ([View(kind="page", image=_fit(img, PAGE_MAX_PX), bbox=(0, 0, 1, 1), **common)] + _tiles(img, common)
                    + [View(kind="title", image=_fit(img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))), PAGE_MAX_PX),
                            bbox=TITLE_BLOCK, **common)])
    img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    return [View(page_no=1, kind="photo", image=img, bbox=(0, 0, 1, 1))]


def render_region(path: str, page_no: int, box: tuple[float, float, float, float], view_rotation: int = 0,
                  max_side: int = 1800, max_dpi: int = 400) -> Image.Image:
    """A region of a page rendered fresh from the source at up to max_dpi. Stored views are ~140 dpi, but the scans are
    300 dpi: a dense chart's small curve labels (a "1" that looks like "/") are lost at 140 and legible at 300.
    box = fractions (x0, y0, x1, y1) of the page as stored, i.e. after our upright rotation (view_rotation, CCW)."""
    def unrot(u, v):                     # stored (upright) view -> page as pdfium renders it
        if view_rotation == 90:
            return 1 - v, u
        if view_rotation == 270:
            return v, 1 - u
        if view_rotation == 180:
            return 1 - u, 1 - v
        return u, v
    (a, b), (c, d) = unrot(box[0], box[1]), unrot(box[2], box[3])
    x0, x1, y0, y1 = max(0, min(a, c)), min(1, max(a, c)), max(0, min(b, d)), min(1, max(b, d))
    if path.lower().endswith(".pdf"):
        pdf = pdfium.PdfDocument(path)
        page = pdf[page_no - 1]
        W, H = page.get_size()            # displayed size in points (page /Rotate applied), same frame as render()
        rw, rh = (x1 - x0) * W, (y1 - y0) * H
        scale = min(max_dpi / 72, max_side / max(rw, rh, 1))
        img = page.render(scale=scale, crop=(x0 * W, (1 - y1) * H, (1 - x1) * W, y0 * H)).to_pil().convert("RGB")
        page.close()
        pdf.close()
    else:
        im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        Wp, Hp = im.size
        img = im.crop((int(x0 * Wp), int(y0 * Hp), int(x1 * Wp), int(y1 * Hp)))
        img.thumbnail((max_side, max_side))
    return img.rotate(view_rotation, expand=True) if view_rotation else img


def views_for_file(path: str) -> list[View]:
    p = path.lower()
    if p.endswith(".pdf"):
        return views_for_pdf(path)
    if p.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")):
        return views_for_image(path)
    raise ValueError(f"unsupported file type: {path}")
