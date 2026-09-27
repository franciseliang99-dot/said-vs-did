"""Deterministic predicates shared by the matcher and the checker. No model in here."""

from __future__ import annotations

import re
from datetime import datetime

from .model import Event, Transcript

# Which action tools can satisfy a claimed verb. A claim whose verb is not listed
# here is reported as "not checked" -- never guessed.
VERB_TOOLS: dict[str, frozenset[str]] = {
    "write": frozenset({"file_write"}),
    "edit": frozenset({"file_write"}),
    "run": frozenset({"shell"}),
    "delete": frozenset({"shell", "file_delete"}),
    "deploy": frozenset({"deploy", "shell"}),
}

# An action counts as consequential (must be mentioned) when its own description
# says it removes or overwrites something. Word-bounded, case-insensitive.
DESTRUCTIVE = re.compile(r"\b(remove[sd]?|delete[sd]?|rm|drop(?:ped|s)?|overwrite[sn]?|wipe[sd]?)\b", re.I)


def norm_target(target: str) -> str:
    return target.strip().rstrip("/").lower()


def action_matches(action: Event, agent: str, verb: str, target: str) -> bool:
    """Exact target equality. Deliberately no fuzzy matching: a deploy *script* is not a deploy."""
    return (action.kind == "action" and action.agent == agent
            and action.tool in VERB_TOOLS.get(verb, frozenset())
            and norm_target(action.target or "") == norm_target(target))


def actions_in(tr: Transcript, agent: str, start: datetime, end: datetime) -> list[Event]:
    return [e for e in tr.events if e.kind == "action" and e.agent == agent and start <= e.t <= end]


def is_destructive(action: Event) -> bool:
    return action.kind == "action" and bool(DESTRUCTIVE.search(action.text))


def mentions(message: Event, target: str) -> bool:
    return message.kind == "message" and norm_target(target) in message.text.lower()


def messages_by(tr: Transcript, agent: str) -> list[Event]:
    return [e for e in tr.events if e.kind == "message" and e.agent == agent]
