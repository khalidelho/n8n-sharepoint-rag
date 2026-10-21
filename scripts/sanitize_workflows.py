#!/usr/bin/env python3
"""Turn raw n8n workflow exports into files that are safe to publish.

    python3 scripts/sanitize_workflows.py <export_dir> n8n/workflows

- keeps only what's needed to import a workflow (name, nodes, connections, settings, id)
- drops instance details: owner/project, version ids, pinned test data, static data
- keeps each node's credential *name* but drops its id (n8n matches by name on import)
- replaces webhook IDs with stable fresh ones (the originals are part of the live instance's URLs)
- applies private find→replace pairs from $PRIVATE_RAG_REPLACEMENTS
  (default ~/.config/private-rag/replacements.json — outside the repo, because the
  original values are exactly what must not be published)

Run scripts/check-leaks.sh afterwards.
"""
import json
import os
import pathlib
import re
import sys
import uuid

KEEP = ("id", "name", "nodes", "connections", "settings")


def slug(name: str) -> str:
    name = re.sub(r"^RAG\s*[–-]\s*", "", name)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def replace_all(obj, pairs):
    if isinstance(obj, dict):
        return {k: replace_all(v, pairs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_all(v, pairs) for v in obj]
    if isinstance(obj, str):
        for old, new in pairs:
            obj = obj.replace(old, new)
    return obj


def clean(wf: dict, pairs) -> dict:
    out = {k: wf[k] for k in KEEP if k in wf}
    out["active"] = False
    for node in out["nodes"]:
        for cred in (node.get("credentials") or {}).values():
            cred.pop("id", None)
        if "webhookId" in node:
            node["webhookId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"private-rag/{out['name']}/{node['name']}"))
    return replace_all(out, pairs)


def main(src: str, dst: str) -> None:
    rep = pathlib.Path(os.environ.get("PRIVATE_RAG_REPLACEMENTS",
                                      "~/.config/private-rag/replacements.json")).expanduser()
    pairs = json.loads(rep.read_text()) if rep.exists() else []
    if not pairs:
        print(f"warning: no replacements loaded from {rep}", file=sys.stderr)
    pathlib.Path(dst).mkdir(parents=True, exist_ok=True)
    for f in sorted(pathlib.Path(src).glob("*.json")):
        data = json.loads(f.read_text())
        for wf in data if isinstance(data, list) else [data]:
            if not wf.get("name", "").startswith("RAG"):
                continue
            target = pathlib.Path(dst) / f"{slug(wf['name'])}.json"
            target.write_text(json.dumps(clean(wf, pairs), indent=2, ensure_ascii=False) + "\n")
            print(f"{wf['name']:28} -> {target}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
