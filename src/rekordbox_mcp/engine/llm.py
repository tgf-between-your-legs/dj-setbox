"""Claude API access for the few runtime jobs that need language, routed by job to a model tier.

Deterministic code does all the music maths. Models are used only for:
  bulk       Haiku 5.5  - name/metadata clean-up, tracklist line matching (chunked, Batch API capable)
  structure  Sonnet 5.5 - plain English -> validated constraints JSON; "why" text
  critique   Opus 5.5   - a full-set critique on request

Override any tier with RBMCP_MODEL_BULK / RBMCP_MODEL_STRUCTURE / RBMCP_MODEL_CRITIQUE.
The key comes from ANTHROPIC_API_KEY (never stored in the repo or the engine DB).
Every function accepts `client=` so tests can pass a fake.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

MODELS = {
    "bulk": "claude-haiku-5-5",
    "structure": "claude-sonnet-5-5",
    "critique": "claude-opus-5-5",
}
HAIKU_CHUNK = 150   # items per bulk request; keeps prompts far below Haiku's 100K-token price step


def model_for(job: str) -> str:
    return os.environ.get(f"RBMCP_MODEL_{job.upper()}") or MODELS[job]


def client():
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pip install anthropic to use the language features") from e
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set; language features are optional and everything else works without it")
    return anthropic.Anthropic()


def tool_call(system: str, user: str, tool_name: str, schema: dict, job: str = "structure",
              client_: Any = None, max_tokens: int = 8192, retries: int = 1) -> dict:
    """Ask for exactly one call of `tool_name`; returns its input (schema-shaped by the model).

    Current models reject a forced tool_choice ("tool"/"any") when thinking is on, so this uses
    "auto" with a firm instruction and retries once if the model answers in text instead.
    """
    c = client_ or client()
    sys_prompt = f"{system}\n\nAnswer ONLY by calling the {tool_name} tool exactly once. Do not reply in text."
    messages = [{"role": "user", "content": user}]
    for _ in range(retries + 1):
        msg = c.messages.create(
            model=model_for(job), max_tokens=max_tokens, system=sys_prompt,
            tools=[{"name": tool_name, "description": f"Return the {tool_name} result.", "input_schema": schema}],
            tool_choice={"type": "auto"},
            messages=messages,
        )
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", tool_name) == tool_name:
                return dict(block.input)
        messages = messages + [{"role": "assistant", "content": msg.content},
                               {"role": "user", "content": f"Call the {tool_name} tool now with your answer."}]
    raise RuntimeError(f"model did not call {tool_name}")


def text(system: str, user: str, job: str = "critique", client_: Any = None, max_tokens: int = 4096) -> str:
    c = client_ or client()
    msg = c.messages.create(model=model_for(job), max_tokens=max_tokens, system=system,
                            messages=[{"role": "user", "content": user}])
    return "".join(getattr(b, "text", "") for b in msg.content)


def chunks(items: list, n: int = HAIKU_CHUNK):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ----------------------------------------------------------------------------- batch API (bulk jobs)
def batch_submit(requests: list[dict], client_: Any = None) -> str:
    """requests: [{custom_id, system, user, tool_name, schema}] -> batch id (50% cheaper, async)."""
    c = client_ or client()
    reqs = [{"custom_id": r["custom_id"], "params": {
        "model": model_for("bulk"), "max_tokens": 8192,
        "system": r["system"] + f"\n\nAnswer ONLY by calling the {r['tool_name']} tool exactly once.",
        "tools": [{"name": r["tool_name"], "description": "Return the result.", "input_schema": r["schema"]}],
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": r["user"]}]}} for r in requests]
    return c.messages.batches.create(requests=reqs).id


def batch_results(batch_id: str, client_: Any = None) -> Optional[dict[str, dict]]:
    """None while the batch is still running; else {custom_id: tool input}."""
    c = client_ or client()
    b = c.messages.batches.retrieve(batch_id)
    if b.processing_status != "ended":
        return None
    out = {}
    for r in c.messages.batches.results(batch_id):
        if r.result.type == "succeeded":
            for block in r.result.message.content:
                if getattr(block, "type", None) == "tool_use":
                    out[r.custom_id] = dict(block.input)
    return out


# ----------------------------------------------------------------------------- jobs
NAMES_SCHEMA = {
    "type": "object",
    "properties": {"tracks": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "artist": {"type": "string"},
                       "genre": {"type": "string"}, "language": {"type": "string"}},
        "required": ["id", "title", "artist"]}}},
    "required": ["tracks"],
}
NAMES_SYSTEM = ("You clean DJ library metadata. For each track return a short display title (drop 'feat.' lists, "
                "'(Official Audio)', upload junk, label names) and the main artist(s). Keep remix/edit names that "
                "change the song, shortened. Guess a broad genre (k-pop, house, dnb, trap, hip-hop, pop, edm, other) "
                "and the main sung language code. Never invent artists you can't see in the input.")


def clean_names(tracks: list[dict], client_: Any = None) -> list[dict]:
    """tracks: [{id, title, artist, genre?}] -> cleaned rows (Haiku, chunked)."""
    out = []
    for ch in chunks(tracks):
        r = tool_call(NAMES_SYSTEM, json.dumps(ch, ensure_ascii=False), "clean_names", NAMES_SCHEMA,
                      job="bulk", client_=client_)
        out += r.get("tracks", [])
    return out


def critique_plan(summary: str, client_: Any = None) -> str:
    return text("You are an experienced club DJ reviewing a planned set. Be specific: name transition numbers, "
                "say what will sound wrong on a dance floor and what to change. Short bullets.",
                summary, job="critique", client_=client_)
