// Tests for n8n/js/answer_checks.js: Layer 1 of the error flow (design 2026-10-27, part 4). Run: node --test n8n/js/
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import checks from './answer_checks.js';

const { extractClaims, applyChecks, INTERNAL_PREFIXES, IMAGE_PREFIX } = checks;
const WARN = '⚠️';   // the ⚠️ of the design's footer (U+26A0 U+FE0F)
const UNAVAILABLE_LINE = 'Some sources could not be checked right now (visual search was unavailable).';
const searched = (observation = '') => [{ action: { tool: 'search_documents', toolInput: { query: 'q' } }, observation }];
const img = (name) => `![screenshot](${IMAGE_PREFIX}${name})`;

test('extractClaims parses (Source: NAME, p. 3-5) and (NAME, page 14) and (doc) NAME cells', () => {
  const output = [
    'Open the form first (Source: Safety Manual.pdf, p. 3-5).',
    'Inlets are spaced per the manual (Design Manual.pdf, page 14).',
    '',
    '| # | Recommendation | Why | Source | Priority |',
    '|---|---|---|---|---|',
    '| 1 | Add a sign-off line | Missing today | (doc) Field Checklist.docx | High |',
  ].join('\n');
  assert.deepEqual(extractClaims(output, searched(), 'q').cites, [
    { name: 'Safety Manual.pdf', pages: [3, 4, 5] },
    { name: 'Design Manual.pdf', pages: [14] },
    { name: 'Field Checklist.docx', pages: null },
  ]);
});

test('extractClaims handles names with commas and parentheses', () => {
  const c = extractClaims('See the grading sheet (Source: Plan Set (Final), Rev 2.pdf, p. 3-5).', searched(), '');
  assert.deepEqual(c.cites, [{ name: 'Plan Set (Final), Rev 2.pdf', pages: [3, 4, 5] }]);
});

test("extractClaims reads the agent's real citation style (bold names, pp., several files, an earlier parenthesis)", () => {
  const output = [
    "The inlet (Type C) is on the detail (Source: **Safety Manual.pdf**, pp. 2-3; **Field Guide (Office and Site).pdf**, pp. 6-7, 11).",
    'More steps (Source: **safety manual.pdf**, p. 9; **Training Deck.pptx**; **Recording.mp4**)',
    'Links stay links ([agency](https://example.gov/manual.pdf)) and (see the manual) is prose.',
  ].join('\n');
  assert.deepEqual(extractClaims(output, searched(), '').cites, [
    { name: 'Safety Manual.pdf', pages: [2, 3, 9] },
    { name: "Field Guide (Office and Site).pdf", pages: [6, 7, 11] },
    { name: 'Training Deck.pptx', pages: null },
  ]);
});

test('extractClaims reads "Source:" lines and "Sources:" lists', () => {
  const output = [
    'Steps here.',
    '**Source:** *Training Deck.pptx*; *Field Guide.pdf* (pp. 2-6 / slides 2-6)',
    '**Source:** Field Equipment Handbook.pdf (pages 10, 12, and 80).',
    '',
    '**Sources:**  ',
    '- **Safety Manual.pdf**, pp. 4-5, 13  ',
    '- **Site Photo 2.jpg**, p. 1',
    '',
    'Source: **the state agency**; dated 2024.',
  ].join('\n');
  const c = extractClaims(output, searched(), '');
  assert.deepEqual(c.cites, [
    { name: 'Training Deck.pptx', pages: null },
    { name: 'Field Guide.pdf', pages: null },
    { name: 'Field Equipment Handbook.pdf', pages: [10, 12, 80] },
    { name: 'Safety Manual.pdf', pages: [4, 5, 13] },
    { name: 'Site Photo 2.jpg', pages: [1] },
  ]);
  assert.deepEqual(c.numbers, ['2024']);
});

test('extractClaims keeps numbers that are not in the sources or the question', () => {
  const steps = searched(JSON.stringify([{ response: 'IDF table: intensity 5.4 in/hr for the design storm.' }]));
  const output = '1. At 15 minutes the 10-year intensity is 5.4 in/hr, so use a 24 in pipe (Source: IDF Curves.pdf, p. 3-5).\n'
    + '2. Repeat step 3 for each inlet.';
  assert.deepEqual(extractClaims(output, steps, '10-year storm at 15 minutes').numbers, ['24']);
});

test('numbers present in the question are exempt', () => {
  const c = extractClaims('Use 36 in pipe with 48 in of cover and 1,200 ft spacing.', searched('no figures here'),
    'Is 36 in pipe ok with 1200 ft spacing?');
  assert.deepEqual(c.numbers, ['48']);
});

test('integers below 10 are exempt unless they carry a unit; 2+ significant digits are checked', () => {
  const c = extractClaims('Repeat 3 times for 2 inlets. Keep 5 ft of clearance and a 0.5 slope; the factor is 2.5 at a 4% grade.',
    searched('nothing'), '');
  assert.deepEqual(c.numbers, ['5', '2.5', '4']);
});

test('chart lines are exempt', () => {
  const output = 'The 100-year curve gives about 7.2 in/hr at 20 minutes. Read from the chart — verify on the figure '
    + '(or a published table/equation) before using it for design.\nThe pipe is 30 in.';
  assert.deepEqual(extractClaims(output, searched(), '').numbers, ['30']);
});

test('extractClaims lists tools used and sets unavailable', () => {
  const steps = [
    { action: { tool: 'search_documents' }, observation: '[{"response":"text"}]' },
    { action: { tool: 'search_pages' }, observation: 'VISUAL SEARCH UNAVAILABLE: the local vision service did not respond.' },
    { action: { tool: 'search_documents' }, observation: [{ response: 'page 7 text' }] },
  ];
  const c = extractClaims('answer', steps, 'q');
  assert.deepEqual(c.toolsUsed, ['search_documents', 'search_pages']);
  assert.equal(c.unavailable, true);
  assert.ok(c.observations.includes('[{"response":"page 7 text"}]'), 'a non-string observation is JSON text');
  assert.ok(c.observations.includes('VISUAL SEARCH UNAVAILABLE'));
  assert.equal(extractClaims('answer', searched('The data is unavailable for that year.'), 'q').unavailable, false);
  assert.deepEqual(extractClaims('answer', undefined, undefined).toolsUsed, []);
  // only the tools' marker counts: upper-case document text is not a tool failure
  assert.equal(extractClaims('answer', searched('RATE: DATA UNAVAILABLE FOR 2019'), 'q').unavailable, false);
  assert.equal(extractClaims('answer', searched(JSON.stringify([{ response: 'SERVICE UNAVAILABLE (shown on the sign)' }])), 'q')
    .unavailable, false);
});

test('extractClaims leaves the web sections out of the documents part', () => {
  const output = 'Inlets every 300 ft (Source: Design Manual.pdf, p. 4).\n\n'
    + '### From verified public sources (verify before relying)\n- A federal rule sets 450 ft (Source: Agency Guide.pdf, p. 99).\n\n'
    + '### From the wider web — not on the verified list (use with caution)\n- 600 ft spacing.\n\n'
    + '_Web sources retrieved 2026-10-27._';
  const c = extractClaims(output, searched('spacing 300 ft'), 'q');
  assert.equal(c.docPart, 'Inlets every 300 ft (Source: Design Manual.pdf, p. 4).');
  assert.deepEqual(c.cites, [{ name: 'Design Manual.pdf', pages: [4] }]);
  assert.deepEqual(c.numbers, []);
});

test('extractClaims lists screenshot object names after IMAGE_PREFIX', () => {
  const output = `Step 1\n${img('01ABC/p1-img-0.jpeg')}\nStep 2 ![x](https://elsewhere.example/a.png)`;
  const c = extractClaims(output, searched(), '');
  assert.deepEqual(c.images, ['01ABC/p1-img-0.jpeg']);
  assert.deepEqual(c.numbers, []);
});

test('applyChecks flags a missing file, an out-of-range page, a broken image, an unsupported number', () => {
  const lines = [
    'Use the 24 in pipe (Source: Safety Manual.pdf, p. 40).',
    'The old rule is gone (Source: Old Manual.pdf, p. 2).',
    img('01ABC/p1-img-0.jpeg'),
    img('01ABC/p9-img-9.jpeg'),
  ];
  const claims = extractClaims(lines.join('\n'), searched('pipe sizes are listed'), 'which pipe?');
  const r = applyChecks(claims, { files: [{ file_name: 'Safety Manual.pdf', last_page: 12 }], images: ['01ABC/p1-img-0.jpeg'] });
  assert.deepEqual(r.flags, [
    { code: 'missing_file', text: 'the cited file "Old Manual.pdf" is not in the library' },
    { code: 'page_out_of_range', text: 'page 40 of "Safety Manual.pdf" is past its last page (12)' },
    { code: 'broken_image', text: 'a screenshot could not be loaded' },
    { code: 'number_unsupported', text: 'the value 24 was not found in the sources' },
  ]);
  assert.equal(r.output, lines.slice(0, 3).join('\n') + `\n\n${WARN} Could not verify: the cited file "Old Manual.pdf" is not in`
    + ' the library; page 40 of "Safety Manual.pdf" is past its last page (12); a screenshot could not be loaded; the value 24 was'
    + ' not found in the sources.');
});

test('applyChecks matches file names case-insensitively, with or without the folder', () => {
  const output = 'A (Source: safety manual.PDF, p. 3). B (Source: Standards/Design Manual.pdf, p. 2). C (Source: Field Guide.pdf, page 1).';
  const r = applyChecks(extractClaims(output, searched(), ''), { files: [
    { file_name: 'Standards/Safety Manual.pdf', last_page: 12 },
    { file_name: 'Design Manual.pdf', last_page: 30 },
    { file_name: 'Field Guide.pdf', last_page: null },
  ], images: [] });
  assert.deepEqual(r.flags, []);
  assert.equal(r.output, output);
});

test('applyChecks treats underscores as spaces and ignores a space before the extension', () => {
  // real case: the agent wrote the name of a file stored with underscores using spaces
  const output = 'A (Source: **Request Portal User Guide.pdf**, pp. 2-5). B (Source: Intro Training.pdf, p. 1).';
  const r = applyChecks(extractClaims(output, searched(), ''), { files: [
    { file_name: 'Request_Portal_User_Guide.pdf', last_page: 9 },
    { file_name: 'Intro Training .pdf', last_page: 1 },
  ], images: [] });
  assert.deepEqual(r.flags, []);
  assert.deepEqual(applyChecks(extractClaims(output, searched(), ''), { files: [{ file_name: 'Request Portal.pdf',
    last_page: 9 }], images: [] }).flags.map(f => f.code), ['missing_file', 'missing_file']);
});

test('applyChecks flags citations with no library tool run', () => {
  const output = 'Inlets every 300 ft (Source: Design Manual.pdf, p. 4).';
  const lookup = { files: [{ file_name: 'Design Manual.pdf', last_page: 10 }], images: [] };
  const web = [{ action: { tool: 'search_web' }, observation: 'Inlets every 300 ft' }];
  const r = applyChecks(extractClaims(output, web, ''), lookup);
  assert.deepEqual(r.flags, [{ code: 'cites_without_search', text: 'cites sources without having searched' }]);
  assert.equal(r.output, `${output}\n\n${WARN} Could not verify: cites sources without having searched.`);
  assert.deepEqual(applyChecks(extractClaims(output, [], ''), lookup).flags.map(f => f.code),
    ['number_unsupported', 'cites_without_search']);
});

test('applyChecks appends the fixed unavailable line and removes broken images from the output', () => {
  const good = img('01ABC/p1-img-0.jpeg'), bad = img('01ABC/p2-img-1.jpeg');
  const output = `1. Open the sheet.  \n   ${bad}\n2. Read the title block ${good} here.\n3. Done ![x](https://elsewhere.example/a.png) now.`;
  const down = [{ action: { tool: 'search_pages' }, observation: 'VISUAL SEARCH UNAVAILABLE: the local vision service did not respond.' }];
  const r = applyChecks(extractClaims(output, down, ''), { files: [], images: ['01ABC/p1-img-0.jpeg'] });
  assert.equal(r.output, `1. Open the sheet.  \n2. Read the title block ${good} here.\n3. Done  now.`
    + `\n\n${WARN} Could not verify: 2 screenshots could not be loaded.\n\n${UNAVAILABLE_LINE}`);
  assert.deepEqual(r.flags, [
    { code: 'broken_image', text: '2 screenshots could not be loaded' },
    { code: 'tool_unavailable', text: UNAVAILABLE_LINE },
  ]);
  const alone = applyChecks(extractClaims('Nothing found in the drawings.', down, ''), { files: [], images: [] });
  assert.equal(alone.output, `Nothing found in the drawings.\n\n${UNAVAILABLE_LINE}`);
  assert.deepEqual(alone.flags, [{ code: 'tool_unavailable', text: UNAVAILABLE_LINE }]);
});

test('Source: lines and Sources: items wrapped in parentheses or brackets are one clean citation', () => {
  const lookup = { files: [{ file_name: 'Safety Manual.pdf', last_page: 12 }, { file_name: 'Field Guide.pdf', last_page: 9 }], images: [] };
  const shapes = [
    ['**Source:** (Safety Manual.pdf, p. 3)', [{ name: 'Safety Manual.pdf', pages: [3] }]],
    ['**Sources:**\n- (Safety Manual.pdf, p. 3)\n- (Field Guide.pdf, pp. 4-5)', [{ name: 'Safety Manual.pdf', pages: [3] }, { name: 'Field Guide.pdf', pages: [4, 5] }]],
    ['Source: [Safety Manual.pdf, p. 3]', [{ name: 'Safety Manual.pdf', pages: [3] }]],
    ['**Source:** (Safety Manual.pdf)', [{ name: 'Safety Manual.pdf', pages: null }]],
  ];
  for (const [output, cites] of shapes) {
    const c = extractClaims(output, searched(), '');
    assert.deepEqual(c.cites, cites, output);
    assert.deepEqual(applyChecks(c, lookup).flags, [], output);
  }
});

test('web addresses are not library citations', () => {
  const output = 'Spacing rules (Source: https://agency.gov/docs/guide.pdf, p. 4). Also (Source: www.agency.gov/manual.pdf).';
  const c = extractClaims(output, searched(), '');
  assert.deepEqual(c.cites, []);
  assert.deepEqual(c.numbers, []);
  assert.deepEqual(applyChecks(c, { files: [], images: [] }).flags, []);
});

test('step, item, figure, table, sheet and section numbers are exempt', () => {
  const output = 'Step 12: close the form.\n### Step 10 of 12\nSee Figure 4-11, Fig. 22, Table 3.14, Sheet #18, Item No. 25 and Section 4.2.1.';
  assert.deepEqual(extractClaims(output, searched(), '').numbers, []);
  assert.deepEqual(extractClaims('Step 12 needs 48 in of cover.', searched(), '').numbers, ['48']);
});

test('applyChecks fails open when the lookup is missing or malformed', () => {
  const output = `Use the 24 in pipe (Source: Ghost.pdf, p. 99).\n${img('01ABC/p9-img-9.jpeg')}`;
  const claims = extractClaims(output, searched('pipe sizes are listed'), 'which pipe?');
  const numberOnly = `${output}\n\n${WARN} Could not verify: the value 24 was not found in the sources.`;
  for (const lookup of [undefined, null, { error: 'x' }, { files: 'nope', images: null }]) {
    const r = applyChecks(claims, lookup);
    assert.deepEqual(r.flags, [{ code: 'number_unsupported', text: 'the value 24 was not found in the sources' }], String(lookup));
    assert.equal(r.output, numberOnly);
  }
  // files present, images missing: checks 1-2 run, check 3 is skipped
  const r = applyChecks(claims, { files: [{ file_name: 'Other.pdf', last_page: 3 }] });
  assert.deepEqual(r.flags.map(f => f.code), ['missing_file', 'number_unsupported']);
  assert.ok(r.output.includes(img('01ABC/p9-img-9.jpeg')));
});

test('a follow-up answer is checked against the earlier messages of the chat', () => {
  const output = 'Yes, the 24 in pipe (Source: Safety Manual.pdf, p. 3).';
  const claims = extractClaims(output, [], 'and the pipe size again?');
  const files = [{ file_name: 'Standards/Safety Manual.pdf', last_page: 12 }];
  const history = 'user: which pipe?\nassistant: A 24 in pipe is required (Source: **Safety_Manual.pdf**, p. 3).';
  const clean = applyChecks(claims, { files, images: [], history });
  assert.deepEqual(clean.flags, []);
  assert.equal(clean.output, output);
  for (const h of [undefined, '', 'user: hello\nassistant: Hi, how can I help?']) {
    assert.deepEqual(applyChecks(claims, { files, images: [], history: h }).flags.map(f => f.code),
      ['number_unsupported', 'cites_without_search'], String(h));
  }
  // every cited file must be in the history, not just one
  const two = extractClaims('Per (Source: Safety Manual.pdf, p. 3) and (Source: Field Guide.pdf, p. 2).', [], '');
  assert.deepEqual(applyChecks(two, { files: files.concat([{ file_name: 'Field Guide.pdf', last_page: 9 }]), images: [], history })
    .flags.map(f => f.code), ['cites_without_search']);
});

test('applyChecks returns the output unchanged and no flags for a clean answer', () => {
  const output = `To file a complaint:\n1. Open the form.\n   ${img('01ABC/p1-img-0.jpeg')}\n`
    + '2. Enter the 6-digit request number and a 24 in pipe size (Source: **User Guide.pdf**, pp. 2-5).\n\n'
    + '### From verified public sources (verify before relying)\n- A rule from 2019 says 99 ft.\n\n_Web sources retrieved 2026-10-27._';
  const steps = searched(JSON.stringify([{ response: 'Pipe size: 24 in.' }]));
  const r = applyChecks(extractClaims(output, steps, 'How do I file a complaint?'),
    { files: [{ file_name: 'Guides/User Guide.pdf', last_page: 9 }], images: ['01ABC/p1-img-0.jpeg'] });
  assert.deepEqual(r.flags, []);
  assert.equal(r.output, output);
});

test('the file runs unchanged inside an n8n Code node', () => {
  const src = readFileSync(new URL('./answer_checks.js', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /^\s*(?:import|export)\b/m);
  assert.doesNotMatch(src, /\brequire\s*\(/);
  assert.doesNotMatch(src, /\.replaceAll\(|\.at\(|\?\?=|\|\|=|&&=|Object\.hasOwn|structuredClone/, 'post-ES2020 syntax');
  assert.match(src.trimEnd().split('\n').pop(),
    /^if \(typeof module !== 'undefined'\) module\.exports = \{ extractClaims, applyChecks, INTERNAL_PREFIXES, IMAGE_PREFIX \};?$/);
  // the Code node prelude declares these names before the pasted file text; none may clash with it
  const prelude = 'const output = "", steps = [], question = "", sessionId = "", seconds = null, claims = {}, lookup = {}, $json = {};\n';
  const m = new Function(prelude + src + '\nreturn { extractClaims, applyChecks, INTERNAL_PREFIXES, IMAGE_PREFIX };')();
  assert.equal(typeof m.extractClaims, 'function');
  assert.equal(typeof m.applyChecks, 'function');
  assert.deepEqual(m.INTERNAL_PREFIXES, ['canary-', 'eval-', 'demo-', 'messy-eval-', 'visual-eval-', 'deploy-check-']);
  assert.equal(m.IMAGE_PREFIX, 'http://localhost:8000/storage/v1/object/public/document-images/');
  assert.deepEqual(INTERNAL_PREFIXES, m.INTERNAL_PREFIXES);
});
