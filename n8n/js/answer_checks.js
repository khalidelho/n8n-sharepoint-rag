// Layer 1 of the error flow (design 2026-10-27, part 4): check an answer against the tool results the agent actually
// got, before it is sent. Flags only; nothing is rewritten except that broken screenshots are removed.
// Plain ES2020, no imports: n8n Code nodes paste this whole file unchanged ("Extract claims" calls extractClaims,
// "Check the answer" calls applyChecks) and node --test loads it through module.exports (last line).
// Top-level names: extractClaims, applyChecks, INTERNAL_PREFIXES, IMAGE_PREFIX, and helpers prefixed AC_/ac, so the
// code around it in a Code node (output, steps, question, claims, lookup, ...) cannot clash with them.

const INTERNAL_PREFIXES = ['canary-', 'eval-', 'demo-', 'messy-eval-', 'visual-eval-', 'deploy-check-'];
const IMAGE_PREFIX = 'http://localhost:8000/storage/v1/object/public/document-images/';

const AC_LIBRARY_TOOLS = ['search_documents', 'get_full_document', 'query_spreadsheets', 'search_pages', 'look_at_pages'];
const AC_FOOTER = '\u26A0\uFE0F Could not verify: ';
const AC_UNAVAILABLE_LINE = 'Some sources could not be checked right now (visual search was unavailable).';
// what a tool returns when its service is down (the Page search and Look at pages workflows); document text saying "UNAVAILABLE" is not it
const AC_UNAVAILABLE_MARKER = /\bVISUAL SEARCH UNAVAILABLE\b/;
const AC_EXT = '(?:pdf|docx?|pptx?|xlsx?|csv|jpe?g|png|tiff?)(?![a-z0-9])';
const AC_DOC_EXT = '(?:pdf|docx?|pptx?|xlsx?|csv)(?![a-z0-9])';
const AC_PAGE_WORD = '(?:pages?|pgs?\\.?|pp?\\.?)';
const AC_PAGE_RANGE = '\\d+(?:\\s*(?:[-\u2013\u2014]|to)\\s*\\d+)?';
const AC_PAGE_SPEC = AC_PAGE_RANGE + '(?:\\s*(?:,\\s*(?:and|&)?|&|and)\\s*' + AC_PAGE_RANGE + ')*';
// one file inside a citation: NAME.ext [, p. 3-5 | (pp. 3-5)] [) or ]], then ";" "," "and", the end (a final "."
// allowed), or a "(" that starts something else. The name is lazy, so it ends at the first known extension: "Plan Set
// (Final), Rev 2.pdf" stays one name. Sticky: pieces are read one after the other.
const AC_PIECE = '\\s*([^;|\\n]+?\\.' + AC_EXT + ')(?:\\s*,?\\s*[(\\[]?\\s*' + AC_PAGE_WORD + '\\s*(' + AC_PAGE_SPEC + '))?'
  + '\\s*[)\\]]?\\s*(?:[;,]|\\band\\b|\\.?\\s*$|(?=[(\\[]))';
// review-mode table cells: "(doc) NAME.ext" [, p. N]
const AC_DOC_CELL = new RegExp('\\(doc\\)\\s*([^\\s|(;][^|;\\n]*?\\.' + AC_DOC_EXT + ')(?:\\s*,?\\s*\\(?\\s*' + AC_PAGE_WORD
  + '\\s*(' + AC_PAGE_SPEC + ')\\)?)?', 'gi');
const AC_IMAGE = /!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
const AC_WEB_HEADING = /^[ \t]*(?:#{1,6}[ \t]*|\*\*[ \t]*)From (?:verified public sources|the wider web)/im;
// "Step 12", "Step 10 of 12", "Figure 4-11", "Fig. 22", "Table 3.14", "Sheet #18", "Item No. 25", "Section 4.2.1":
// the number names a part of a document, it is not a value
const AC_LABELLED = new RegExp('\\b((?:steps?|items?|figures?|fig\\.|tables?|sheets?|sections?)\\s*(?:#\\s*|no\\.\\s*)?)'
  + '(\\d+(?:[-\u2013.]\\d+)*(?:\\s*(?:[-\u2013]|to)\\s*\\d+(?:[-\u2013.]\\d+)*)?(?:\\s+of\\s+\\d+)?)', 'gi');
// a number in the answer: not glued to a letter, digit, "_" or "." before it; "1,200" is one number
const AC_NUMBER = /(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.(\d+))?/g;
// a unit right after a number ("5 ft", "24in", "10-year", "4%"). A bare "in" counts only when no word follows it
// ("24 in", "5.4 in/hr"), so "step 2 in the portal" is not 2 inches.
const AC_UNIT = new RegExp('^[ \\t]?-?[ \\t]?(?:[%\u00B0\u2032\u2033]|(?:percent|inch(?:es)?|feet|foot|ft|mm|cm|km|m|mi|miles?|'
  + 'yds?|yards?|sf|sq|acres?|ac|ha|gal(?:lons?)?|cf|cy|cfs|gpm|gpd|mgd|lbs?|pounds?|kg|tons?|kips?|psi|ksi|psf|pcf|kpa|mpa|'
  + 'secs?|seconds?|mins?|minutes?|hrs?|hours?|days?|weeks?|wks?|months?|years?|yrs?|mph|fps|deg(?:rees?)?|kv|kwh?|mw|hz|'
  + 'amps?|volts?|watts?)(?![A-Za-z])|in(?=[ \\t]*(?:[^ \\tA-Za-z]|$)))', 'i');

// a file name for comparing: lower case, "_" read as a space (the agent writes "User Guide.pdf" for User_Guide.pdf),
// runs of spaces as one, no space before the extension ("Training .pdf" is a real library name)
function acKey(name) {
  return String(name == null ? '' : name).replace(/_/g, ' ').replace(/\s+/g, ' ').replace(/ \./g, '.').trim().toLowerCase();
}

// case-insensitive, with or without a "Folder/" prefix on either side (after acKey)
function acSameFile(cited, stored) {
  const a = acKey(cited), b = acKey(stored);
  return a !== '' && (a === b || b.endsWith('/' + a) || a.endsWith('/' + b));
}

// a cited name as read: spaces collapsed, no leading "(" or "["; null for a web address (not a library file)
function acFileName(raw) {
  const name = String(raw).replace(/^[\s(\[]+/, '').replace(/\s+/g, ' ').trim();
  return name && !/:\/\/|^www\./i.test(name) ? name : null;
}

function acBlank(text) { return text.replace(/[^\n]/g, ' '); }

function acEscape(text) { return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

function acPages(spec) {
  const pages = [];
  for (const part of String(spec).split(/\s*(?:,|&|\band\b)\s*/i)) {
    const r = /^(\d+)(?:\s*(?:[-\u2013\u2014]|to)\s*(\d+))?$/i.exec(part.trim());
    if (!r) continue;
    const a = Number(r[1]), b = r[2] ? Number(r[2]) : a;
    const lo = Math.min(a, b), hi = Math.max(a, b);
    if (hi - lo > 2000) { pages.push(lo, hi); continue; }
    for (let n = lo; n <= hi; n++) pages.push(n);
  }
  return pages.length ? pages : null;
}

// the files cited inside one parenthesis: "Source: A.pdf, p. 3-5; B.pdf" or "A.pdf, page 14". Without "Source:" a file
// counts only with a page, so prose such as "(see the 2019 manual.pdf)" is not taken for a citation.
function acCitePieces(inner) {
  const head = /^\s*sources?\s*:/i.exec(inner);
  const rest = head ? inner.slice(head[0].length) : inner;
  const re = new RegExp(AC_PIECE, 'iy');
  const pieces = [];
  let m;
  while (re.lastIndex < rest.length && (m = re.exec(rest))) {
    const name = acFileName(m[1]);
    if (name === null) continue;                                       // a URL: skip it, read on
    if (/,\s*(?:pages?|pgs?\.?|pp?\.)\s*\d/i.test(name)) break;      // the name swallowed a page reference: stop
    const pages = m[2] ? acPages(m[2]) : null;
    if (!head && !pages) break;
    pieces.push({ name, pages });
    if (!m[0].length) break;
  }
  return pieces;
}

// citations in the documents part, in order of appearance, deduplicated by lower-cased name (pages merged), plus the
// character spans they occupy (blanked before numbers are read). Three forms: a parenthesis, a review-table cell
// "(doc) NAME", and a "Source: ..." line or a "Sources:" line followed by one file per list item.
function acCites(clean) {
  const hits = [], spans = [];
  let stack = [];
  for (let i = 0; i < clean.length; i++) {
    const ch = clean[i];
    if (ch === '\n') stack = [];
    else if (ch === '(') stack.push(i);
    else if (ch === ')' && stack.length) {
      // the matching "(" of this ")": a parenthesis earlier on the line, or inside the name, cannot shift the start
      const open = stack.pop();
      const pieces = acCitePieces(clean.slice(open + 1, i));
      if (!pieces.length) continue;
      for (const p of pieces) hits.push({ name: p.name, pages: p.pages, at: open });
      spans.push([open, i + 1]);
    }
  }
  let offset = 0;
  for (const line of clean.split('\n')) {
    if (/^\s*\|/.test(line)) {
      for (const m of line.matchAll(AC_DOC_CELL)) {
        const name = acFileName(m[1]);
        if (name === null) continue;
        hits.push({ name, pages: m[2] ? acPages(m[2]) : null, at: offset + m.index });
        spans.push([offset + m.index, offset + m.index + m[0].length]);
      }
    }
    offset += line.length + 1;
  }
  offset = 0;
  let list = false;                                  // inside the items under a bare "Sources:" line
  for (const line of clean.split('\n')) {
    const head = /^\s*(?:[-+]\s+)?Sources?\s*:\s*/i.exec(line);
    const item = list && !head && /^\s*(?:[-+]\s+|\d+[.)]\s+)?(?=\S)/.exec(line);
    const rest = head ? line.slice(head[0].length) : item ? line.slice(item[0].length) : '';
    const pieces = rest.trim() ? acCitePieces('Source: ' + rest) : [];
    for (const p of pieces) hits.push({ name: p.name, pages: p.pages, at: offset });
    if (pieces.length) spans.push([offset, offset + line.length]);
    list = head ? !rest.trim() : list && pieces.length > 0;
    offset += line.length + 1;
  }
  hits.sort((a, b) => a.at - b.at);
  const byKey = new Map();
  for (const h of hits) {
    const key = acKey(h.name);
    const seen = byKey.get(key);
    if (!seen) byKey.set(key, { name: h.name, pages: h.pages ? h.pages.slice() : null });
    else if (h.pages) seen.pages = (seen.pages || []).concat(h.pages);
  }
  const cites = [];
  for (const c of byKey.values()) {
    cites.push({ name: c.name, pages: c.pages ? Array.from(new Set(c.pages)).sort((a, b) => a - b) : null });
  }
  return { cites, spans };
}

// every number in a text, read loosely (glued to letters, inside JSON), with and without thousands separators
function acNumberSet(text) {
  const set = new Set();
  const add = (t) => { for (const m of t.matchAll(/\d*\.\d+|\d+/g)) set.add(parseFloat(m[0])); };
  add(text);
  add(text.replace(/(\d),(?=\d{3}(?!\d))/g, '$1'));
  return set;
}

// the documents part with everything that is not a claim blanked out (same length, so spans stay valid): citations,
// images, link targets and URLs, file names, page references, list and table-row numbers, and chart-reading lines
function acClaimText(clean, spans, cites) {
  const chars = clean.split('');
  for (const [a, b] of spans) for (let i = a; i < b; i++) if (chars[i] !== '\n') chars[i] = ' ';
  let s = chars.join('');
  s = s.replace(/!\[[^\]]*\]\([^)\n]*\)/g, acBlank)       // images whole, alt text and title included
    .replace(/\]\([^)\s]*\)/g, acBlank).replace(/\b(?:https?:\/\/|www\.)[^\s)>\]]+/gi, acBlank);
  for (const c of cites) s = s.replace(new RegExp(acEscape(c.name).replace(/ /g, '\\s+'), 'gi'), acBlank);
  s = s.replace(new RegExp('(?<![^\\s(|])[^\\s(|]*\\.' + AC_EXT, 'gi'), acBlank)   // file-name tokens, from their start
    .replace(new RegExp('\\b(?:pages?|pgs?\\.|pp?\\.)\\s*' + AC_PAGE_SPEC, 'gi'), acBlank)
    .replace(AC_LABELLED, (m, label, n) => label + acBlank(n));
  return s.split('\n').map((line) => (/read from the chart/i.test(line) ? acBlank(line) : line
    .replace(/^(\s*(?:[-+]\s+)?)(\d+)(?=[.)](?:\s|$))/, (m, pre, d) => pre + acBlank(d))
    .replace(/^(\s*#{1,6}\s*)(\d+)/, (m, pre, d) => pre + acBlank(d))
    .replace(/^(\s*\|\s*)(\d+)(?=\s*\|)/, (m, pre, d) => pre + acBlank(d)))).join('\n');
}

// numbers that need a source (2+ significant digits, >= 10, or a unit) and appear in neither the question nor the
// tool results; normalised (no thousands separators, no leading zeros), in order, once each
function acNumbers(text, question, observations) {
  const fromQuestion = acNumberSet(question), fromSources = acNumberSet(observations);
  const out = [];
  for (const m of text.matchAll(AC_NUMBER)) {
    const int = m[1].replace(/,/g, '').replace(/^0+(?=\d)/, ''), frac = m[2] || '';
    const norm = frac ? int + '.' + frac : int;
    const value = parseFloat(norm);
    const sig = ((int === '0' ? '' : int) + frac).replace(/^0+/, '').length;
    const end = m.index + m[0].length;
    const unit = /[$\u20AC\u00A3]/.test(text.charAt(m.index - 1)) || AC_UNIT.test(text.slice(end, end + 24));
    if (!(value >= 10 || unit || sig >= 2)) continue;            // integers below 10 without a unit: list numbering etc.
    if (fromQuestion.has(value) || fromSources.has(value) || out.includes(norm)) continue;
    out.push(norm);
  }
  return out;
}

function acImageName(url) {
  if (!String(url).startsWith(IMAGE_PREFIX)) return null;
  let name = String(url).slice(IMAGE_PREFIX.length).split(/[?#]/)[0];
  try { name = decodeURIComponent(name); } catch (e) { /* keep it as written */ }
  return name || null;
}

// the documents part: the answer up to the first web heading (those sections are code-generated from tool output),
// without footers
function acDocPart(text) {
  const cut = text.search(AC_WEB_HEADING);
  return (cut >= 0 ? text.slice(0, cut) : text).split('\n')
    .filter((l) => !/^\s*(?:_Web sources retrieved\b|\u26A0\uFE0F?\s*Could not verify:)/.test(l) && l.trim() !== AC_UNAVAILABLE_LINE)
    .join('\n').replace(/\s+$/, '');
}

function acList(items) {
  const shown = items.slice(0, 5), more = items.length - shown.length;
  if (more > 0) return shown.join(', ') + ' and ' + more + ' more';
  return shown.length > 1 ? shown.slice(0, -1).join(', ') + ' and ' + shown[shown.length - 1] : shown[0];
}

function extractClaims(output, steps, question) {
  const text = String(output == null ? '' : output);
  const toolsUsed = [], parts = [];
  for (const s of (Array.isArray(steps) ? steps : [])) {
    const tool = s && s.action && s.action.tool;
    if (tool && !toolsUsed.includes(String(tool))) toolsUsed.push(String(tool));
    const o = s ? s.observation : undefined;
    if (o === undefined || o === null) continue;
    let t;
    if (typeof o === 'string') t = o;
    else { try { t = JSON.stringify(o); } catch (e) { t = String(o); } }
    if (typeof t === 'string') parts.push(t);
  }
  const observations = parts.join('\n\n');
  const docPart = acDocPart(text);
  const clean = docPart.replace(/[*`"\u201C\u201D]/g, ' ');     // **bold** / `code` / quoted names, same length
  const { cites, spans } = acCites(clean);
  const images = [];
  for (const m of text.matchAll(AC_IMAGE)) {
    const name = acImageName(m[1]);
    if (name !== null && !images.includes(name)) images.push(name);
  }
  const numbers = acNumbers(acClaimText(clean, spans, cites), String(question == null ? '' : question), observations);
  return { output: text, docPart, cites, images, numbers, toolsUsed, observations, unavailable: AC_UNAVAILABLE_MARKER.test(observations) };
}

// lookup: { files: [{file_name, last_page}], images: [object names], history?: the chat's earlier messages as one
// string }. Fails open: without a files array checks 1-2 are skipped, without an images array check 3 is skipped
// (a failed lookup must never strip screenshots or flag files). With history, a follow-up answer may reuse what the
// conversation already showed: its numbers are not flagged, nor its citations when every cited file appears there.
function applyChecks(claims, lookup) {
  const c = claims || {}, lk = lookup || {};
  const original = String(c.output == null ? '' : c.output);
  const haveFiles = Array.isArray(lk.files), haveImages = Array.isArray(lk.images);
  const files = haveFiles ? lk.files.filter((f) => f && f.file_name) : [];
  const stored = new Set(haveImages ? lk.images.map(String) : []);
  const history = typeof lk.history === 'string' ? lk.history : '';
  const cites = Array.isArray(c.cites) ? c.cites : [];
  const flags = [];
  // 1. cited files exist; 2. cited pages are in range (files with page views only)
  const matched = !haveFiles ? []
    : cites.map((cite) => ({ cite, files: files.filter((f) => acSameFile(cite.name, f.file_name)) }));
  for (const m of matched) {
    if (!m.files.length) flags.push({ code: 'missing_file', text: `the cited file "${m.cite.name}" is not in the library` });
  }
  for (const m of matched) {
    const lasts = m.files.map((f) => f.last_page).filter((n) => n !== null && n !== undefined && n !== '' && Number.isFinite(Number(n)))
      .map(Number);
    const pages = Array.isArray(m.cite.pages) ? m.cite.pages : [];
    if (!lasts.length || !pages.length) continue;
    const last = Math.max(...lasts), top = Math.max(...pages);
    if (top > last) flags.push({ code: 'page_out_of_range', text: `page ${top} of "${m.cite.name}" is past its last page (${last})` });
  }
  // 3. screenshots load: in the image store and present there; a broken one is removed (its line too, if it was alone)
  const broken = (url) => { const name = acImageName(url); return name === null || !stored.has(name); };
  let out = original, removed = 0;
  if (haveImages && Array.from(original.matchAll(AC_IMAGE)).some((m) => broken(m[1]))) {
    const kept = [];
    for (const line of original.split('\n')) {
      const next = line.replace(AC_IMAGE, (m, url) => { if (!broken(url)) return m; removed++; return ''; });
      if (next !== line && next.trim() === '') continue;
      kept.push(next);
    }
    out = kept.join('\n');
  }
  if (removed) flags.push({ code: 'broken_image', text: removed === 1 ? 'a screenshot could not be loaded' : `${removed} screenshots could not be loaded` });
  // 4. numbers are in the sources (extractClaims kept only the ones that are not)
  const fromHistory = history ? acNumberSet(history) : null;
  const numbers = (Array.isArray(c.numbers) ? c.numbers : [])
    .filter((n) => !(fromHistory && fromHistory.has(parseFloat(n))));
  if (numbers.length === 1) flags.push({ code: 'number_unsupported', text: `the value ${numbers[0]} was not found in the sources` });
  else if (numbers.length) flags.push({ code: 'number_unsupported', text: `the values ${acList(numbers)} were not found in the sources` });
  // 5. no citations without a search
  const tools = Array.isArray(c.toolsUsed) ? c.toolsUsed : [];
  const said = acKey(history);
  const inHistory = (cite) => {           // the name, or the name without its folder, as a whole name in the history
    const k = acKey(cite.name), base = k.slice(k.lastIndexOf('/') + 1);
    return base !== '' && [k, base].some((n) => new RegExp('(?:^|[^a-z0-9])' + acEscape(n) + '(?![a-z0-9])').test(said));
  };
  if (cites.length && !tools.some((t) => AC_LIBRARY_TOOLS.includes(t)) && !(said && cites.every(inHistory))) {
    flags.push({ code: 'cites_without_search', text: 'cites sources without having searched' });
  }
  // 6. a tool reported unavailable: a fixed line of its own, whatever the model said
  if (c.unavailable) flags.push({ code: 'tool_unavailable', text: AC_UNAVAILABLE_LINE });
  const listed = flags.filter((f) => f.code !== 'tool_unavailable');
  if (listed.length) out += '\n\n' + AC_FOOTER + listed.map((f) => f.text).join('; ') + '.';
  if (c.unavailable) out += '\n\n' + AC_UNAVAILABLE_LINE;
  return { output: out, flags };
}

if (typeof module !== 'undefined') module.exports = { extractClaims, applyChecks, INTERNAL_PREFIXES, IMAGE_PREFIX };
