"""
title: Download as Word
author: Knowledge Assistant
version: 0.1.0
description: Adds a "Download as Word" button under each answer. Turns the answer (headings, lists, tables, screenshots, sources) into a .docx and downloads it in the browser. Nothing is stored and nothing leaves the server.
required_open_webui_version: 0.6.0
icon_url: data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0iMS44IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiPjxwYXRoIGQ9Ik0xNCAzSDdhMiAyIDAgMCAwLTIgMnYxNGEyIDIgMCAwIDAgMiAyaDEwYTIgMiAwIDAgMCAyLTJWOHoiLz48cGF0aCBkPSJNMTQgM3Y1aDUiLz48cGF0aCBkPSJNOCAxMmwxLjUgNSAxLjUtNCAxLjUgNCAxLjUtNSIvPjwvc3ZnPg==
"""
# How it works: the answer's Markdown goes through pandoc (installed in the Open WebUI image) to .docx. Screenshots
# are fetched from the image store inside the server network and embedded, so the file is complete offline. The file
# is handed to the browser through Open WebUI's "execute" event (a tiny script that triggers the download), so it is
# never written to disk or to Open WebUI's file store.
import asyncio
import base64
import json
import datetime
import os
import re
import tempfile
import urllib.request

from pydantic import BaseModel, Field


class Action:
    class Valves(BaseModel):
        allowed_image_prefix: str = Field(
            default="http://localhost:8000/storage/v1/object/public/document-images/",
            description="Only screenshots under this address are embedded (the library's image store); any other image is left out",
        )
        image_url_rewrite: str = Field(
            default="http://localhost:8000=>http://host.docker.internal:8000",
            description="FROM=>TO: how the server reaches screenshot links that point at the user's machine",
        )
        draft_note: str = Field(
            default="Draft prepared with the Knowledge Assistant from the document library on {date}. Review before use.",
            description="Italic note at the top of the document ({date} = today). Empty = no note.",
        )
        drop_chat_lines: bool = Field(
            default=True, description="Remove chat-only lines: 'I read … as …' and closing 'If you want, I can …' offers"
        )
        max_image_mb: int = Field(default=10, description="Skip screenshots larger than this")

    def __init__(self):
        self.valves = self.Valves()

    # ---------------------------------------------------------------------------------------------------------------
    def _clean(self, md: str) -> str:
        md = re.sub(r"<details[\s\S]*?</details>", "", md)                      # tool-call / reasoning blocks
        if self.valves.drop_chat_lines:
            md = re.sub(r"^\s*_?I read [^\n]{1,120}tell me if you meant something else\._?\s*\n", "", md, flags=re.I)
            md = re.sub(r"\n+\s*(?:If you(?:'|’)?d? (?:want|like)|Want me to|Would you like)[^\n]*\s*$", "\n", md.rstrip(), flags=re.I)
        return md.strip() + "\n"

    GENERIC = {"summary", "overview", "answer", "steps", "details", "already covered", "recommended additions / updates",
               "recommended additions", "possibly outdated", "suggested wording", "from your documents", "notes", "sources"}

    def _title(self, md: str, question: str) -> str:
        """First meaningful heading; else the answer's own opening line ("To enter a new complaint in …:"); else the
        question. (The question is the user's own typing, typos included, so it comes last.)"""
        for m in re.finditer(r"^\s{0,3}#{1,3}\s+(.+)$", md, re.M):
            t = re.sub(r"[*_`#]", "", m.group(1)).strip()
            if t and t.lower().rstrip(":") not in self.GENERIC and not t.lower().startswith("from "):
                return t[:90]
        first = re.sub(r"[*_`]", "", next((l for l in md.splitlines() if l.strip() and not l.lstrip().startswith(("#", "!", "|", "-", "*", ">"))), "")).strip()
        m = re.match(r"(?:here(?:'|’)s how to|to)\s+(.{8,100}?)[,:]", first, re.I)
        if m:
            t = m.group(1).strip()
            return (t[:1].upper() + t[1:])[:90]
        t = re.sub(r"\s+", " ", question).strip().rstrip("?.!")
        return (t[:1].upper() + t[1:])[:90] or "Knowledge Assistant answer"

    def _polish(self, path: str):
        """Business look: Calibri, full-width tables with columns sized to their content, light grid, shaded header."""
        import math
        from docx import Document
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Pt

        doc = Document(path)
        for st in doc.styles:
            if getattr(st, "type", None) != 1 or st.name.startswith(("Heading", "Title")):   # paragraph styles, not headings
                continue
            rpr = st.element.get_or_add_rPr()
            fonts = rpr.find(qn("w:rFonts"))
            if fonts is None:
                fonts = OxmlElement("w:rFonts")
                rpr.insert(0, fonts)
            for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
                fonts.set(qn(a), "Calibri")
            for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
                fonts.attrib.pop(qn(a), None)
        from docx.shared import Inches
        for sec in doc.sections:                      # pandoc leaves the page size unset: US Letter, 1" margins
            sec.page_width, sec.page_height = Inches(8.5), Inches(11)
            sec.left_margin = sec.right_margin = sec.top_margin = sec.bottom_margin = Inches(1)
        width = Inches(6.5)
        for t in doc.tables:
            rows = t.rows
            n = max(len(r.cells) for r in rows)
            lens, longest = [0.0] * n, [1] * n
            for r in rows:
                for i, c in enumerate(r.cells[:n]):
                    lens[i] += len(c.text) / len(rows)
                    longest[i] = max([longest[i]] + [len(x) for x in c.text.split()])
            # share of the width by typical text length, but never narrower than the column's longest word
            w = [max(max(4.0, min(60.0, x)) ** 0.8, min(14, lw) * 1.1) for x, lw in zip(lens, longest)]
            cols = [int(width * x / sum(w)) for x in w]
            tblPr = t._tbl.tblPr
            for tag in ("w:tblW", "w:tblLayout", "w:tblBorders"):
                for e in tblPr.findall(qn(tag)):
                    tblPr.remove(e)
            tw = OxmlElement("w:tblW"); tw.set(qn("w:w"), "5000"); tw.set(qn("w:type"), "pct"); tblPr.append(tw)
            lay = OxmlElement("w:tblLayout"); lay.set(qn("w:type"), "fixed"); tblPr.append(lay)
            b = OxmlElement("w:tblBorders")
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                e = OxmlElement(f"w:{side}"); e.set(qn("w:val"), "single"); e.set(qn("w:sz"), "4"); e.set(qn("w:color"), "BFBFBF"); b.append(e)
            tblPr.append(b)
            grid = t._tbl.tblGrid
            for gc, cw in zip(grid.findall(qn("w:gridCol")), cols):
                gc.set(qn("w:w"), str(int(cw / 635)))      # EMU -> twips
            for ri, r in enumerate(rows):
                for i, c in enumerate(r.cells[:n]):
                    c.width = cols[i]
                    for r_el in c._tc.iter(qn("w:r")):          # every run, including those inside hyperlinks
                        rpr = r_el.get_or_add_rPr()
                        for tag in ("w:sz", "w:szCs"):
                            e = rpr.find(qn(tag))
                            if e is None:
                                e = OxmlElement(tag); rpr.append(e)
                            e.set(qn("w:val"), "20")
                        if ri == 0 and rpr.find(qn("w:b")) is None:
                            rpr.append(OxmlElement("w:b"))
                    if ri == 0:
                        sh = OxmlElement("w:shd"); sh.set(qn("w:val"), "clear"); sh.set(qn("w:fill"), "EDF1F7")
                        c._tc.get_or_add_tcPr().append(sh)
        doc.save(path)

    def _embed_images(self, md: str) -> str:
        """Screenshots from the library's image store only (a user can edit a message, so arbitrary URLs must never be
        fetched by the server); each is embedded as a data URI, so pandoc can run sandboxed. Every other image reference
        (other hosts, local paths, HTML <img>) is replaced by its alt text."""
        frm, _, to = self.valves.image_url_rewrite.partition("=>")
        allowed = self.valves.allowed_image_prefix
        cap_bytes = self.valves.max_image_mb * 1024 * 1024

        def fetch(m):
            alt, url = m.group(1), m.group(2).strip()
            label = f"*[Screenshot: {alt[:80]}]*" if alt else ""
            if not allowed or not url.startswith(allowed) or ".." in url or "%" in url:   # no (encoded) path tricks
                return label
            src = url.replace(frm, to, 1) if frm and to else url
            try:
                with urllib.request.urlopen(src, timeout=10) as r:
                    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    data = r.read(cap_bytes + 1)
                if ctype not in ("image/png", "image/jpeg", "image/gif", "image/webp") or len(data) > cap_bytes:
                    return label
                cap = alt if len(alt) <= 80 else ""          # model-written alt texts are long descriptions
                return f"![{cap}](data:{ctype};base64,{base64.b64encode(data).decode()})"
            except Exception:
                return f"*[Screenshot not available: {alt[:80]}]*" if alt else ""

        md = re.sub(r"<img\b[^>]*>", "", md, flags=re.I)                          # raw HTML images: never fetched
        md = re.sub(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'(][^)]*)?\)", fetch, md)  # every markdown image, titles included
        return md

    @staticmethod
    def _only_embedded_images(node):
        """Replace any Image whose source is not a data:image/ URI with its alt text (in italics)."""
        if isinstance(node, list):
            return [Action._only_embedded_images(x) for x in node]
        if isinstance(node, dict):
            if node.get("t") == "Image":
                attr, alt, target = node["c"]
                if not str(target[0]).startswith("data:image/"):
                    return {"t": "Emph", "c": alt or [{"t": "Str", "c": "[image]"}]}
                return node
            return {k: Action._only_embedded_images(v) for k, v in node.items()}
        return node

    def build_docx(self, answer: str, question: str = "") -> tuple[str, bytes]:
        import pypandoc

        md = self._clean(answer)
        title = self._title(md, question)
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as d:
            md = self._embed_images(md)
            note = self.valves.draft_note.format(date=today).strip()
            header = f"*{note}*\n\n" if note else ""
            # the first heading becomes the document title, so it isn't repeated in the body
            first_h = re.match(r"\s{0,3}#{1,3}\s+(.+)\n", md)
            used = first_h and re.sub(r"[*_`#]", "", first_h.group(1)).strip()[:90] == title
            body = md[first_h.end():] if used else md
            out = os.path.join(d, "answer.docx")
            # Two steps, so pandoc can never fetch anything (its --sandbox is broken in this version): read Markdown into
            # pandoc's document tree (readers never fetch), drop every image that is not an embedded data image (this
            # also catches reference-style ![x][ref] images), then write the .docx from that tree.
            tree = json.loads(pypandoc.convert_text(header + body, "json", format="gfm"))
            tree = self._only_embedded_images(tree)
            pypandoc.convert_text(json.dumps(tree), "docx", format="json", outputfile=out,
                                  extra_args=["--metadata", f"title={title}"])
            try:
                self._polish(out)
            except Exception:
                pass                                    # plain pandoc output is still a fine document
            with open(out, "rb") as f:
                data = f.read()
        safe = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:70] or "answer"
        return f"{safe}_{today}.docx", data

    # ---------------------------------------------------------------------------------------------------------------
    async def action(self, body: dict, __user__=None, __event_emitter__=None, __event_call__=None):
        msgs = body.get("messages") or []
        if body.get("id"):                               # the message the button was clicked on
            idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("id") == body["id"]), None)
        else:                                            # older clients send no id: the latest answer
            idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "assistant"), None)
        if idx is None or not str(msgs[idx].get("content") or "").strip():
            if __event_emitter__:
                await __event_emitter__({"type": "notification", "data": {"type": "error", "content": "No answer to export."}})
            return
        question = next((str(m.get("content") or "") for m in reversed(msgs[:idx]) if m.get("role") == "user"), "")
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Building the Word document…", "done": False}})
        try:
            # off the event loop: image fetches + pandoc would otherwise stall Open WebUI for every user
            name, data = await asyncio.to_thread(self.build_docx, str(msgs[idx]["content"]), question)
        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Word export failed", "done": True}})
                await __event_emitter__({"type": "notification", "data": {"type": "error", "content": f"Word export failed: {str(e)[:200]}"}})
            return
        b64 = base64.b64encode(data).decode()
        js = ("const b = atob('" + b64 + "'); const u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);"
              " const url = URL.createObjectURL(new Blob([u], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }));"
              " const a = document.createElement('a'); a.href = url; a.download = " + json.dumps(name) + ";"
              " document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 2000); return true;")
        if __event_call__:
            await __event_call__({"type": "execute", "data": {"code": js}})
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Downloaded {name}", "done": True}})
