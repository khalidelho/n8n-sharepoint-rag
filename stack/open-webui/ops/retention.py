"""Delete Open WebUI chats that haven't been touched for RETENTION_DAYS (default 90).

Runs inside an Open WebUI container (same image, same data volume) and uses Open WebUI's own
delete function, so a chat's messages, shared links and automation references go with it.
  python retention.py            -> delete
  python retention.py --dry-run  -> only count
  python retention.py --loop     -> run once a day forever (the compose sidecar)
"""
import asyncio
import os
import sys
import time

from sqlalchemy import select

from open_webui.internal.db import get_async_db_context
from open_webui.models.chats import Chat, Chats

DAYS = int(os.environ.get('RETENTION_DAYS', '90'))


async def run(dry_run: bool) -> None:
    cutoff = int(time.time()) - DAYS * 86400
    async with get_async_db_context() as session:
        rows = (await session.execute(select(Chat.id).where(Chat.updated_at < cutoff))).scalars().all()
    deleted = 0
    if not dry_run:
        for chat_id in rows:
            if await Chats.delete_chat_by_id(chat_id):
                deleted += 1
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'{stamp} retention {DAYS}d: {len(rows)} chat(s) older than cutoff; '
          f'{"dry run, nothing deleted" if dry_run else f"{deleted} deleted"}', flush=True)


if __name__ == '__main__':
    if '--loop' in sys.argv:
        while True:
            asyncio.run(run(dry_run=False))
            time.sleep(24 * 3600)
    else:
        asyncio.run(run(dry_run='--dry-run' in sys.argv))
