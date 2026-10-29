"""Tests for ops/backup.sh. Run from the repo root:  python3 -m unittest ops/tests/test_backup.py -v

stdlib only. The script runs for real under /bin/bash (3.2 on macOS) with a stub `docker` first on its PATH (the script
puts $HOME/.docker/bin first, so each test points HOME at a temp dir holding the stub) and a local HTTP server standing
in for the incident webhook. No test touches Docker, a container or a volume; setUpClass refuses to run if the
script's PATH would find a real docker instead of the stub.
"""
import http.server
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "backup.sh"
TOKEN = "test-token-7f3a9c"
SCRIPT_PATH = "$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# A fake docker: logs each call (one JSON array per line) and imitates just what backup.sh asks of it.
#   STUB_FAIL=<substring of the joined argv> -> that call fails · STUB_WF=bad|empty -> broken workflows.json
#   STUB_TOC_DROP=<table> -> that table missing from pg_restore --list · STUB_OWU_RUNNING=false|missing -> Open WebUI
#   stopped | no such container · STUB_QUICK_CHECK=bad -> the archived webui.db fails quick_check
STUB = r'''
import io, json, os, sys, tarfile
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if os.environ.get("STUB_FAIL") and os.environ["STUB_FAIL"] in " ".join(args):
    sys.stderr.write("stub docker: forced failure\n"); sys.exit(1)

def mount(target):
    for i, a in enumerate(args):
        if a == "-v" and args[i + 1].split(":")[1] == target:
            return args[i + 1].split(":")[0]

def tgz(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            else:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))

cmd = args[0]
if cmd == "exec":
    what = args[2]
    if what == "pg_dump":
        sys.stdout.buffer.write(b"PGDMP fake dump of " + args[-1].encode() + b"\n" * 5000)
    elif what == "n8n":
        print("Successfully exported 2 workflows.")
elif cmd == "cp":
    src, dst = args[-2], args[-1]
    if "workflows" in src:
        wf = os.environ.get("STUB_WF")
        open(dst, "w").write("not json {" if wf == "bad" else "[]" if wf == "empty" else '[{"id": "a"}, {"id": "b"}]')
    else:
        open(dst, "wb").write(b"SQLite format 3\x00snapshot")
elif cmd == "inspect":
    state = os.environ.get("STUB_OWU_RUNNING", "true")
    if state == "missing":
        sys.stderr.write("Error: No such object: " + args[-1] + "\n"); sys.exit(1)
    print(state + " ghcr.io/open-webui/open-webui:v0.12.3")
elif cmd == "run":
    if "--entrypoint" in args:                       # quick_check of the archived webui.db
        assert os.path.isfile(os.path.join(mount("/c"), "webui.db")), "webui.db not extracted for the check"
        if os.environ.get("STUB_QUICK_CHECK") == "bad":
            sys.stderr.write("quick_check: *** in database main ***\n"); sys.exit(1)
    elif "pg_restore" in args:
        dump = os.path.basename(args[-1])
        tables = {"n8n.dump": [("public", "workflow_entity"), ("public", "credentials_entity")],
                  "supabase.dump": [("public", "documents"), ("public", "page_views"), ("storage", "objects")]}[dump]
        print(";\n; Archive created at 2026-10-27\n;")
        for i, (schema, table) in enumerate(tables):
            if table != os.environ.get("STUB_TOC_DROP"):
                print(f"{4000 + i}; 0 {16000 + i} TABLE DATA {schema} {table} owner")
    elif mount("/d") and "open-webui" in mount("/d"):
        tgz(os.path.join(mount("/b"), "open-webui-data.tgz"),
            [("./", None), ("./uploads/", None), ("./webui.db", b"SQLite format 3\x00snapshot")])
    elif mount("/d") and "storage" in mount("/d"):
        tgz(os.path.join(mount("/b"), "supabase-storage.tgz"),
            [("./", None), ("./stub/stub/document-images/", None),
             ("./stub/stub/document-images/DOC1/p1-img-1.jpeg/v1", b"\xff\xd8jpeg"),
             ("./stub/stub/document-images/DOC1/p1-img-2.jpeg/v1", b"\xff\xd8jpeg")])
'''


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.posts.append({"auth": self.headers.get("Authorization"), "path": self.path, "body": json.loads(body)})
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class BackupScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        cls.server.posts, cls.server.status = [], 200
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/webhook/rag-incident"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-test-"))
        self.home = self.tmp / "home"
        bindir = self.home / ".docker" / "bin"
        bindir.mkdir(parents=True)
        self.stub = bindir / "docker"
        self.stub.write_text(f"#!{sys.executable}\n{STUB}")
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)
        self.stub_log = self.tmp / "docker-calls.log"
        self.stub_log.touch()
        self.env_file = self.tmp / "n8n.env"
        self.env_file.write_text(f'N8N_HOST=localhost\nRAG_WEBHOOK_TOKEN="{TOKEN}"\nOTHER=1\n')
        self.bk = self.tmp / "rag-backups"
        self.server.posts.clear()
        self.server.status = 200
        # the safety check: the script's own PATH must resolve docker to the stub, never to a real Docker CLI
        found = subprocess.run(["/bin/bash", "-c", f'export PATH="{SCRIPT_PATH}"; command -v docker'],
                               env={"HOME": str(self.home)}, capture_output=True, text=True).stdout.strip()
        if found != str(self.stub):
            self.fail(f"the script would run {found!r}, not the stub: refusing to touch real Docker")

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def run_backup(self, **env):
        base = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "STUB_LOG": str(self.stub_log),
                "BACKUP_DIR": str(self.bk), "RAG_ENV_FILE": str(self.env_file), "RAG_INCIDENT_URL": self.url}
        base.update({k: str(v) for k, v in env.items()})
        p = subprocess.run(["/bin/bash", str(SCRIPT)], env=base, capture_output=True, text=True, timeout=120)
        self.assertNotIn(TOKEN, p.stdout + p.stderr, "the webhook token leaked into the output")
        self.assertNotIn(TOKEN, self.stub_log.read_text(), "the webhook token reached a docker command line")
        return p

    def sets(self):
        return sorted(d.name for d in self.bk.iterdir() if re.fullmatch(r"\d{8}-\d{4}", d.name)) if self.bk.exists() else []

    def docker_calls(self):
        return [json.loads(line) for line in self.stub_log.read_text().splitlines()]

    # --- syntax ---
    def test_syntax(self):
        subprocess.run(["/bin/bash", "-n", str(SCRIPT)], check=True)

    # --- happy paths ---
    def test_ok_with_notify_writes_a_whole_set_and_posts_ok(self):
        p = self.run_backup()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [name] = self.sets()
        s = self.bk / name
        self.assertEqual(sorted(f.name for f in s.iterdir()), sorted(
            ["n8n.dump", "supabase.dump", "workflows.json", "open-webui-data.tgz", "supabase-storage.tgz",
             "last_backup.json"]))
        status = json.loads((s / "last_backup.json").read_text())
        self.assertIs(status["ok"], True)
        self.assertEqual(status["path"], str(s))
        self.assertEqual(status["workflows"], 2)
        self.assertEqual(status["storage_files"], 2)
        self.assertEqual(status["bytes"], sum(f.stat().st_size for f in s.iterdir() if f.name != "last_backup.json"))
        self.assertEqual(set(status["files"]), {"n8n.dump", "supabase.dump", "workflows.json", "open-webui-data.tgz",
                                                "supabase-storage.tgz"})
        self.assertEqual(json.loads((self.bk / "last_backup.json").read_text()), status)
        self.assertEqual(stat.S_IMODE(s.stat().st_mode), 0o700)
        checks = [c for c in self.docker_calls() if c[0] == "run" and "--entrypoint" in c]
        self.assertEqual(len(checks), 1)
        self.assertIn("ghcr.io/open-webui/open-webui:v0.12.3", checks[0])
        self.assertIn("quick_check", " ".join(checks[0]))
        self.assertFalse((s / ".check").exists())
        self.assertFalse(list(self.bk.glob("*.partial")))
        [post] = self.server.posts
        self.assertEqual(post["auth"], f"Bearer {TOKEN}")
        self.assertEqual(post["path"], "/webhook/rag-incident")
        b = post["body"]
        self.assertEqual((b["source"], b["kind"]), ("backup", "ok"))
        self.assertEqual(b["data"], {"bytes": status["bytes"], "path": str(s)})
        self.assertRegex(b["message"], r"^\d+(\.\d)? [MG]B in " + re.escape(str(s)) + "$")

    def test_notify_off_posts_nothing_and_never_reads_the_token_file(self):
        self.env_file.unlink()
        os.mkfifo(self.env_file)       # reading a writer-less FIFO blocks: the run would time out if it read the file
        p = self.run_backup(BACKUP_NOTIFY=0)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.sets()), 1)
        self.assertEqual(self.server.posts, [])
        self.assertIn("notify off", p.stdout)

    def test_live_volumes_are_only_ever_mounted_read_only(self):
        self.assertEqual(self.run_backup(BACKUP_NOTIFY=0).returncode, 0)
        runs = [c for c in self.docker_calls() if c[0] == "run"]
        self.assertTrue(runs)
        for c in runs:
            self.assertIn("--rm", c)
            self.assertIn("--network", c)
            for i, a in enumerate(c):
                if a == "-v" and not c[i + 1].startswith("/"):          # a named (live) volume
                    self.assertTrue(c[i + 1].endswith(":ro"), c)
                if a == "-v" and "pg_restore" in c:                       # validation reads the set, never writes it
                    self.assertTrue(c[i + 1].endswith(":ro"), c)

    def test_open_webui_snapshot_when_running_and_plain_copy_when_stopped(self):
        self.assertEqual(self.run_backup(BACKUP_NOTIFY=0).returncode, 0)
        self.assertTrue(any(c[:3] == ["exec", "rag-openwebui-open-webui-1", "python3"] for c in self.docker_calls()))
        self.stub_log.write_text("")
        (self.bk / self.sets()[0]).rename(self.bk / "19990101-0000")       # free this minute's stamp
        p = self.run_backup(BACKUP_NOTIFY=0, STUB_OWU_RUNNING="false")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Open WebUI is not running", p.stdout)
        self.assertFalse(any(c[0] == "exec" and c[2] == "python3" for c in self.docker_calls()))

    # --- failures: ERR trap, one 'failed' incident, non-zero exit, no whole set ---
    def assert_failed(self, p, step, posts=1):
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(f"Backup failed at '{step}'", p.stdout)
        self.assertEqual(len(self.server.posts), posts, self.server.posts)
        if posts:
            b = self.server.posts[0]["body"]
            self.assertEqual((b["source"], b["kind"]), ("backup", "failed"))
            self.assertIn(f"'{step}'", b["message"])
            self.assertEqual(self.server.posts[0]["auth"], f"Bearer {TOKEN}")
        self.assertEqual(self.sets(), [])
        self.assertFalse((self.bk / "last_backup.json").exists())

    def test_missing_open_webui_container_is_an_error_not_stopped(self):
        p = self.run_backup(STUB_OWU_RUNNING="missing")
        self.assert_failed(p, "open-webui archive")
        self.assertIn("rag-openwebui-open-webui-1 not found", self.server.posts[0]["body"]["message"])
        self.assertFalse(any(c[0] == "run" and any("open-webui-data:/d" in a for a in c) for c in self.docker_calls()))

    def test_archived_webui_db_failing_quick_check_fails_validation(self):
        p = self.run_backup(STUB_QUICK_CHECK="bad")
        self.assert_failed(p, "validate open-webui-data.tgz")
        self.assertIn("quick_check", self.server.posts[0]["body"]["message"])

    def test_refuses_to_start_beside_this_minutes_partial_and_leaves_it_alone(self):
        import time
        self.bk.mkdir(mode=0o700)
        now = time.time()
        others = [self.bk / (time.strftime("%Y%m%d-%H%M", time.localtime(t)) + ".partial") for t in (now, now + 60)]
        for d in others:                          # another run's set in progress (this minute, or the next if we cross)
            d.mkdir(exist_ok=True)
            (d / "n8n.dump").write_text("theirs")
        p = self.run_backup()
        self.assert_failed(p, "preflight")
        b = self.server.posts[0]["body"]
        self.assertIn("another backup is running", b["message"])
        self.assertEqual(b["data"]["path"], str(self.bk))
        for d in others:
            self.assertEqual(sorted(f.name for f in d.iterdir()), ["n8n.dump"])
            self.assertEqual((d / "n8n.dump").read_text(), "theirs")
        self.assertEqual(self.docker_calls(), [])

    def test_unwritable_backup_dir_fails_and_posts_failed(self):
        p = self.run_backup(BACKUP_DIR="/nonexistent/ro")
        self.assert_failed(p, "preflight")
        self.assertIn("cannot create /nonexistent/ro", self.server.posts[0]["body"]["message"])

    def test_unwritable_backup_dir_with_notify_off_fails_quietly(self):
        p = self.run_backup(BACKUP_DIR="/nonexistent/ro", BACKUP_NOTIFY=0)
        self.assert_failed(p, "preflight", posts=0)

    def test_failed_dump_keeps_a_partial_set_marked_not_ok(self):
        p = self.run_backup(STUB_FAIL="pg_dump -U postgres")
        self.assert_failed(p, "supabase dump")
        [partial] = list(self.bk.glob("*.partial"))
        status = json.loads((partial / "last_backup.json").read_text())
        self.assertIs(status["ok"], False)
        self.assertIn("supabase dump", status["error"])
        self.assertEqual(self.server.posts[0]["body"]["data"]["path"], str(partial))

    def test_failure_inside_command_substitution_posts_once(self):
        p = self.run_backup(STUB_FAIL="pg_restore --list /b/n8n.dump")
        self.assert_failed(p, "validate n8n.dump")

    def test_dump_without_key_table_fails_validation(self):
        p = self.run_backup(STUB_TOC_DROP="documents")
        self.assert_failed(p, "validate supabase.dump")
        self.assertIn("no data for public documents", self.server.posts[0]["body"]["message"])

    def test_broken_or_empty_workflows_json_fails_validation(self):
        self.assert_failed(self.run_backup(STUB_WF="bad"), "validate workflows.json")
        self.server.posts.clear()
        for d in self.bk.glob("*.partial"):
            subprocess.run(["rm", "-rf", str(d)])
        self.assert_failed(self.run_backup(STUB_WF="empty"), "validate workflows.json")

    def test_missing_docker_fails_in_preflight(self):
        self.stub.unlink()
        found = subprocess.run(["/bin/bash", "-c", f'export PATH="{SCRIPT_PATH}"; command -v docker'],
                               env={"HOME": str(self.home)}, capture_output=True, text=True).stdout.strip()
        if found:
            self.skipTest(f"a docker outside HOME is on the script's PATH ({found})")
        p = self.run_backup()
        self.assert_failed(p, "preflight")
        self.assertIn("docker not found", self.server.posts[0]["body"]["message"])

    def test_ok_post_failing_exits_3_and_keeps_the_set(self):
        self.server.status = 500
        p = self.run_backup()
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(len(self.sets()), 1)
        self.assertEqual([x["body"]["kind"] for x in self.server.posts], ["ok"])    # no 'failed' on top

    # --- pruning ---
    def test_prune_keeps_the_newest_sets_and_leaves_other_files_alone(self):
        self.bk.mkdir(mode=0o700)
        old = [f"202501{d:02d}-0340" for d in range(1, 17)]
        for name in old + ["20250117-0340.partial", "notes"]:
            (self.bk / name).mkdir()
        os.utime(self.bk / "20250117-0340.partial", (1735700000, 1735700000))     # a failed run from long ago
        (self.bk / "README").write_text("keep me")
        p = self.run_backup(BACKUP_NOTIFY=0, BACKUP_KEEP=3)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        kept = self.sets()
        self.assertEqual(len(kept), 3)
        self.assertEqual(kept[:2], old[-2:])
        self.assertTrue((self.bk / "notes").is_dir())
        self.assertTrue((self.bk / "README").exists())
        self.assertFalse(list(self.bk.glob("*.partial")))

    def test_failed_attempts_are_cleared_at_the_next_start_but_not_while_young(self):
        self.bk.mkdir(mode=0o700)
        (self.bk / "20250101-0340.partial").mkdir()
        os.utime(self.bk / "20250101-0340.partial", (1735700000, 1735700000))
        (self.bk / "20250102-0340.partial").mkdir()        # just now: may be a run still going
        p = self.run_backup(STUB_FAIL="pg_dump -U postgres")
        self.assert_failed(p, "supabase dump")
        left = sorted(d.name for d in self.bk.glob("*.partial"))
        self.assertNotIn("20250101-0340.partial", left)
        self.assertIn("20250102-0340.partial", left)
        self.assertEqual(len(left), 2)                     # the young one + this run's own

    def test_bad_keep_value_fails_before_touching_anything(self):
        self.bk.mkdir(mode=0o700)
        for name in ["20250101-0340", "20250102-0340"]:
            (self.bk / name).mkdir()
        for keep in ["0", "abc", "-1", "07"]:   # empty = unset = the default 14
            self.server.posts.clear()
            p = self.run_backup(BACKUP_KEEP=keep)
            self.assertNotEqual(p.returncode, 0, keep)
            self.assertIn("BACKUP_KEEP", p.stdout)
            self.assertEqual(self.sets(), ["20250101-0340", "20250102-0340"])
            self.assertFalse(list(self.bk.glob("*.partial")))
            self.assertEqual([x["body"]["kind"] for x in self.server.posts], ["failed"])
        self.assertEqual(self.docker_calls(), [])


if __name__ == "__main__":
    unittest.main()
