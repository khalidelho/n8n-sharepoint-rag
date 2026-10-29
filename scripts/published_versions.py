#!/usr/bin/env python3
"""Swap the editor drafts in an n8n export for the versions that actually run, and keep only released workflows.

    python3 scripts/published_versions.py <export_dir> <out_dir>

`n8n export:workflow` writes each workflow's editor DRAFT (its working copy), which can differ from the PUBLISHED
version n8n runs: an edit saved but never published, or a draft left behind by a rollback. This step reads the
published version from n8n's own Postgres (read-only SELECT) and, per workflow:

- nodes, connections and nodeGroups come from workflow_history at the entity's activeVersionId, which must equal
  workflow_published_version.publishedVersionId (otherwise it stops: the instance is mid-publish or inconsistent);
- everything else (name, settings, ...) comes from the export;
- only workflows that are active, named "RAG ...", not a test copy (no "test" in the name) and not listed in
  scripts/export-skip.txt (active on the source instance but not released yet) are written.

Environment: N8N_PG_CONTAINER (default rag-n8n-postgres-1), N8N_DB_USER / N8N_DB_NAME (default n8n / n8n).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

VERSIONED = ("nodes", "connections", "nodeGroups")   # what workflow_history keeps per version
SQL = """select coalesce(json_agg(json_build_object(
  'id', e.id, 'name', e.name, 'active', e.active,
  'activeVersionId', e."activeVersionId", 'publishedVersionId', p."publishedVersionId", 'draftVersionId', e."versionId",
  'nodes', h.nodes, 'connections', h.connections, 'nodeGroups', h."nodeGroups")), '[]'::json)
from workflow_entity e
left join workflow_history h on h."versionId" = e."activeVersionId"
left join workflow_published_version p on p."workflowId" = e.id"""


def published() -> dict:
    pg = os.environ.get("N8N_PG_CONTAINER", "rag-n8n-postgres-1")
    cmd = ["docker", "exec", pg, "psql", "-U", os.environ.get("N8N_DB_USER", "n8n"),
           "-d", os.environ.get("N8N_DB_NAME", "n8n"), "-At", "-v", "ON_ERROR_STOP=1", "-c", SQL]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        sys.exit(f"STOPPED: could not read published versions from {pg}: {r.stderr.strip()[-300:]}")
    return {w["id"]: w for w in json.loads(r.stdout)}


def skip_list() -> dict:
    f = pathlib.Path(__file__).with_name("export-skip.txt")
    out = {}
    for line in f.read_text().splitlines() if f.exists() else []:
        wid, _, why = line.partition("#")
        if wid.strip():
            out[wid.strip()] = why.strip() or "listed in export-skip.txt"
    return out


def why_not(wf: dict, pub: dict | None, skip: dict) -> str | None:
    name = wf.get("name", "")
    if not name.startswith("RAG"):
        return "not a RAG workflow"
    if "test" in name.lower():
        return "test copy"
    if wf["id"] in skip:
        return f"skip list: {skip[wf['id']]}"
    if not pub or not pub["active"] or not pub["activeVersionId"]:
        return "not active"
    return None


def main(src: str, dst: str) -> None:
    versions, skip = published(), skip_list()
    pathlib.Path(dst).mkdir(parents=True, exist_ok=True)
    kept = 0
    for f in sorted(pathlib.Path(src).glob("*.json")):
        data = json.loads(f.read_text())
        for wf in data if isinstance(data, list) else [data]:
            pub = versions.get(wf.get("id"))
            reason = why_not(wf, pub, skip)
            if reason:
                print(f"  skip  {wf.get('name', '?'):44} ({reason})")
                continue
            if pub["publishedVersionId"] != pub["activeVersionId"] or pub["nodes"] is None:
                sys.exit(f"STOPPED: {wf['id']}: active version {pub['activeVersionId']} is not the published one "
                         f"({pub['publishedVersionId']}) or has no history row")
            for k in VERSIONED:
                wf[k] = pub[k]
            note = "" if pub["draftVersionId"] == pub["activeVersionId"] else "  (editor draft differs; published used)"
            (pathlib.Path(dst) / f"{wf['id']}.json").write_text(json.dumps(wf, ensure_ascii=False) + "\n")
            print(f"  keep  {wf['name']:44} published {pub['activeVersionId']}{note}")
            kept += 1
    if not kept:
        sys.exit("STOPPED: no released workflows found")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
