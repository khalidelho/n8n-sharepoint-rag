"""
title: Knowledge Assistant
description: Sends each message to the private RAG agent in n8n and returns its answer.
"""

import json
import re

import requests
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        n8n_url: str = Field(
            default="http://host.docker.internal:5680/webhook/rag-chat",
            description="The agent's webhook (host.docker.internal = the Mac, as seen from this container)",
        )
        n8n_token: str = Field(
            default="", description="RAG_WEBHOOK_TOKEN from stack/n8n/.env"
        )
        timeout_seconds: int = Field(default=180)

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "knowledge-assistant", "name": "Knowledge Assistant"}]

    def pipe(
        self,
        body: dict,
        __user__: dict = None,
        __metadata__: dict = None,
        __task__: str = None,
    ):
        messages = body.get("messages", [])

        # Open WebUI also asks the selected model to write chat titles, tags and follow-up suggestions.
        # Those must NOT reach the agent: each would run a full search and be stored in the chat's memory.
        if __task__:
            prompt = str(messages[-1].get("content", "")) if messages else ""
            if __task__ == "title_generation":
                first = re.search(r"USER:\s*(.+)", prompt)
                title = (
                    first.group(1) if first else "Knowledge Assistant chat"
                ).strip()
                return json.dumps({"title": title[:60]})
            if __task__ == "follow_up_generation":
                return json.dumps({"follow_ups": []})
            if __task__ == "tags_generation":
                return json.dumps({"tags": []})
            return ""

        # A real question: the newest message; earlier context lives in n8n's memory, keyed by chat
        content = messages[-1].get("content", "") if messages else ""
        if isinstance(content, list):  # messages with attachments arrive as parts
            content = " ".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )

        # "<person>:<chat>" — one memory per chat, and every chat traceable to its person for erasure requests
        user_id = (__user__ or {}).get("id", "unknown-user")
        session_id = f"{user_id}:{(__metadata__ or {}).get('chat_id') or 'no-chat-id'}"

        try:
            r = requests.post(
                self.valves.n8n_url,
                json={"chatInput": content, "sessionId": session_id},
                headers={"Authorization": f"Bearer {self.valves.n8n_token}"},
                timeout=self.valves.timeout_seconds,
            )
            if r.status_code == 403:
                return "The knowledge assistant rejected the request — check the token in this function's settings."
            r.raise_for_status()
            return r.json().get("output") or "The assistant returned an empty answer."
        except Exception as e:
            return f"Sorry — the knowledge assistant couldn't be reached right now ({type(e).__name__})."

