"""Tests for install_function.py against a temporary SQLite file shaped like Open WebUI 0.12.3's webui.db.
No Docker, no network: the in-container program runs with this python (`python -`), and the restart is patched.

Run:  python3 -m unittest discover -s open-webui/ops -p 'test_install_function.py' -v
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import install_function as inst  # noqa: E402

# Open WebUI stores valves as plain JSON here (ENABLE_VALVE_ENCRYPTION is unset); the token lives in them.
# The installer must never change, read or print them.
VALVES = '{"n8n_token": "tok-SECRET-VALVE-VALUE"}'
OLD = '"""\ntitle: Knowledge Assistant\n"""\n\nclass Pipe:\n    pass  # old — version\n'
NEW = '"""\ntitle: Knowledge Assistant\n"""\n\nclass Pipe:\n    def pipe(self, body):\n        return "new — version"\n'
META = {"description": "Sends each message", "manifest": {"title": "Knowledge Assistant"}, "toggle": False, "i18n": {}}


def make_db(path, with_function=True):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE function (id VARCHAR NOT NULL, user_id VARCHAR, name TEXT, type TEXT, content TEXT, meta TEXT,
            valves TEXT, is_active BOOLEAN, is_global BOOLEAN, updated_at BIGINT, created_at BIGINT, version_id TEXT,
            PRIMARY KEY (id));
        CREATE TABLE function_history (id TEXT PRIMARY KEY, function_id TEXT NOT NULL, parent_id TEXT, snapshot JSON NOT NULL,
            user_id TEXT NOT NULL, commit_message TEXT, created_at BIGINT NOT NULL);
    """)
    if with_function:
        con.execute("insert into function values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("knowledge_assistant", "user-1", "Knowledge Assistant", "pipe", OLD, json.dumps(META), VALVES,
                     1, 0, 1000, 900, "v-old"))
        con.execute("insert into function_history values (?,?,?,?,?,?,?)",
                    ("v-old", "knowledge_assistant", None,
                     json.dumps({"name": "Knowledge Assistant", "content": OLD, "meta": {"description": "Sends each message"}}),
                     "user-1", None, 1000))
    con.execute("insert into function values (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("download_as_word", "user-1", "Download as Word", "action", "class Action: pass\n", "{}", None,
                 1, 1, 1100, 1100, "v-word"))
    con.commit()
    con.close()


def row(path, fid="knowledge_assistant"):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    r = con.execute("select * from function where id = ?", (fid,)).fetchone()
    con.close()
    return dict(r) if r else None


def history(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("select * from function_history order by created_at, id")]
    con.close()
    return rows


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "webui.db")
        make_db(self.db)
        self.src = self.write_src(NEW)
        patches = [
            mock.patch.object(inst, "EXEC_CMD", (sys.executable, "-")),
            mock.patch.object(inst, "DB_PATH", self.db),
            mock.patch.object(inst, "restart_and_wait", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write_src(self, text, name="knowledge_assistant.py"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = inst.main(list(argv))
        return code, out.getvalue()

    def test_install_updates_content_and_keeps_valves(self):
        before, word_before = row(self.db), row(self.db, "download_as_word")
        code, out = self.run_main(self.src)
        self.assertEqual(code, 0, out)
        after = row(self.db)
        self.assertEqual(after["content"], NEW)  # byte-for-byte, including the em dash
        for col in ("valves", "meta", "is_active", "is_global", "created_at", "user_id", "name", "type"):
            self.assertEqual(after[col], before[col], col)
        self.assertGreater(after["updated_at"], before["updated_at"])
        self.assertEqual(row(self.db, "download_as_word"), word_before)
        inst.restart_and_wait.assert_called_once_with()
        self.assertNotIn("tok-SECRET-VALVE-VALUE", out)
        self.assertNotIn("new — version", out)

    def test_install_records_a_history_version(self):
        self.run_main(self.src)
        after, hist = row(self.db), history(self.db)
        self.assertEqual(len(hist), 2)
        new = next(h for h in hist if h["id"] != "v-old")
        self.assertEqual(after["version_id"], new["id"])
        self.assertEqual(new["parent_id"], "v-old")
        self.assertEqual(new["function_id"], "knowledge_assistant")
        self.assertEqual(new["user_id"], "user-1")
        self.assertEqual(json.loads(new["snapshot"]),
                         {"name": "Knowledge Assistant", "content": NEW, "meta": {"description": "Sends each message"}})

    def test_dry_run_writes_nothing(self):
        before, hist_before = row(self.db), history(self.db)
        code, out = self.run_main(self.src, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(row(self.db), before)
        self.assertEqual(history(self.db), hist_before)
        inst.restart_and_wait.assert_not_called()
        self.assertIn(f"{len(OLD)} chars", out)
        self.assertIn(f"{len(NEW)} chars", out)
        self.assertIn("dry run", out)
        self.assertNotIn("tok-SECRET-VALVE-VALUE", out)
        self.assertNotIn("new — version", out)

    def test_same_content_changes_nothing(self):
        before = row(self.db)
        code, out = self.run_main(self.write_src(OLD, "same.py"))
        self.assertEqual(code, 0, out)
        self.assertEqual(row(self.db), before)
        self.assertEqual(len(history(self.db)), 1)
        inst.restart_and_wait.assert_not_called()
        self.assertIn("already", out)

    def test_missing_function_fails(self):
        os.remove(self.db)
        make_db(self.db, with_function=False)
        code, out = self.run_main(self.src)
        self.assertEqual(code, 1)
        self.assertIn("knowledge_assistant", out)
        inst.restart_and_wait.assert_not_called()

    def test_refuses_code_that_would_break_the_pipe(self):
        before = row(self.db)
        for text in ("class Pipe:\n    def pipe(self\n", "class Action:\n    pass\n"):
            for dry in ((), ("--dry-run",)):
                with self.subTest(text=text, dry=dry):
                    code, out = self.run_main(self.write_src(text, "bad.py"), *dry)
                    self.assertEqual(code, 1)
                    self.assertIn("refusing", out)
        self.assertEqual(row(self.db), before)
        self.assertEqual(len(history(self.db)), 1)
        inst.restart_and_wait.assert_not_called()

    def test_compiles_with_the_python_that_runs_the_program(self):
        # the syntax check runs inside the program (the container's Python 3.11), not with this python.
        program = inst.build_program("class Pipe:\n    def pipe(self\n", db=self.db, function_id="knowledge_assistant",
                                     write=True, note="n")
        result = inst.run_program(program, cmd=(sys.executable, "-"))
        self.assertFalse(result["ok"])
        self.assertIn("does not compile on Python %d.%d" % sys.version_info[:2], result["error"])
        self.assertEqual(row(self.db)["content"], OLD)
        with mock.patch.object(inst, "compile", side_effect=AssertionError("host compile"), create=True):
            code, out = self.run_main(self.src, "--dry-run")
        self.assertEqual(code, 0, out)

    def test_failed_restart_is_an_error(self):
        inst.restart_and_wait.return_value = False
        code, out = self.run_main(self.src)
        self.assertEqual(code, 1)
        self.assertEqual(row(self.db)["content"], NEW)

    def test_restart_that_raises_says_the_code_is_written(self):
        errors = [inst.subprocess.CalledProcessError(1, ["docker", "restart", inst.CONTAINER]),
                  inst.subprocess.TimeoutExpired(["docker", "restart", inst.CONTAINER], 120),
                  FileNotFoundError("docker")]
        for err in errors:
            with self.subTest(err=type(err).__name__):
                os.remove(self.db)
                make_db(self.db)
                inst.restart_and_wait.side_effect = err
                code, out = self.run_main(self.src)
                self.assertNotEqual(code, 0)
                self.assertIn("code written; restart failed: run `docker restart rag-openwebui-open-webui-1`", out)
                self.assertNotIn("Traceback", out)
                self.assertEqual(row(self.db)["content"], NEW)

    def test_program_that_hangs_or_cannot_start_is_an_error(self):
        for err in (inst.subprocess.TimeoutExpired(list(inst.EXEC_CMD), 120), FileNotFoundError("docker")):
            with self.subTest(err=type(err).__name__), mock.patch.object(inst.subprocess, "run", side_effect=err):
                code, out = self.run_main(self.src)
                self.assertEqual(code, 1)
                self.assertIn("failed:", out)
                self.assertNotIn("Traceback", out)
        inst.restart_and_wait.assert_not_called()


class RestartTest(unittest.TestCase):
    def test_waits_for_health(self):
        answers = [OSError("refused"), OSError("refused"), io.BytesIO(b'{"status":true}')]

        def urlopen(url, timeout):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            a.status = 200
            return a

        with mock.patch.object(inst.subprocess, "run") as run, \
                mock.patch.object(inst.urllib.request, "urlopen", side_effect=urlopen):
            self.assertTrue(inst.restart_and_wait(sleep=lambda s: None))
        (cmd,), _ = run.call_args
        self.assertEqual(cmd, ["docker", "restart", inst.CONTAINER])
        self.assertEqual(answers, [])

    def test_gives_up_after_the_wait(self):
        clock = iter(range(0, 1000, 10))
        with mock.patch.object(inst.subprocess, "run"), \
                mock.patch.object(inst.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertFalse(inst.restart_and_wait(wait=60, sleep=lambda s: None, now=lambda: next(clock)))


if __name__ == "__main__":
    unittest.main()
