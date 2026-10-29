"""Tests for the Knowledge Assistant pipe (knowledge_assistant.py). No network: requests.post is always patched.

Run on the Mac:   python3 -m unittest discover -s open-webui/functions -p 'test_knowledge_assistant.py' -v
`requests` (and `pydantic`) are stubbed below when this python3 doesn't have them; the stub copies the real
exception hierarchy, so the kind mapping is tested the same way. Inside the Open WebUI container the real
libraries are used (see the README for the docker exec line).
"""
import inspect
import json
import logging
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def _stub_requests():
    # Mirrors requests.exceptions: ConnectTimeout is both a ConnectionError and a Timeout.
    m = types.ModuleType("requests")
    m.__stub__ = True

    class RequestException(IOError):
        pass

    class ConnectionError(RequestException):  # noqa: A001 - same name as in requests
        pass

    class Timeout(RequestException):
        pass

    class ConnectTimeout(ConnectionError, Timeout):
        pass

    class ReadTimeout(Timeout):
        pass

    class HTTPError(RequestException):
        pass

    class InvalidHeader(RequestException, ValueError):
        pass

    for cls in (RequestException, ConnectionError, Timeout, ConnectTimeout, ReadTimeout, HTTPError, InvalidHeader):
        setattr(m, cls.__name__, cls)
    m.exceptions = m

    def post(*a, **k):
        raise AssertionError("requests.post must be patched in tests")

    m.post = post
    return m


def _stub_pydantic():
    m = types.ModuleType("pydantic")
    m.__stub__ = True

    def Field(default=None, **_):
        return default

    class BaseModel:
        def __init__(self, **kw):
            for name in getattr(type(self), "__annotations__", {}):
                setattr(self, name, kw.get(name, getattr(type(self), name, None)))

    m.Field, m.BaseModel = Field, BaseModel
    return m


try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = _stub_requests()
try:
    import pydantic  # noqa: F401
except ImportError:
    sys.modules["pydantic"] = _stub_pydantic()

import requests  # noqa: E402

sys.path.insert(0, HERE)
import knowledge_assistant as ka  # noqa: E402

logging.getLogger(ka.__name__).addHandler(logging.NullHandler())  # keep test output quiet

REF = "p-7f3a"
TEXTS = {
    "timeout": "The assistant took too long to answer (ref p-7f3a). It has been reported; please try again.",
    "offline": "The assistant is offline right now (ref p-7f3a). It has been reported.",
    "http": "The assistant hit a problem (ref p-7f3a). It has been reported; please try again in a minute.",
    "forbidden": "The knowledge assistant rejected the request — check the token in this function's settings. (ref p-7f3a)",
}
TOKEN_TEXT = "The knowledge assistant rejected the request — check the token in this function's settings."
QUESTION = "What is the minimum cover over the SECRET-PIPE-QUESTION duct?"
TOKEN = "tok-abc123-not-a-real-token"

# The live pipe's __task__ block (as before the error flow), which must stay byte-for-byte: titles, tags and follow-ups
# must never reach the agent (each would run a full search and land in the chat's memory).
ORIGINAL_TASK_BLOCK = '''        # Open WebUI also asks the selected model to write chat titles, tags and follow-up suggestions.
        # Those must NOT reach the agent: each would run a full search and be stored in the chat's memory.
        if __task__:
            prompt = str(messages[-1].get("content", "")) if messages else ""
            if __task__ == "title_generation":
                first = re.search(r"USER:\\s*(.+)", prompt)
                title = (
                    first.group(1) if first else "Knowledge Assistant chat"
                ).strip()
                return json.dumps({"title": title[:60]})
            if __task__ == "follow_up_generation":
                return json.dumps({"follow_ups": []})
            if __task__ == "tags_generation":
                return json.dumps({"tags": []})
            return ""
'''


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


class Router:
    """Stands in for requests.post: the n8n_url call does `chat`, the incident_url call is recorded."""

    def __init__(self, pipe, chat, incident=None):
        self.pipe, self.chat, self.incident = pipe, chat, incident
        self.chat_calls, self.incident_calls = [], []

    def __call__(self, url, **kw):
        if url == self.pipe.valves.incident_url:
            self.incident_calls.append(kw)
            if isinstance(self.incident, BaseException):
                raise self.incident
            return self.incident or FakeResponse(200, {"ok": True})
        assert url == self.pipe.valves.n8n_url, url
        self.chat_calls.append(kw)
        if isinstance(self.chat, BaseException):
            raise self.chat
        return self.chat


def make_pipe(**valves):
    p = ka.Pipe()
    p.valves = p.Valves(**{"n8n_token": TOKEN, **valves})
    return p


def ask(pipe, chat, incident=None, question=QUESTION):
    router = Router(pipe, chat, incident)
    body = {"messages": [{"role": "user", "content": "earlier"}, {"role": "user", "content": question}]}
    with mock.patch.object(ka.requests, "post", side_effect=router):
        out = pipe.pipe(body, __user__={"id": "u1"}, __metadata__={"chat_id": "c1"})
    return out, router


class FailureMessageTest(unittest.TestCase):
    def test_failure_message_kinds(self):
        cases = [
            ((requests.ReadTimeout("read timed out"), None), "timeout"),
            ((requests.Timeout(), None), "timeout"),
            ((requests.ConnectionError("refused"), None), "offline"),
            ((requests.ConnectTimeout("connect timed out"), None), "offline"),
            ((None, 500), "http"),
            ((None, 502), "http"),
            ((requests.HTTPError("500"), 500), "http"),
            ((None, 403), "forbidden"),
        ]
        for (exc, status), kind in cases:
            with self.subTest(exc=exc, status=status):
                got_kind, text = ka.failure_message(exc, status, REF)
                self.assertEqual(got_kind, kind)
                self.assertEqual(text, TEXTS[kind])
                self.assertIn(REF, text)

    def test_forbidden_keeps_the_existing_token_text(self):
        _, text = ka.failure_message(None, 403, REF)
        self.assertTrue(text.startswith(TOKEN_TEXT))

    def test_empty_answer_shows_the_http_text(self):
        kind, text = ka.failure_message(ka.EmptyAnswer("empty output"), 200, REF)
        self.assertEqual((kind, text), ("empty", TEXTS["http"]))

    def test_empty_incident_message_uses_only_fixed_reasons(self):
        url = "http://host.docker.internal:5680/webhook/rag-chat"
        msg = ka.incident_message("empty", ka.EmptyAnswer(QUESTION), 200, url, 180)
        self.assertEqual(msg, "EmptyAnswer: host.docker.internal:5680 answered HTTP 200: no usable answer")
        for reason in ka.EmptyAnswer.REASONS:
            self.assertTrue(ka.incident_message("empty", ka.EmptyAnswer(reason), 200, url, 180).endswith(": " + reason))

    def test_unexpected_error_shows_the_http_text(self):
        kind, text = ka.failure_message(RuntimeError("boom"), None, REF)
        self.assertEqual((kind, text), ("error", TEXTS["http"]))

    def test_make_ref(self):
        for _ in range(20):
            self.assertRegex(ka.make_ref(), r"^p-[0-9a-f]{4}$")


class TaskTest(unittest.TestCase):
    def test_task_requests_never_reach_n8n(self):
        pipe = make_pipe()
        prompt = "### Task:\nGenerate a concise title.\n### Chat History:\nUSER: How deep is the trench?\nASSISTANT: 4 ft"
        body = {"messages": [{"role": "user", "content": prompt}]}
        with mock.patch.object(ka.requests, "post") as post:
            self.assertEqual(json.loads(pipe.pipe(body, __task__="title_generation")),
                             {"title": "How deep is the trench?"})
            self.assertEqual(json.loads(pipe.pipe(body, __task__="follow_up_generation")), {"follow_ups": []})
            self.assertEqual(json.loads(pipe.pipe(body, __task__="tags_generation")), {"tags": []})
            self.assertEqual(pipe.pipe(body, __task__="emoji_generation"), "")
            self.assertEqual(json.loads(pipe.pipe({"messages": []}, __task__="title_generation")),
                             {"title": "Knowledge Assistant chat"})
        post.assert_not_called()

    def test_task_block_is_unchanged(self):
        self.assertIn(ORIGINAL_TASK_BLOCK, inspect.getsource(ka))


class IncidentReportTest(unittest.TestCase):
    def test_incident_report_is_best_effort(self):
        for exc in (requests.ConnectionError("refused"), requests.Timeout(), RuntimeError("anything")):
            with self.subTest(exc=exc), mock.patch.object(ka.requests, "post", side_effect=exc) as post:
                self.assertFalse(
                    ka.report_incident("http://n8n:5680/webhook/rag-incident", TOKEN, "offline", "ConnectionError: x", REF))
                post.assert_called_once()
        with mock.patch.object(ka.requests, "post", return_value=FakeResponse(403, None, text="Forbidden")):
            self.assertFalse(ka.report_incident("http://n8n:5680/webhook/rag-incident", "wrong", "forbidden", "m", REF))

    def test_incident_report_shape(self):
        with mock.patch.object(ka.requests, "post", return_value=FakeResponse(200, {})) as post:
            ka.report_incident("http://n8n:5680/webhook/rag-incident", TOKEN, "timeout", "ReadTimeout: " + "x" * 400, REF)
        (url,), kw = post.call_args
        self.assertEqual(url, "http://n8n:5680/webhook/rag-incident")
        self.assertEqual(kw["timeout"], 3)
        self.assertEqual(kw["headers"], {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(set(kw["json"]), {"source", "kind", "message", "ref"})
        self.assertEqual((kw["json"]["source"], kw["json"]["kind"], kw["json"]["ref"]), ("pipe", "timeout", REF))
        self.assertLessEqual(len(kw["json"]["message"]), 200)

    def test_no_incident_url_means_no_report(self):
        with mock.patch.object(ka.requests, "post") as post:
            self.assertFalse(ka.report_incident("", TOKEN, "offline", "ConnectionError: x", REF))
        post.assert_not_called()


class PipeTest(unittest.TestCase):
    def test_valves_keep_old_names_and_defaults(self):
        v = ka.Pipe().valves
        self.assertEqual(v.n8n_url, "http://host.docker.internal:5680/webhook/rag-chat")
        self.assertEqual(v.n8n_token, "")
        self.assertEqual(v.timeout_seconds, 180)
        self.assertEqual(v.incident_url, "http://host.docker.internal:5680/webhook/rag-incident")
        # The stored valves JSON predates incident_url: loading it must still work and fill the default.
        stored = ka.Pipe.Valves(**{"n8n_url": "http://x/webhook/rag-chat", "n8n_token": "t", "timeout_seconds": 90})
        self.assertEqual(stored.incident_url, "http://host.docker.internal:5680/webhook/rag-incident")

    def test_normal_answer_passes_through(self):
        pipe = make_pipe()
        out, r = ask(pipe, FakeResponse(200, {"output": "The cover is 24 in [doc.pdf p. 3]"}))
        self.assertEqual(out, "The cover is 24 in [doc.pdf p. 3]")
        self.assertEqual(r.incident_calls, [])
        (kw,) = r.chat_calls
        self.assertEqual(kw["json"], {"chatInput": QUESTION, "sessionId": "u1:c1"})
        self.assertEqual(kw["headers"], {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(kw["timeout"], 180)

    def test_n8n_error_line_with_ref_is_shown_as_is(self):
        line = "I couldn't finish this answer because of a system problem (ref 48213). It has been reported automatically."
        out, r = ask(make_pipe(), FakeResponse(200, {"output": line, "ref": "48213"}))
        self.assertEqual(out, line)
        self.assertEqual(r.incident_calls, [])

    def test_failures_show_their_line_and_report_an_incident(self):
        cases = [
            (requests.ReadTimeout("read timed out"), "timeout"),
            (requests.ConnectionError("Connection refused"), "offline"),
            (FakeResponse(404, {"message": "The requested webhook is not registered."}), "http"),
            (FakeResponse(403, None, text="Forbidden"), "forbidden"),
        ]
        for chat, kind in cases:
            with self.subTest(kind=kind):
                out, r = ask(make_pipe(), chat)
                (inc,) = r.incident_calls
                ref = inc["json"]["ref"]
                self.assertRegex(ref, r"^p-[0-9a-f]{4}$")
                self.assertEqual(out, TEXTS[kind].replace(REF, ref))
                self.assertEqual((inc["json"]["source"], inc["json"]["kind"]), ("pipe", kind))
                self.assertEqual(inc["timeout"], 3)

    def test_empty_or_unreadable_answers_are_kind_empty(self):
        cases = [
            (FakeResponse(200, {"output": ""}), "no output text"),
            (FakeResponse(200, {"output": "   \n"}), "no output text"),
            (FakeResponse(200, {}), "no output text"),
            (FakeResponse(200, {"output": None, "ref": "123"}), "no output text"),
            (FakeResponse(200, {"output": {"text": "not a string"}}), "no output text"),
            (FakeResponse(200, ["a", "list"]), "JSON that is not an object"),
            (FakeResponse(200, None, text="<html>Bad gateway</html>"), "a body that is not JSON"),
            (FakeResponse(204, None, text=""), "a body that is not JSON"),
        ]
        for chat, reason in cases:
            with self.subTest(payload=chat._payload, text=chat._text):
                out, r = ask(make_pipe(), chat)
                (inc,) = r.incident_calls
                self.assertEqual(inc["json"]["kind"], "empty")
                self.assertEqual(inc["json"]["message"],
                                 f"EmptyAnswer: host.docker.internal:5680 answered HTTP {chat.status_code}: {reason}")
                self.assertEqual(out, TEXTS["http"].replace(REF, inc["json"]["ref"]))
                self.assertNotIn("empty answer", out)

    def test_n8n_5xx_is_left_to_n8ns_error_workflow(self):
        # a failed n8n execution already reaches the error workflow (its own incident and ref); the pipe only
        # shows its http line and logs it, so one failure is one alert.
        for status in (500, 502, 503):
            with self.subTest(status=status), self.assertLogs(ka.__name__, "WARNING") as logs:
                out, r = ask(make_pipe(), FakeResponse(status, {"message": "Error in workflow"}))
                self.assertEqual(r.incident_calls, [])
                ref = out.split("(ref ")[1].split(")")[0]
                self.assertRegex(ref, r"^p-[0-9a-f]{4}$")
                self.assertEqual(out, TEXTS["http"].replace(REF, ref))
                self.assertIn(ref, "\n".join(logs.output))
                self.assertIn(f"HTTP {status}", "\n".join(logs.output))

    def test_incident_message_never_carries_the_question_or_token(self):
        # requests.InvalidHeader quotes the header value (the token) in its text; a ConnectionError can quote URLs.
        leaky = [
            requests.exceptions.InvalidHeader(f"Invalid return character in header value: 'Bearer {TOKEN}\\n'"),
            requests.ConnectionError(f"failed for chatInput={QUESTION}"),
            RuntimeError(QUESTION),
            requests.ReadTimeout(QUESTION),
        ]
        for exc in leaky:
            with self.subTest(exc=type(exc).__name__), self.assertLogs(ka.__name__, "WARNING") as logs:
                out, r = ask(make_pipe(), exc)
                (inc,) = r.incident_calls
                msg = inc["json"]["message"]
                self.assertTrue(msg.startswith(type(exc).__name__), msg)
                self.assertLessEqual(len(msg), 200)
                for secret in (TOKEN, "SECRET-PIPE-QUESTION", "earlier"):
                    self.assertNotIn(secret, json.dumps(inc["json"]))
                    self.assertNotIn(secret, out)
                    self.assertNotIn(secret, "\n".join(logs.output))

    def test_incident_failure_still_shows_the_user_line(self):
        cases = [
            (requests.ConnectionError("refused"), requests.ConnectionError("refused"), "The assistant is offline"),
            # a wrong token: the incident webhook rejects the report with the same token, so no row can appear
            (FakeResponse(403, None, text="Forbidden"), FakeResponse(403, None, text="Forbidden"),
             "The knowledge assistant rejected the request"),
        ]
        for chat, incident, start in cases:
            with self.subTest(start=start), self.assertLogs(ka.__name__, "INFO") as logs:
                out, r = ask(make_pipe(), chat, incident=incident)
                self.assertEqual(len(r.incident_calls), 1)
                self.assertTrue(out.startswith(start), out)
                ref = r.incident_calls[0]["json"]["ref"]
                info = [line for line in logs.output if line.startswith("INFO:")]
                self.assertEqual(len(info), 1, logs.output)
                self.assertIn(ref, info[0])
                self.assertIn("not delivered", info[0])

    def test_empty_incident_url_skips_the_report(self):
        out, r = ask(make_pipe(incident_url=""), requests.ConnectionError("refused"))
        self.assertEqual(r.incident_calls, [])
        self.assertTrue(out.startswith("The assistant is offline right now (ref p-"))


if __name__ == "__main__":
    unittest.main()
