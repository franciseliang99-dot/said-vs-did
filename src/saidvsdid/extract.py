"""Extract claims from messages with a model, then validate every one of them in code.

The model only proposes. Code fills in which event and which agent a claim belongs to, and a
claim reaches the matcher only if the deterministic rules in ``validate`` hold. Anything the
model got wrong is listed, never silently dropped.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .model import CLAIM_TYPES, Claim, Event, Transcript
from .prompt import EXTRACT_PROMPT
from .rules import VERB_TOOLS, norm_target

Backend = Callable[[str], str]  # rendered prompt -> raw model text

FIELDS = ("type", "quote", "verb", "target", "assignee")


@dataclass(frozen=True)
class Rejected:
    event: str
    item: str  # the model's item, re-serialized, so the report shows exactly what was refused
    reason: str


@dataclass
class Extraction:
    claims: list[Claim] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (event, why the call produced nothing usable)
    messages: int = 0


def render_prompt(ev: Event, agents: list[str]) -> str:
    return EXTRACT_PROMPT.format(speaker=ev.agent, agents=", ".join(agents), message=ev.text)


def parse_items(raw: str) -> list:
    """The reply must be a JSON array. One surrounding code fence is tolerated; nothing else is."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    items = json.loads(text)
    if not isinstance(items, list):
        raise ValueError(f"expected a JSON array, got {type(items).__name__}")
    return items


_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'll|I'd|I’m|I’ve|I’ll|I’d|me|my|we|we're|we've|we'll|"
                           r"we’re|we’ve|we’ll|us)\b", re.IGNORECASE)
_THIRD_PERSON_START = re.compile(r"^\W*(?:the|this|that|these|those|it|its|there|they|their|he|she|his|her|"
                                 r"a|an)\b", re.IGNORECASE)
_FUTURE = re.compile(r"\b(?:will|I'll|I’ll|we'll|we’ll|going to|about to)\b", re.IGNORECASE)


def _not_the_speakers_act(t: str, quote: str, speaker: str, agents: list[str]) -> str:
    """A small model reads a status report ("Claude X has completed the checklist") as the speaker's
    own done claim, and the matcher then finds no action by the speaker. On a 1191-message day that
    produced 23 of 24 findings. A claim without a first-person word whose subject is another agent or
    a third-person noun phrase is not the speaker's; a done claim worded in the future is not done."""
    if t == "done" and _FUTURE.search(quote):
        return "done claim is worded in the future"
    if _FIRST_PERSON.search(quote):
        return ""
    for name in agents:
        if name != speaker and re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", quote):
            return f"quote names another agent ({name}) and not the speaker"
    if _THIRD_PERSON_START.search(quote):
        return "quote's subject is a third-person noun phrase, not the speaker"
    return ""


def validate(item, ev: Event, agents: list[str]) -> tuple[Claim | None, str]:
    if not isinstance(item, dict):
        return None, "item is not an object"
    missing = [k for k in FIELDS if k not in item]
    if missing:
        return None, f"missing field(s) {missing}"
    t, quote, verb, target, assignee = (item[k] for k in FIELDS)
    if t not in CLAIM_TYPES:
        return None, f"unknown type {t!r}"
    if not isinstance(quote, str) or not quote.strip():
        return None, "empty quote"
    if quote not in ev.text:
        return None, "quote is not a verbatim substring of the message"
    if verb is not None and verb not in VERB_TOOLS:
        return None, f"verb {verb!r} is not one of {sorted(VERB_TOOLS)} or null"
    if target is not None:
        if not isinstance(target, str) or not target.strip():
            return None, "target must be a non-empty string or null"
        # The target must be named inside the quoted claim itself. A target the message never
        # names is a guess, and one borrowed from another sentence pairs the claim with the
        # wrong object. Neither reaches the matcher.
        if norm_target(target) not in quote.lower():
            return None, f"target {target!r} does not appear in the quote"
    if t != "assign":
        why = _not_the_speakers_act(t, quote, ev.agent, agents)
        if why:
            return None, why
    if t == "assign":
        if assignee not in agents:
            return None, f"assignee {assignee!r} is not a known agent"
        if assignee == ev.agent:
            return None, "assignee is the speaker"
    elif assignee is not None:
        return None, f"assignee is only allowed on assign claims (got {assignee!r} on {t})"
    return Claim(ev.id, ev.agent, t, quote, verb, target, assignee), ""


def extract(tr: Transcript, backend: Backend, since: datetime | None = None) -> Extraction:
    """Messages before ``since`` are lookback: not extracted, but still visible to the checker."""
    agents = sorted({e.agent for e in tr.events})
    out = Extraction()
    for ev in tr.events:
        if ev.kind != "message" or (since is not None and ev.t < since):
            continue
        out.messages += 1
        try:
            items = parse_items(backend(render_prompt(ev, agents)))
        except (OSError, ValueError, RuntimeError) as exc:  # JSONDecodeError is a ValueError
            out.failed.append((ev.id, f"{type(exc).__name__}: {exc}"))
            continue
        for item in items:
            claim, why = validate(item, ev, agents)
            if claim:
                out.claims.append(claim)
            else:
                out.rejected.append(Rejected(ev.id, json.dumps(item, ensure_ascii=False), why))
    return out


def claim_to_json(c: Claim) -> dict:
    d = {"event": c.event, "agent": c.agent, "type": c.type, "quote": c.quote,
         "about": {"verb": c.verb, "target": c.target}}
    if c.assignee is not None:
        d["assignee"] = c.assignee
    return d


# --- backends ----------------------------------------------------------------------------------

def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}") from exc


def ollama(model: str, url: str | None = None, timeout: float = 120.0) -> Backend:
    base = (url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")

    def call(prompt: str) -> str:
        # think=false: a model that reasons first (qwen3.5) otherwise spends the whole timeout
        # before answering; models without that mode ignore it.
        r = _post(f"{base}/api/generate", {"model": model, "prompt": prompt, "stream": False, "think": False,
                                            "options": {"temperature": 0}}, {}, timeout)
        return r.get("response", "")
    return call


def anthropic(model: str = "claude-haiku-4-5-20251001", timeout: float = 60.0) -> Backend:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    def call(prompt: str) -> str:
        r = _post("https://api.anthropic.com/v1/messages",
                  {"model": model, "max_tokens": 1024, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]},
                  {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    return call


def backend_from_spec(spec: str) -> Backend:
    """``ollama:<model>`` or ``anthropic`` / ``anthropic:<model>``."""
    kind, _, model = spec.partition(":")
    if kind == "ollama" and model:
        return ollama(model)
    if kind == "anthropic":
        return anthropic(model) if model else anthropic()
    raise ValueError(f"unknown backend {spec!r}; use ollama:<model> or anthropic[:<model>]")
