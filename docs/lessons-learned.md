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
