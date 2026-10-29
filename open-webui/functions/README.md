# Open WebUI functions

| File | Type | What it does |
|---|---|---|
| `knowledge_assistant.py` | Pipe | The **Knowledge Assistant** model. Sends each question to the RAG agent in n8n (`/webhook/rag-chat`) and returns its answer. When the call fails, the user gets a plain line with a `p-` ref, and the pipe reports the failure to n8n's incident webhook (Telegram), except an n8n 5xx answer, which n8n's error workflow reports itself. |
| `download_word.py` | Action | Adds a **Download as Word** button under every answer. It turns the answer into a .docx and downloads it in the browser. The .docx includes headings, lists, full-width tables, screenshots and sources, plus a "Draft … review before use" note. |

## Knowledge Assistant pipe

**First install:** Admin Panel → Functions → **+**, paste `knowledge_assistant.py`, keep the id `knowledge_assistant`, save, enable it. Then set its
`n8n_token` valve once to your `RAG_WEBHOOK_TOKEN` (from `stack/n8n/.env`). "Knowledge Assistant" appears in the model
picker.

**Updates** go through the install script, so the code in Open WebUI and the file in git never drift apart
(Open WebUI restarts, about 20 s):

```sh
python3 open-webui/ops/install_function.py open-webui/functions/knowledge_assistant.py --dry-run   # sizes and hashes only
python3 open-webui/ops/install_function.py open-webui/functions/knowledge_assistant.py
```

The script replaces the function's code inside the running container and records it as a new version in Open WebUI's
function history, as Save does. It then restarts the container and waits for `/health`. The valves (plain JSON, with
the token in them) are kept as stored and never printed. It refuses a file that has no `class Pipe` or doesn't compile
on the container's Python 3.11. To roll back, install an earlier version from git history
(`git show <commit>:open-webui/functions/knowledge_assistant.py > /tmp/ka.py`, then install `/tmp/ka.py`).

**Don't reopen the valves with the gear icon** once the token is set: the form shows the token in clear text, so it
ends up in screen shares and screenshots. [docs/runbook.md](../../docs/runbook.md) ("Chat agent errors") has a check
that compares the valve with `.env` without printing either, and a key-only update.

**What the user sees when something fails.** Each line has a ref like `p-7f3a`. For a failure the pipe reports, the
same ref is on the `incident_log` row (source `pipe`) and the Telegram alert. Two exceptions:
- **n8n answers 5xx** (an execution failed). The pipe does not report it, because n8n's error workflow already logs and
  alerts it under its own ref, the execution id. The user's `p-` ref then appears only in Open WebUI's log.
- **A wrong token** (`forbidden`) can never produce an incident row: the incident webhook rejects the report because it
  uses the same token. The `p-` ref appears only in Open WebUI's log, as `incident for ref p-… not delivered`.

| Kind | When | The user sees |
|---|---|---|
| `timeout` | no answer within `timeout_seconds` (180) | The assistant took too long to answer (ref p-7f3a). It has been reported; please try again. |
| `offline` | can't connect to n8n (refused, connect timeout) | The assistant is offline right now (ref p-7f3a). It has been reported. |
| `http` | n8n answered with a non-2xx status (4xx is reported by the pipe; 5xx is left to n8n's error workflow) | The assistant hit a problem (ref p-7f3a). It has been reported; please try again in a minute. |
| `empty` | n8n answered 2xx but with no answer text, a non-JSON body, or JSON that isn't an object | the `http` line |
| `forbidden` | n8n answered 403 (wrong token) | The knowledge assistant rejected the request — check the token in this function's settings. (ref p-7f3a) |
| `error` | anything else (for example a broken `n8n_url`) | the `http` line |

Title, tag and follow-up requests (`__task__`) are answered inside the pipe and never reach the agent.

**The incident report** is best effort: one POST of `{source: "pipe", kind, message, ref}` to the `incident_url` valve,
with the same bearer token and a 3 s timeout. It never raises. If n8n is down, or the token is wrong, the report is lost
and the pipe logs `incident for ref p-… not delivered`. The `message` is the exception class plus a short reason the
pipe writes itself (at most 200 characters, for example `ConnectionError: could not connect to host.docker.internal:5680`).
It never includes the question, the chat, or the exception's own text, because request errors can quote the token.

**Valves**: `n8n_url`, `n8n_token`, `timeout_seconds`, and `incident_url` (default
`http://host.docker.internal:5680/webhook/rag-incident`; leave it empty to stop reports).

## Download as Word: install or update (about 2 minutes)

1. In Open WebUI, go to **Admin Panel → Functions → +** ("Create new function").
2. Paste the whole contents of `download_word.py`. The id fills in as `download_as_word`. Click **Save**.
3. In the Functions list, switch the function **on**. Then, in its **⋯** menu, turn on **Global** so the button appears
   on every model. Alternatively, assign it to the "Knowledge Assistant" model only, under
   *Workspace → Models → Knowledge Assistant → Actions*.
4. Open any answer. The Word icon appears in the row under the message; click it.

- **Images:** screenshots are fetched only from the library's image store (the `allowed_image_prefix` valve) and
  embedded in the file. Any other image link in a message is left out, so the server can't be made to fetch other
  addresses.
- **Conversion:** pandoc runs inside Open WebUI's own container. Nothing is stored, and the file goes straight to the
  browser.
- **Valves** (the gear icon on this function holds no secrets): the draft note, and the removal of chat-only lines
  ("I read X as Y", "If you want, I can…").

## Tests

No network or Docker needed (`requests` is stubbed when this python3 doesn't have it):

```sh
python3 -m unittest discover -s open-webui/functions -p 'test_knowledge_assistant.py' -v
python3 -m unittest discover -s open-webui/ops -p 'test_install_function.py' -v
```
