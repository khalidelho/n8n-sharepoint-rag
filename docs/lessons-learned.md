# Lessons learned

Real problems hit while building and running this, with the fix for each. Most of them are invisible in
tutorials and only show up with real documents.

## Documents and OCR

- **Azure's Mistral OCR has two hard limits: 30 MB and 30 pages per request.** Base64 inflates files by
  about a third, so anything over ~22 MB on disk fails. The `pages` parameter does *not* get around the
  page limit — the whole document's page count is checked first. Fix: split PDFs into parts with
  Gotenberg before OCR; convert Office files to PDF at 150 dpi (a 51 MB deck became 3 MB).
- **Converting PowerPoint to PDF first gives much better OCR.** An 86-slide deck read directly as PPTX gave
  4.9k characters and 217 images (mostly repeated logos); converted to PDF first, 72k characters and 58 images.
- **Image descriptions make OCR slow.** With annotations on, 30-page requests ran past a 3-minute timeout.
  Fix: 10-page parts, 5-minute timeout, and an OCR timeout marks just that file for retry instead of
  failing the run.
- **Permanent failures must be marked, not retried.** A password-protected deck or an oversized file will
  fail forever; only 429/5xx should retry. Otherwise every run pays for the same failure again.
- **A filename with an apostrophe broke the converter.** Upload every file to Gotenberg as
  `document.<ext>` and keep the real name only for citations.
- **Filter images before uploading them.** Skipping tiny images and exact repeats kept about half of them.

## n8n

- **Postgres query parameters split on commas** in the string form `{{a}},{{b}}`. Use the array form
  `{{ [a, b] }}`, or a file name with a comma breaks the query.
- **The Postgres node evaluates `{{ }}` inside the SQL itself** — the query field has no Expression toggle.
- **"Access token is empty"** meant the HTTP node's Authentication wasn't set to the credential, even
  though the credential itself was connected.
- **Tools drift from tutorials.** The SharePoint node was rebuilt on Microsoft Graph and only accepts the
  generic Microsoft OAuth2 credential; it returns *list item* IDs, so downloads still need Graph's file IDs.
- **"Execute step" runs only that node and its ancestors.** Side branches and downstream steps (like
  "mark as processed") never run. Test with *Execute workflow* when downstream effects matter.
- **A node's run mode matters.** Per-item code saved in "Run once for all items" mode processed only the
  first file of each batch — silently.
- **LLM nodes may hand you parsed JSON, not text.** Accept both.
- **A bulk-delete response became one item per deleted object** and multiplied the file ×13 downstream.
  Take the full response as a single item. An all-or-nothing join check caught it.
- **Executions can be huge.** Two nodes holding base64 copies of every document made each run
  110–240 MB and n8n's database grew to 3.1 GB. Fixes: read file bytes inside one Code node
  (`this.helpers.getBinaryDataBuffer`), save failed runs only, prune after 72 h / 500 runs, and delete
  orphaned `binary_data` rows when cleaning executions by SQL. Result: 19 MB.
- **Workflows called as sub-workflows, and production webhooks, must be published.**
- **CLI `import:workflow` unpublishes the workflow.** Publish again and restart n8n after every import, then
  check each workflow is active and the webhook answers.
- **A stale editor tab can overwrite a newer workflow.** Saving from a tab opened hours earlier wrote the
  old version back over a fixed one. One editor at a time; reload before saving.
- **A stray `// comment` can delete data.** A comment inserted mid-line silently disabled `.map(i => i.json)`
  in the compare step, so nothing matched and processed files were treated as deleted. Check table counts
  before and after every change; a spike in "deleted" is an alarm.

## Running a large backlog unattended

- **One unusual file can stall everything.** Outlook's "Save as PDF" writes a non-standard catalog entry
  (`/PageLayout /PDLayoutDontCare`) that Gotenberg's PDF engine rejects outright. The split node threw, a failed
  run marks nothing, so every run retried the same batch. Fix: give the Gotenberg nodes an error output and send
  such files to OCR whole — Mistral reads PDF/DOCX/PPTX directly up to 30 pages.
- **The spreadsheet extractor reads only the first tab.** Workbooks with 9–14 tabs lost most of their rows (one
  stored nothing because its first tab was blank). Fix without extra libraries: an .xlsx is a zip — unzip a copy
  with the Compression node, read the tab names from `xl/workbook.xml`, emit one item per tab, and pass the tab
  name to the extractor. Store the tab name with each row so the agent can filter by it.
- **A run can freeze with no error.** The HTTP node fires batched requests on a timer without waiting for
  responses; with dozens of large OCR requests in flight, occasionally one never settled. Diagnose with
  `N8N_LOG_LEVEL=debug` (per-node start/finish lines) and the worker's open sockets. Fix: smaller batches run
  more often (the overlap guard makes short intervals safe) plus a workflow time limit, so a frozen run cancels
  itself and its files are retried.
- **A time limit can create its own loop.** Files that genuinely need longer than the limit are cancelled and
  retried forever, blocking everything behind them. Defer the giants, clear the backlog, then run each alone.
- **Photos without text fail OCR.** The OCR model's document annotation summarizes the text it read; it doesn't
  look at the picture. Send text-free images to a vision model for a short factual description instead.
- **Content filters can block part of a document permanently.** Keep the readable parts and put a visible note
  where the blocked pages were, rather than failing the whole file forever.
- **Measure completeness against the source.** List the library through Graph and check that every eligible file
  has a status — "the pipeline stopped complaining" is not the same as "everything is in".

## Web search next to private documents

- **Keep the levels of trust visibly apart.** Answers that used the web carry separate sections — "From your
  documents", "From verified public sources" and "From the wider web — not on the verified list" — never mixed in one
  sentence. When they disagree the answer says so; documents first, then verified sources, take precedence.
- **Restrict the web to an allow-list.** On Azure, the `web_search_preview` tool rejects domain filters; the
  `web_search` tool accepts `filters.allowed_domains` and only cites those sites.
- **Use the web only when needed.** The agent searches the web for *current* regulations, changes or best
  practices, or when the documents don't answer — not for questions the documents already settle.
- **A review mode is mostly instructions.** Read the template in full, search related internal standards,
  check current regulations, then return a fixed structure (covered / add / outdated / suggested wording) where
  every recommendation names its source. No new infrastructure needed.

- **Route the question before the agent.** A rule like "use the web for current information" was ignored even when
  phrased as MUST; raising the model's reasoning effort didn't help and doubled simple answers' latency. One small
  classification call before the agent, turned into a per-question instruction, made tool use reliable.
- **Don't let the model file sources.** Given pre-sorted findings, the model still moved bullets between "verified"
  and "wider web" and dropped links. Have the agent return its tool calls (intermediate steps) and append the web
  sections in code, verbatim — the labels are then guaranteed, not requested.
- **n8n's Code-node sandbox has no `URL` class.** `new URL()` threw inside a try/catch, so every host looked
  unverified and the bug was silent. Parse hosts with a pattern, and test the classification on real output.

## Drawings, scans and charts

- **Build the test set before the feature.** A retrieval test written from extracted *text* can't show what a text
  pipeline misses. Fifteen questions on drawings, scans and charts, each answer checked on the actual page, put the
  text-only agent at 13%; visual page search took it to 83% with strict grading.
- **Measure charts, don't eyeball them.** My own eyeball reading of a rainfall-intensity chart was 10% off. Detecting
  the grid lines and tracing the curve matched the source's own worked example within 0.05. Grade a chart answer by
  whether its stated range *contains* the true value, not by its midpoint — the lenient grade first reported 90%.
- **OCR quietly corrupts small text on large sheets.** A table on a 34×22 in drawing came back as `304.0 | 253.0` for
  `3.0-4.0 | 2.0-3.0`, and the agent stated it as fact. CAD-exported PDFs carry an exact text layer: use the PDF's own
  text when it has one, OCR only image pages.
- **One question can exhaust the rate limit.** When the text doesn't hold the answer, the agent keeps searching, and
  every step re-sends everything before it: prompt tokens grew from 2.6k to 72k within one question (275k tokens in
  45 s against a 200k-per-minute deployment). Pace test runs, cap what a search returns, and give the agent a tool
  that answers in one look.
- **Check region availability with the CLI, not a report.** A research report inferred a US-only option for Azure's
  multimodal embedders from billing meters; `az … list-models` showed every one is Global Standard only. So page images
  are embedded locally on the Mac's GPU, and only the reading goes to Azure.
- **Tile large sheets.** Every image embedder squeezes a 34×22 in sheet to about 50 dpi. Index each sheet whole, as 12
  overlapping tiles and as a title-block crop.
- **Tesseract's orientation check gives up on charts** (confidence 0.1 on a sideways chart). Read the page in all four
  orientations, keep the one with the most confident real words, and rotate only on a clear win: some chart books print
  sideways charts on upright pages. Run the check on real scans only — on converted slides it doubled indexing time.
- **The GPU allocator can kill a long run silently.** The first embedding run died at view ~1,600: PyTorch's MPS
  allocator caches a buffer per image size. Clear the cache per batch, and checkpoint so a crash loses nothing.
- **Duplicates outrank the original.** Seven copies of one training deck each reproduced the same figure and outranked
  the real one. If the question names a document, also search inside it; show duplicate pages once; rank a figure page
  (one figure number, little text) above pages that cite many figures.
- **Don't pick tiles by similarity.** "The end of the profile" isn't something an image embedding knows. Send the vision
  model the whole sheet plus all 12 tiles, and let only its short answer go back to the agent.
- **A bigger model of the same kind didn't read the chart better.** A fair A/B — the page-reading step replayed outside
  n8n with the same pages and prompts, 15 tries per model — gave the full model and the mini model 0–1 of 15 each. The
  answers showed why: the full model never once saw the lowest curve's tiny "1" label and counted seven curves instead
  of eight, consistently and confidently. A perception limit, not a reasoning one. Read the answers, not just the
  scores. A newer model generation, gpt-6.1-sol, then read both chart questions right 10/10 in the same A/B.
- **Reasoning models need room to answer.** At the production output budget the full model spent everything on
  thinking and returned nothing, and the mini model did the same in 5 of 30 tries — where the workflow quietly fell
  back to its first-look draft. Doubling the budget and labelling a cut-off read "approximate" (with an incident)
  turned a silent wrong answer into a visible one.

## Citations and pages

- **Keep the page on every chunk.** None of 1,388 passages had a page number, because OCR output was chunked without
  page boundaries. The page index already holds each page's own text, so passages are matched to pages without
  re-running OCR: 98.5% land on the right pages.
- **Score in the right direction.** "How much of the passage does this page cover" let one text-heavy agenda slide win
  every passage of its deck (33% right). "How much of the page is in the passage" fixed it: passages are contiguous
  runs of pages. Grow the range outward from the best page instead of taking every matching page (26-page ranges).
- **Examples in a prompt beat the instructions next to them.** The new rule said "cite pages", but the prompt's
  citation example had none, and the model copied the example. Fixing the example fixed the answers.

## Reading what people type

- **Split the job by what each part can know.** A request said "Fall stabilisation … creeks that feed the river" and meant *outfall*; the agent wrote about the season. The model can't know the library's vocabulary and the
  database can't read the sentence, so a lookup lists library terms that look like words in the message, the router
  judges them in context, and the user is told in one line whenever the system reinterprets a word.
- **"Much more common in the library" isn't enough.** "fall" appeared in 30 files and "outfall" in 53. List clipped-word
  candidates generously and let the sentence decide.
- **Trigram similarity is blind to swapped letters in short words** (an acronym with two letters swapped scored 0.2
  against the real one). Use edit distance plus a swapped-letters check — which also needs a small edit distance,
  because the typo happened to be an anagram of an ordinary word.
- **Test the words that must not change.** Negative controls ("fall protection", "fall seeding", "write a scope of
  work…") stayed untouched; an initials guard drops acronym "corrections" that are really expansions; identifiers are
  never touched.
- **Keep the router's input byte-identical.** Adding a "Message:" prefix shifted the routing of one demo question; a
  36-question old-vs-new routing comparison caught it.
- **n8n error items carry no link back.** An error-output item has no `pairedItem`, so the fallback crashed when the
  lookup failed. Read upstream nodes by position (`.all()[$itemIndex]`), not `.item` — and force the error path once.

## Polishing answers

- **A local reranker beat GPT and freed the quota.** Reranking 25 passages with GPT sent ~19k tokens per search, the
  biggest single use of the Azure quota. bge-reranker-v2-m3 on llama.cpp put the right file in the top four 84% of the
  time against 78% (first place 66% vs 72%), 0.7 s faster, with no Azure tokens. Its scores separate off-topic
  questions cleanly, so passages below a threshold are dropped, and GPT decides when nothing passes — which keeps the
  honest "not in the library" answers.
- **A self-graded fix can only agree with itself.** Moving screenshots under their steps scored well on an automated
  check that used the same matching as the fixer. A hand sample found 3 of 14 moves wrong: a neighbouring image's
  machine-written caption had polluted the context. Match only the document's own text since the previous image.
- **Source documents can hold secrets.** A user guide in the library contained a real login in plain text. Answers now
  hide credential-shaped values (30 test cases, including "the password is case-sensitive"), but the real fix is to
  change that password and replace the document: the text still sits in the index and in chat history.
- **An export button is a server-side fetcher.** "Download as Word" fetched every image in the message, so an edited
  message could make the server fetch any address. Fetch only the library's own image store, and remove every other
  image from pandoc's document tree (pandoc's own sandbox mode was broken in the shipped version).

## A system that checks itself

- **Every failure becomes a message; every user gets a ref.** Before this, a failed answer showed a raw "couldn't be
  reached (HTTPError)" and told nobody. Now each failure is one incident row and one throttled Telegram alert, and the
  user sees a plain line with the same reference number.
- **Fixed thresholds on small eval sets roll back changes for noise.** A go-live was rolled back on a 12-question set
  whose two misses passed on a rerun, with byte-identical prompt, model and tools. The fair test is a *paired, same-day
  baseline*: run old and new on the same questions the same day, and let the new one fail only items the old one also
  failed, unless it passes one rerun. It cuts both ways: on the second attempt the old version passed a question twice
  and the new one failed it twice — not noise to explain away, but with n = 2 not proof either.
- **Put the floor below the baseline.** A weekly floor equal to the measured baseline pages on a normal week. Use
  baseline − 1, and ask each miss once more before checking it.
- **`n8n export:workflow` saves the editor draft, not what runs.** Backups taken that way held exactly the code being
  rolled back, and a helper script once exported an unpublished day-old draft. Read the published version from
  `workflow_history` at the active version id (this repo's export does), and accept a backup as a rollback source only
  when its version id equals the active one.
- **Rollbacks by version id expire.** This n8n edition prunes non-current workflow versions after about a day, so keep
  rollback files.
- **A generated file can switch off the alerts.** A builder script skipped the "error workflow" setting; the live
  workflow had it, so nothing looked wrong, but the next deploy from that file would have silenced its alerts. Test
  that every committed workflow carries the setting, and check it with SQL after every deploy.
- **Shared event logs cause false "crashed" alerts.** In queue mode the workers share a data volume and wrote the same
  event-log file; after a restart, one worker's crash recovery marked the other's finished runs as crashed. Give each
  container its own event-log name.
- **Alerts race.** Two workers handling a burst could each decide "nothing sent in 30 minutes" and send. A per-key
  advisory lock plus claiming the alert in the same insert: 10 parallel failures → exactly one message.
- **Graders need the evidence in their window.** An answer was graded "unsupported" because the files it cited sat
  past the grader's 30,000-character cut. Put passages that mention a cited file first. And respond first, then grade:
  a grader inside the answer's run eats the user's timeout.
- **Drill the restore, not just the backup.** A restore into throwaway containers took 10 s and matched every count,
  but on a bare database printed 56 "role does not exist" errors: Postgres roles aren't in a dump. The runbook now
  creates them first. Keys and `.env` files stay out of the backup on purpose; they belong in a password manager.

## Evaluating alternatives

- **"Completed" is not the same as "readable".** One pipeline marked image-only PDFs as completed with zero text
  extracted — no error. Count the text extracted per file, not just the job status.
- **Hold the search engine constant.** Comparing pipelines on the same engine and embedding model (same
  questions, known source file per question) isolates what actually differs: how documents are read and split.

## Sync design

- **Write metadata last.** If a run dies midway, the file is simply redone; it's never "marked done but
  half-indexed".
- **Make every run idempotent.** Clear a file's old chunks, rows and images right before processing it, so
  an interrupted run never leaves duplicates.
- **Use the content hash, not just timestamps.** Graph's `quickXorHash` catches edits whose timestamp didn't
  change and avoids re-processing files that were only renamed.
- **Count empty files as processed.** A spreadsheet with a blank first sheet would otherwise be "new"
  forever and block a batch slot.
- **Guard against overlapping runs.** The first node asks n8n's own database whether another ingestion run
  is still going (ignoring anything older than 2 hours, which is a crash).

## Retrieval and the agent

- **Vector similarity is not usefulness.** Cover pages and title slides score high because they name the
  topic; duplicate copies of a deck take several slots. A GPT reranker over 25 candidates fixed all of it in
  side-by-side tests, sometimes promoting the passage vector search had ranked last.
- **Ask the model for cut points, not rewritten text.** Agentic chunking returns only boundaries over
  numbered paragraphs; code cuts the original text. Far fewer output tokens, byte-exact chunks. Repair an
  off-by-one plan from its start points instead of rejecting it.
- **Make prompt rules MUSTs.** "When an image helps, include it" produced zero screenshots; "MUST copy the
  image markdown exactly, never invent URLs" placed them under the right steps.
- **Front ends call your model for side jobs.** Open WebUI asks the selected model for titles, tags and
  follow-ups; through a pipe, each became a full agent run and polluted the chat memory. Handle `__task__`
  in the pipe itself.

## Infrastructure

- **Always give a Compose project an explicit `name:`.** Without one, a new `n8n` folder took the same
  project name as an existing n8n stack and mounted its data volume (encryption-key mismatch crash loop).
  Don't run `--remove-orphans` blind — it would have deleted the other stack.
- **Supabase Storage on macOS needs a named volume.** Docker Desktop bind mounts don't support extended
  attributes, so every upload failed with HTTP 500 — while a "continue on error" node showed the run green.
- **Pre-create n8n's chat memory table with RLS.** If n8n creates it, Supabase exposes it through the
  public API and anyone with the anon key can read every conversation.
- **Give the agent's SQL tool a read-only login** with a statement timeout. It writes its own SQL.
