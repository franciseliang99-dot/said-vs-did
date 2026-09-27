"""Normalized records: events, claims, findings. Loading is strict and never silent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EVENT_KINDS = ("message", "action", "observation")
CLAIM_TYPES = ("done", "doing", "will_do", "assign")
FINDING_TYPES = ("claimed_not_done", "done_not_said", "off_assignment")


class InputError(Exception):
    """The input cannot be trusted as a whole. Carries every problem, not just the first."""

    def __init__(self, source: str, problems: list[str]):
        self.source = source
        self.problems = problems
        super().__init__(f"{source}: {len(problems)} problem(s): " + "; ".join(problems[:5]))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Event:
    id: str
    t: datetime
    agent: str
    kind: str
    text: str
    to: tuple[str, ...] = ()
    tool: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class Claim:
    event: str
    agent: str
    type: str
    quote: str
    verb: str | None
    target: str | None
    assignee: str | None = None


@dataclass(frozen=True)
class Cite:
    event: str
    quote: str


@dataclass(frozen=True)
class Finding:
    type: str
    agent: str
    cites: tuple[Cite, ...]
    why: str
    window: tuple[str, str] | None = None
    searched: tuple[str, ...] = ()
    about: tuple[str, str] | None = None  # (verb, target) the finding is about; required for the two claim types


@dataclass
class Transcript:
    events: list[Event]
    by_id: dict[str, Event] = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {e.id: e for e in self.events}

    @property
    def start(self) -> datetime:
        return self.events[0].t

    @property
    def end(self) -> datetime:
        return self.events[-1].t


def _read_jsonl(path: Path) -> list[tuple[int, dict]]:
    rows: list[tuple[int, dict]] = []
    problems: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {n}: not JSON ({exc.msg})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {n}: not an object")
            continue
        rows.append((n, obj))
    if problems:
        raise InputError(str(path), problems)
    return rows


def load_events(path: Path) -> Transcript:
    """Identity adapter for already-normalized events. Zero events from any file is an error."""
    problems: list[str] = []
    events: list[Event] = []
    seen: set[str] = set()
    for n, o in _read_jsonl(path):
        missing = [k for k in ("id", "t", "agent", "kind", "text") if not isinstance(o.get(k), str)]
        if missing:
            problems.append(f"line {n}: missing/non-string {missing}")
            continue
        if o["kind"] not in EVENT_KINDS:
            problems.append(f"line {n}: unknown kind {o['kind']!r}")
            continue
        if o["id"] in seen:
            problems.append(f"line {n}: duplicate id {o['id']!r}")
            continue
        if o["kind"] == "action" and not (isinstance(o.get("tool"), str) and isinstance(o.get("target"), str)):
            problems.append(f"line {n}: action without tool/target")
            continue
        try:
            t = parse_time(o["t"])
        except ValueError:
            problems.append(f"line {n}: bad timestamp {o['t']!r}")
            continue
        seen.add(o["id"])
        events.append(Event(o["id"], t, o["agent"], o["kind"], o["text"], tuple(o.get("to") or ()),
                            o.get("tool"), o.get("target")))
    if not events and not problems:
        problems.append("no events")
    if problems:
        raise InputError(str(path), problems)
    if any(b.t < a.t for a, b in zip(events, events[1:])):
        raise InputError(str(path), ["events are not in time order"])
    return Transcript(events)


def load_claims(path: Path) -> list[Claim]:
    problems: list[str] = []
    claims: list[Claim] = []
    for n, o in _read_jsonl(path):
        if o.get("type") not in CLAIM_TYPES:
            problems.append(f"line {n}: unknown claim type {o.get('type')!r}")
            continue
        missing = [k for k in ("event", "agent", "quote") if not isinstance(o.get(k), str)]
        if missing:
            problems.append(f"line {n}: missing {missing}")
            continue
        about = o.get("about") or {}
        claims.append(Claim(o["event"], o["agent"], o["type"], o["quote"],
                            about.get("verb"), about.get("target"), o.get("assignee")))
    if problems:
        raise InputError(str(path), problems)
    return claims


def load_findings(path: Path) -> list[Finding]:
    problems: list[str] = []
    out: list[Finding] = []
    for n, o in _read_jsonl(path):
        try:
            cites = tuple(Cite(c["event"], c["quote"]) for c in o["cites"])
            window = tuple(o["window"]) if o.get("window") else None
            about = (o["about"]["verb"], o["about"]["target"]) if o.get("about") else None
            out.append(Finding(o["type"], o["agent"], cites, o.get("why", ""), window,
                               tuple(o.get("searched") or ()), about))
        except (KeyError, TypeError) as exc:
            problems.append(f"line {n}: malformed finding ({exc!r})")
    if problems:
        raise InputError(str(path), problems)
    return out


def finding_to_json(f: Finding) -> dict:
    d: dict = {"type": f.type, "agent": f.agent,
               "cites": [{"event": c.event, "quote": c.quote} for c in f.cites], "why": f.why}
    if f.window:
        d["window"] = list(f.window)
    if f.searched:
        d["searched"] = list(f.searched)
    if f.about:
        d["about"] = {"verb": f.about[0], "target": f.about[1]}
    return d
