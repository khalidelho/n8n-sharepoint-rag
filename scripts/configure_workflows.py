#!/usr/bin/env python3
"""Fill the placeholders in n8n/workflows/*.json with your own values, ready to import.

    cp workflows.env.example workflows.env     # then edit it
    python3 scripts/configure_workflows.py     # writes build/workflows/*.json

Placeholders look like __SHAREPOINT_SITE_ID__. Anything left unfilled is reported.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
values = {}
for line in (ROOT / "workflows.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()

out_dir = ROOT / "build" / "workflows"
out_dir.mkdir(parents=True, exist_ok=True)
for f in sorted((ROOT / "n8n" / "workflows").glob("*.json")):
    text = f.read_text()
    for k, v in values.items():
        text = text.replace(f"__{k}__", v)
    (out_dir / f.name).write_text(text)
    left = sorted(set(re.findall(r"__([A-Z][A-Z0-9_]+)__", text)))
    print(f"{f.name:24} {'ok' if not left else 'still unfilled: ' + ', '.join(left)}")
print(f"\nImport from {out_dir} (n8n → Workflows → Import from file).")
