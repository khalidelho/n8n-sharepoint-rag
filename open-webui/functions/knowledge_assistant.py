"""
title: Knowledge Assistant
description: Sends each message to the private RAG agent in n8n and returns its answer.
"""

import json
import logging
import re
import time
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# What the user sees when the pipe itself fails (design part 3). Each line carries a ref ("p-" + 4 hex) that
# the operator can match to the incident_log row (source "pipe") and the Telegram alert.
TEXTS = {
    "timeout": "The assistant took too long to answer (ref {ref}). It has been reported; please try again.",
    "offline": "The assistant is offline right now (ref {ref}). It has been reported.",
    "http": "The assistant hit a problem (ref {ref}). It has been reported; please try again in a minute.",
    "forbidden": "The knowledge assistant rejected the request — check the token in this function's settings. (ref {ref})",
}
INCIDENT_TIMEOUT_SECONDS = 3
INCIDENT_MESSAGE_MAX = 200


class EmptyAnswer(Exception):
    """n8n answered 2xx without a usable answer: empty or missing output, a body that is not JSON, or not an object.
    `reason` is one of REASONS, fixed strings written here; it is the only part that reaches an incident."""

    NOT_JSON = "a body that is not JSON"
    NOT_OBJECT = "JSON that is not an object"
    NO_TEXT = "no output text"
    REASONS = (NOT_JSON, NOT_OBJECT, NO_TEXT)

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason if reason in self.REASONS else "no usable answer"


def make_ref() -> str:
    """'p-' + 4 hex characters from the clock (milliseconds), e.g. p-7f3a."""
    return "p-%04x" % (time.time_ns() // 1_000_000 % 0x10000)


def failure_message(exc: BaseException | None, status: int | None, ref: str) -> tuple[str, str]:
    """(kind, user_text) for a failed call to n8n. exc: the exception (or None); status: n8n's HTTP status (or None)."""
    if status == 403:
        kind = "forbidden"
    elif isinstance(exc, EmptyAnswer):
        return "empty", TEXTS["http"].format(ref=ref)  # the user sees the http line, the incident says empty
    elif isinstance(exc, requests.ConnectTimeout):  # could not even connect: n8n (or its host) is down
        kind = "offline"
    elif isinstance(exc, requests.Timeout):
        kind = "timeout"
    elif isinstance(exc, requests.ConnectionError):
        kind = "offline"
    elif exc is None or isinstance(exc, requests.HTTPError):
        kind = "http"
    else:  # anything unexpected (bad valve URL, a bug): same line as http, its own incident kind
        return "error", TEXTS["http"].format(ref=ref)
    return kind, TEXTS[kind].format(ref=ref)


def incident_message(kind, exc, status, n8n_url, timeout_seconds):
    """Exception class + a short reason we write ourselves. Never str(exc): requests' errors can quote the token
    (InvalidHeader) or anything else, and the incident must never carry the question."""
    try:
        u = urlsplit(n8n_url)
        where = f"{u.hostname}:{u.port}" if u.port else (u.hostname or "n8n")
    except ValueError:
        where = "n8n"
    cls = type(exc).__name__ if exc is not None else "HTTPError"
    empty = exc.reason if isinstance(exc, EmptyAnswer) else "no usable answer"  # a fixed string, never str(exc)
    reason = {
        "timeout": f"no answer from {where} within {timeout_seconds} s",
        "offline": f"could not connect to {where}",
        "http": f"{where} answered HTTP {status}",
        "forbidden": f"{where} answered HTTP 403 (token rejected)",
        "empty": f"{where} answered HTTP {status}: {empty}",
    }.get(kind, f"unexpected error calling {where}")
    return f"{cls}: {reason}"[:INCIDENT_MESSAGE_MAX]


def report_incident(url: str, token: str, kind: str, message: str, ref: str) -> bool:
    """Best effort: POST {source: pipe, kind, message, ref} to n8n's incident webhook. Never raises; if n8n is down
    this fails quietly (the watchdog covers that case). Returns True when n8n took it."""
    if not url:
        return False
    try:
        r = requests.post(
            url,
            json={"source": "pipe", "kind": kind, "message": str(message)[:INCIDENT_MESSAGE_MAX], "ref": ref},
            headers={"Authorization": f"Bearer {token}"},
            timeout=INCIDENT_TIMEOUT_SECONDS,
        )
        return 200 <= r.status_code < 300
    except Exception:
        return False


def answer_text(r) -> str:
    """The answer from a 2xx n8n response, or EmptyAnswer. n8n's own error line (with its ref) is shown as is."""
    try:
        data = r.json()
    except ValueError:
        raise EmptyAnswer(EmptyAnswer.NOT_JSON) from None
    if not isinstance(data, dict):
        raise EmptyAnswer(EmptyAnswer.NOT_OBJECT)
    output = data.get("output")
    if not isinstance(output, str) or not output.strip():
        raise EmptyAnswer(EmptyAnswer.NO_TEXT)
    return output


class Pipe:
    class Valves(BaseModel):
        n8n_url: str = Field(
            default="http://host.docker.internal:5680/webhook/rag-chat",
            description="The agent's webhook (host.docker.internal = the Mac, as seen from this container)",
        )
        n8n_token: str = Field(
            default="", description="RAG_WEBHOOK_TOKEN from stack/n8n/.env"
        )
        timeout_seconds: int = Field(default=180)
        incident_url: str = Field(
            default="http://host.docker.internal:5680/webhook/rag-incident",
            description="Where failures are reported (n8n's incident webhook, same token); empty = don't report",
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "knowledge-assistant", "name": "Knowledge Assistant"}]

    def pipe(
        self,
        body: dict,
        __user__: dict = None,
        __metadata__: dict = None,
        __task__: str = None,
    ):
        messages = body.get("messages", [])

        # Open WebUI also asks the selected model to write chat titles, tags and follow-up suggestions.
        # Those must NOT reach the agent: each would run a full search and be stored in the chat's memory.
        if __task__:
            prompt = str(messages[-1].get("content", "")) if messages else ""
            if __task__ == "title_generation":
                first = re.search(r"USER:\s*(.+)", prompt)
                title = (
                    first.group(1) if first else "Knowledge Assistant chat"
                ).strip()
                return json.dumps({"title": title[:60]})
            if __task__ == "follow_up_generation":
                return json.dumps({"follow_ups": []})
            if __task__ == "tags_generation":
                return json.dumps({"tags": []})
            return ""

        # A real question: the newest message; earlier context lives in n8n's memory, keyed by chat
        content = messages[-1].get("content", "") if messages else ""
        if isinstance(content, list):  # messages with attachments arrive as parts
            content = " ".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )

        # "<person>:<chat>" — one memory per chat, and every chat traceable to its person for erasure requests
        user_id = (__user__ or {}).get("id", "unknown-user")
        session_id = f"{user_id}:{(__metadata__ or {}).get('chat_id') or 'no-chat-id'}"

        status = None
        try:
            r = requests.post(
                self.valves.n8n_url,
                json={"chatInput": content, "sessionId": session_id},
                headers={"Authorization": f"Bearer {self.valves.n8n_token}"},
                timeout=self.valves.timeout_seconds,
            )
            status = r.status_code
            if 200 <= status < 300:
                return answer_text(r)
            error = None
        except Exception as e:
            error = e
        return self._failed(error, status)

    def _failed(self, exc, status):
        """The user's line for this failure, after a best-effort incident report (no question text in either)."""
        ref = make_ref()
        kind, text = failure_message(exc, status, ref)
        message = incident_message(kind, exc, status, self.valves.n8n_url, self.valves.timeout_seconds)
        if kind == "http" and status is not None and 500 <= status < 600:
            # n8n answers 5xx when an execution failed, and its error workflow already logs and alerts that
            # under its own ref. Reporting it here too would send two alerts for one failure. The p- ref is only
            # in this log line.
            log.warning("knowledge_assistant: %s (ref %s, left to n8n's error workflow): %s", kind, ref, message)
            return text
        log.warning("knowledge_assistant: %s (ref %s): %s", kind, ref, message)
        if not report_incident(self.valves.incident_url, self.valves.n8n_token, kind, message, ref):
            # n8n down, no incident_url, or the token was rejected (a wrong token can never log an incident)
            log.info("knowledge_assistant: incident for ref %s not delivered", ref)
        return text
