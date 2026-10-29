#!/usr/bin/env python3
"""Is every eligible SharePoint file accounted for?  GRAPH_CREDENTIAL_ID=<id> python3 ops/check-completeness.py [-v]

Lists the library through Microsoft Graph (borrowing n8n's own Graph token, in memory only), applies
the workflow's rules (top level + one subfolder level, folders named DNU / "Do Not Use" skipped; skip OLD_… and
"Do Not Use" files), and compares every file ID with document_metadata. Also reports folders it did NOT read. "remaining" = files with no status yet; "failed" = latest log entry failed/retry.
"""
import json, re, subprocess, sys, urllib.parse, urllib.request
sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout
import os
# the ingestion workflow (its Graph URLs say which library to list) and n8n's "Microsoft Graph – RAG" credential
# (n8n → Credentials → open it: the id is in the URL)
WF = os.environ.get("INGESTION_WORKFLOW_ID", "GwkCOkOcb9EAOX9e")
CRED = os.environ.get("GRAPH_CREDENTIAL_ID") or sys.exit("set GRAPH_CREDENTIAL_ID to the id of n8n's Graph credential")
wf = json.loads(sh(f"docker exec rag-n8n-n8n-main-1 sh -c 'n8n export:workflow --id={WF} --output=/tmp/cc.json >/dev/null 2>&1; cat /tmp/cc.json; rm -f /tmp/cc.json'"))
wf = wf[0] if isinstance(wf, list) else wf
url = next(n for n in wf["nodes"] if n["name"] == "Graph – list subfolders")["parameters"]["url"]   # .../drive/root/children?...
base = url.split("/root/children")[0]
tok = sh(f"""docker exec rag-n8n-n8n-main-1 sh -c 'n8n export:credentials --id={CRED} --decrypted --output=/tmp/g.json >/dev/null 2>&1; node -e "process.stdout.write(require(\\"/tmp/g.json\\")[0].data.oauthTokenData.access_token)"; rm -f /tmp/g.json'""").strip()
def children(path):
    u, out = f"{base}/{path}/children?$select=id,name,file,folder&$top=200", []
    while u:
        body = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"Authorization": f"Bearer {tok}"}), timeout=60))
        out += body["value"]; u = body.get("@odata.nextLink")
    return out
files, skipped_folders = [], []
try:
    for item in children("root"):
        if item.get("file"): files.append(item)
        elif item.get("folder"):
            if re.match(r"(?i)^dnu$|do not use", item["name"].strip()):
                skipped_folders.append(f"{item['name']} ({item['folder'].get('childCount')} items, skipped on purpose)"); continue
            for sub in children(f"root:/{urllib.parse.quote(item['name'])}:"):
                if sub.get("file"): files.append({**sub, "name": f"{item['name']}/{sub['name']}"})
                elif sub.get("folder"): skipped_folders.append(f"{item['name']}/{sub['name']} (nested, not read)")
except urllib.error.HTTPError as e:
    sys.exit(f"Graph error {e.code} — n8n's token may be stale; it refreshes on the next ingestion run.")
base_name = lambda n: n.rsplit("/", 1)[-1]
eligible = {f["id"]: f["name"] for f in files if not re.match(r"(?i)^old_", base_name(f["name"])) and not re.search(r"(?i)do not use", f["name"])}
q = lambda s: sh(f"docker exec supabase-db psql -U postgres -d postgres -tAF'|' -c \"{s}\"")
done = set(q("select file_id from document_metadata").split())
last = dict((l.split("|")[0], l.split("|")[1:]) for l in q("select distinct on (file_id) file_id, step, status from ingestion_log order by file_id, logged_at desc").splitlines() if l)
missing = [n for i, n in eligible.items() if i not in done]
failed = [f"{eligible[i]} ({last[i][0]})" for i in eligible if last.get(i, ["", ""])[1] in ("failed", "retry")]
skipped = [eligible[i] for i in eligible if last.get(i, ["", ""])[1] in ("skipped", "empty")]
print(f"eligible={len(eligible)} recorded={len(eligible)-len(missing)} remaining={len(missing)} failed={len(failed)} skipped_or_empty={len(skipped)} outdated_excluded={len(files)-len(eligible)}")
for f in skipped_folders: print("  folder not read:", f)
if "-v" in sys.argv:
    for n in sorted(missing): print("  REMAINING:", n)
    for n in sorted(failed): print("  FAILED:", n)
    for n in sorted(skipped): print("  skipped/empty:", n)
