#!/usr/bin/env python3
"""Install a new version of the Knowledge Assistant pipe into the running Open WebUI, then restart it.

  python3 open-webui/ops/install_function.py open-webui/functions/knowledge_assistant.py --dry-run
  python3 open-webui/ops/install_function.py open-webui/functions/knowledge_assistant.py

What it does: replaces `function.content` for id knowledge_assistant in /app/backend/data/webui.db (plain sqlite3,
run inside the container with `docker exec -i ... python -`), bumps `updated_at`, and records the new code as a
version in `function_history` exactly as Open WebUI's own Save does (so Admin -> Functions -> history stays true).
`valves` (plain JSON here, since ENABLE_VALVE_ENCRYPTION is unset; the token lives there) and everything else in the
row are left as stored and never read or printed. Then `docker restart` and a wait for /health. Stdlib only.
It refuses a file with no `class Pipe`, or one that doesn't compile on the container's Python (3.11), in both modes.

--dry-run: prints only sizes, hashes and the number of changed lines; writes nothing, no restart.
Rollback: re-install an earlier version from git history, e.g.
  git show <commit>:open-webui/functions/knowledge_assistant.py > /tmp/knowledge_assistant.orig.py
  python3 open-webui/ops/install_function.py /tmp/knowledge_assistant.orig.py
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request

CONTAINER = "rag-openwebui-open-webui-1"
DB_PATH = "/app/backend/data/webui.db"
FUNCTION_ID = "knowledge_assistant"
HEALTH_URL = "http://localhost:3000/health"
HEALTH_WAIT_SECONDS = 180
EXEC_CMD = ("docker", "exec", "-i", CONTAINER, "python", "-")

# Runs inside the container (Python 3.11), fed on stdin after an `ARGS = ...` line. First compiles the new code
# with that Python (the one Open WebUI will run it with). Then one transaction: reads and writes only content,
# updated_at and version_id of the one row, plus one new function_history row. Prints one JSON line of sizes and
# hashes, never code or valves.
PROGRAM = r'''
import difflib, hashlib, json, sqlite3, sys, time, uuid

db, fid, new, write, note = ARGS["db"], ARGS["id"], ARGS["content"], ARGS["write"], ARGS["note"]

try:
    compile(new, "<pipe>", "exec")
except (SyntaxError, ValueError) as e:
    print(json.dumps({"ok": False, "error": "refusing: the file does not compile on Python %d.%d (%s, line %s)"
                      % (sys.version_info[0], sys.version_info[1], getattr(e, "msg", e), getattr(e, "lineno", "?"))}))
    sys.exit(2)


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def snapshot_meta(meta):
    # open_webui.models.function_history.function_snapshot: what a saved version keeps of meta
    meta = dict(meta or {})
    meta.setdefault("description", None)
    if not meta.get("i18n"):
        meta.pop("i18n", None)
    for key in ("manifest", "has_user_valves", "toggle"):
        meta.pop(key, None)
    return meta


con = sqlite3.connect(db, timeout=30)
try:
    found = con.execute("select name, content, meta, user_id, version_id from function where id = ?", (fid,)).fetchone()
    if found is None:
        print(json.dumps({"ok": False, "error": "no function with id %s in %s" % (fid, db)}))
        sys.exit(2)
    name, old, meta, user_id, parent = found
    old = old or ""
    changed = sum(1 for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
                  if line[:1] in "+-" and not line.startswith(("+++", "---")))
    out = {"ok": True, "old_chars": len(old), "new_chars": len(new), "changed_lines": changed,
           "old_sha": sha(old), "new_sha": sha(new), "same": old == new, "written": False}
    if write and old != new:
        now, version = int(time.time()), str(uuid.uuid4())
        snap = {"name": name, "content": new, "meta": snapshot_meta(json.loads(meta) if meta else {})}
        with con:  # one transaction: the version row and the live row change together or not at all
            con.execute("insert into function_history (id, function_id, parent_id, snapshot, user_id, commit_message,"
                        " created_at) values (?, ?, ?, ?, ?, ?, ?)",
                        (version, fid, parent, json.dumps(snap), user_id or "", note, now))
            cur = con.execute("update function set content = ?, updated_at = ?, version_id = ? where id = ?",
                              (new, now, version, fid))
            if cur.rowcount != 1:
                raise RuntimeError("update touched %d rows" % cur.rowcount)
        out["written"] = True
    print(json.dumps(out))
finally:
    con.close()
'''


def build_program(content, *, db, function_id, write, note):
    args = {"db": db, "id": function_id, "content": content, "write": bool(write), "note": note}
    return "import json\nARGS = json.loads(%r)\n" % json.dumps(args) + PROGRAM


def run_program(program, cmd=None):
    """Run the program with `python -` (in the container by default); its last stdout line is the JSON result."""
    cmd = list(cmd or EXEC_CMD)
    try:
        p = subprocess.run(cmd, input=program, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "no result within 120 s; the code may or may not be written: rerun with --dry-run"}
    except OSError as e:
        return {"ok": False, "error": "could not run %s (%s)" % (cmd[0], type(e).__name__)}
    lines = p.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        tail = (p.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return {"ok": False, "error": "exit %s: %s" % (p.returncode, tail[0][-300:])}


def restart_and_wait(wait=HEALTH_WAIT_SECONDS, sleep=time.sleep, now=time.monotonic):
    """docker restart, then poll /health until it answers {"status": true}. False if it never does within `wait`."""
    subprocess.run(["docker", "restart", CONTAINER], check=True, capture_output=True, text=True, timeout=120)
    deadline = now() + wait
    while now() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
                if r.status == 200 and json.loads(r.read() or b"{}").get("status") is True:
                    return True
        except (OSError, ValueError):
            pass
        sleep(2)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install the Knowledge Assistant pipe's code into Open WebUI.")
    ap.add_argument("path", help="the pipe's source file, e.g. open-webui/functions/knowledge_assistant.py")
    ap.add_argument("--dry-run", action="store_true", help="show how much would change; write nothing, no restart")
    a = ap.parse_args(argv)

    with open(a.path, encoding="utf-8", newline="") as f:  # newline="": install the file's bytes exactly
        content = f.read()
    if not re.search(r"^class Pipe\b", content, re.M):
        print(f"refusing: {a.path} has no `class Pipe`; {FUNCTION_ID} is a pipe")
        return 1

    note = "install_function.py: " + a.path.replace("\\", "/").rsplit("/", 1)[-1]
    result = run_program(build_program(content, db=DB_PATH, function_id=FUNCTION_ID, write=not a.dry_run, note=note))
    if not result.get("ok"):
        print(f"failed: {result.get('error')}")
        return 1
    print(f"{FUNCTION_ID}: live {result['old_chars']} chars (sha {result['old_sha']}) -> file {result['new_chars']} "
          f"chars (sha {result['new_sha']}), {result['changed_lines']} changed lines")
    if result["same"]:
        print("live code already matches the file; nothing written, no restart")
        return 0
    if a.dry_run:
        print("dry run: nothing written, no restart")
        return 0
    if not result.get("written"):
        print("failed: nothing was written")
        return 1
    print(f"content updated and versioned (valves kept); restarting {CONTAINER} ...", flush=True)
    try:
        healthy = restart_and_wait()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"code written; restart failed: run `docker restart {CONTAINER}` ({type(e).__name__})")
        return 1
    if not healthy:
        print(f"Open WebUI did not report healthy within {HEALTH_WAIT_SECONDS} s: check `docker logs {CONTAINER}`")
        return 1
    print("Open WebUI is healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
