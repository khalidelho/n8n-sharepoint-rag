#!/usr/bin/env python3
"""Make a meeting recording searchable: add its Whisper transcript to the knowledge base, attached to the video's
own SharePoint file ID, so answers cite the recording (with timestamps) and nothing is written to SharePoint.

    mlx_whisper talk.wav --model mlx-community/whisper-large-v3-turbo --language en \
        --output-format json --condition-on-previous-text False
    python3 ingest-transcript.py talk.json "<exact SharePoint file name of the .mp4>"

- Chunks: consecutive Whisper segments up to ~2,400 characters (~3–4 minutes of speech), each starting with the
  recording's name and time range, with [hh:mm:ss] markers every few segments.
- Embeddings: text-embedding-3-small on Azure (key and endpoint borrowed from n8n's "Azure AI – RAG" credential,
  in memory only) — the same model the rest of the knowledge base uses.
- Idempotent: replaces any earlier chunks for that file ID; logs to ingestion_log.
- If the video itself changes in SharePoint, the ingestion workflow clears these chunks — re-run this script.
"""
import json, subprocess, sys, urllib.request

MAX_CHARS = 2400
sh = lambda c, inp=None: subprocess.run(c, shell=True, capture_output=True, text=True, input=inp)


def hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def chunks(segments, name):
    out, cur, start = [], [], None
    for i, s in enumerate(segments):
        text = s["text"].strip()
        if not text:
            continue
        if start is None:
            start = s["start"]
        piece = (f"[{hms(s['start'])}] " if len(cur) % 6 == 0 else "") + text
        if cur and sum(len(x) + 1 for x in cur) + len(piece) > MAX_CHARS:
            out.append((start, prev_end, cur)); cur, start = [], s["start"]
            piece = f"[{hms(s['start'])}] " + text
        cur.append(piece); prev_end = s["end"]
    if cur:
        out.append((start, prev_end, cur))
    return [(a, b, f"Transcript of the meeting recording “{name}”, {hms(a)}–{hms(b)}:\n" + " ".join(c)) for a, b, c in out]


def main(transcript, file_name):
    segs = json.load(open(transcript))["segments"]
    # file names can contain quotes, $ or backticks: pass the name as a psql variable over stdin, no shell involved
    file_id = subprocess.run(["docker", "exec", "-i", "supabase-db", "psql", "-U", "postgres", "-d", "postgres", "-tA",
                              "-v", f"fname={file_name}"], input="select file_id from document_metadata where file_name = :'fname';",
                             capture_output=True, text=True).stdout.strip()
    if not file_id:
        sys.exit(f"No document_metadata row named {file_name!r} — let ingestion see the video first.")

    cid = sh("docker exec rag-n8n-postgres-1 psql -U n8n -d n8n -tAc \"select id from credentials_entity where name = 'Azure AI – RAG'\"").stdout.strip()
    cred = json.loads(sh(f"""docker exec rag-n8n-n8n-main-1 sh -c 'n8n export:credentials --id={cid} --decrypted --output=/tmp/a.json >/dev/null 2>&1; node -e "const d=require(\\"/tmp/a.json\\")[0].data; process.stdout.write(JSON.stringify({{k:d.apiKey,e:d.foundryEndpoint}}))"; rm -f /tmp/a.json'""").stdout)

    parts = chunks(segs, file_name)
    vectors = []
    for i in range(0, len(parts), 16):
        body = json.dumps({"model": "text-embedding-3-small", "input": [p[2] for p in parts[i:i + 16]]}).encode()
        req = urllib.request.Request(cred["e"].rstrip("/") + "/embeddings", data=body,
                                     headers={"api-key": cred["k"], "Content-Type": "application/json"})
        vectors += [d["embedding"] for d in json.load(urllib.request.urlopen(req, timeout=120))["data"]]

    lit = lambda s: "'" + s.replace("'", "''") + "'"
    sql = ["begin;", f"delete from documents where metadata->>'file_id' = {lit(file_id)};"]
    for n, ((a, b, text), vec) in enumerate(zip(parts, vectors)):
        meta = {"file_id": file_id, "file_name": file_name, "chunk_index": n, "source": "video transcript",
                "start": hms(a), "end": hms(b)}
        sql.append(f"insert into documents (content, metadata, embedding) values ({lit(text)}, {lit(json.dumps(meta))}::jsonb, '{json.dumps(vec)}');")
    sql.append(f"insert into ingestion_log (file_id, file_name, step, status, chunk_count, message) values ({lit(file_id)}, {lit(file_name)}, 'vector_write', 'ok', {len(parts)}, 'video transcript (local Whisper large-v3-turbo), {hms(segs[-1]['end'])} long');")
    sql.append("commit;")
    r = sh("docker exec -i supabase-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -q", "\n".join(sql))
    if r.returncode:
        sys.exit(r.stderr[-800:])
    print(f"{file_name}: {len(parts)} passages from {len(segs)} segments ({hms(segs[-1]['end'])})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
