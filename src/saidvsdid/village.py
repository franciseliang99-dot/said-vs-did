"""Adapter for the AI Village dataset (AI Digest) -> normalized events.

Reads, from a directory holding the downloaded tables:
  agents.jsonl.gz, events.jsonl.gz, computer_use_sessions.jsonl.gz,
  and computer_use_turns.jsonl.gz or turns.slim.jsonl.gz (same columns, minus agent_messages).

Mapping (column names per the dataset's SCHEMA.md):
  message      <- events where data.actionType == AGENT_TALK; agent = agents.name of data.speakerId
  action       <- computer_use_turns.agent_action (a shell command -> tool "command": text is the
                  literal script); agent via session_id -> computer_use_sessions.agent_id
  observation  <- computer_use_turns.output / error
Human messages (USER_TALK) are never emitted, only counted: the dataset terms rule out quoting them.

Contract (DESIGN.md, "Adapter contract"): deterministic order and ids; every event carries src;
nothing is dropped silently -- every skipped row is counted by reason, unparseable lines are
listed by number and make the run fail; `to` is filled only from an exact "@<agent name>" in the text.

Usage: python -m saidvsdid.village DATA_DIR --since T --until T -o events.jsonl
Exit codes: 0 written · 2 input could not be trusted (nothing written).
Prints counts only, never message text.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import InputError

# Computer-use actions that change something. Everything else (looking, waiting, scrolling,
# talking through the chat tool) is skipped and counted under its own name.
GUI_ACTIONS = frozenset({"left_click", "right_click", "middle_click", "double_click", "triple_click",
                         "left_click_drag", "type", "key"})
TURN_FILES = ("computer_use_turns.jsonl.gz", "turns.slim.jsonl.gz")
KIND_RANK = {"message": 0, "action": 1, "observation": 2}


def village_time(value: str) -> str:
    """'2025-12-29 18:49:21.291984' (UTC, no suffix) -> '2025-12-29T18:49:21.291984Z'.

    Always six fractional digits, so the string sorts and compares like the time it names
    (isoformat() drops '.000000', which would put '...21Z' after '...21.5Z')."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        raise ValueError(f"expected a naive UTC timestamp, got {value!r}")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def mentioned(text: str, names_longest_first: list[str]) -> list[str]:
    """Agent names written as '@<name>', exact and case-sensitive, in order of appearance.
    Longer names are tried first so a shorter name cannot claim part of a longer one.

    '@Claude Opus 4.5' must not also count as '@Claude Opus 4': a match needs a boundary after
    the name (end, or a character that cannot continue it), and matched spans are consumed."""
    taken: list[tuple[int, int]] = []
    hits: list[tuple[int, str]] = []
    for name in names_longest_first:
        needle = "@" + name
        start = 0
        while (i := text.find(needle, start)) != -1:
            end = i + len(needle)
            start = i + 1
            nxt = text[end:end + 2]
            if nxt[:1].isalnum() or nxt[:1] == "_" or (nxt[:1] == "." and nxt[1:2].isdigit()):
                continue
            if any(a < end and i < b for a, b in taken):
                continue
            taken.append((i, end))
            hits.append((i, name))
    found: list[str] = []
    for _, name in sorted(hits):  # in the order they appear in the text
        if name not in found:
            found.append(name)
    return found


@dataclass
class Report:
    rows_read: Counter = field(default_factory=Counter)
    emitted: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)


def _rows(path: Path, report: Report):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            report.rows_read[path.name] += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                report.problems.append(f"{path.name} line {n}: not JSON ({exc.msg})")
                continue
            if not isinstance(obj, dict):
                report.problems.append(f"{path.name} line {n}: not an object")
                continue
            yield n, obj


def _in_window(t: str, since: str, until: str) -> bool:
    return since <= t < until


def convert(data: Path, since: datetime, until: datetime) -> tuple[list[dict], Report]:
    if since.tzinfo is None or until.tzinfo is None or not since < until:
        raise ValueError("--since/--until must be timezone-aware and since < until")
    lo = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    hi = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rep = Report()

    names: dict[str, str] = {}
    for n, o in _rows(data / "agents.jsonl.gz", rep):
        if not (isinstance(o.get("id"), str) and isinstance(o.get("name"), str)):
            rep.problems.append(f"agents line {n}: missing id/name")
            continue
        names[o["id"]] = o["name"]
    dupes = [k for k, v in Counter(names.values()).items() if v > 1]
    if dupes:
        rep.problems.append(f"agents: duplicate display names {sorted(dupes)}")
    longest_first = sorted(set(names.values()), key=lambda s: (-len(s), s))

    session_agent: dict[str, str] = {}
    for n, o in _rows(data / "computer_use_sessions.jsonl.gz", rep):
        if isinstance(o.get("id"), str) and isinstance(o.get("agent_id"), str):
            session_agent[o["id"]] = o["agent_id"]
        else:
            rep.problems.append(f"computer_use_sessions line {n}: missing id/agent_id")

    out: list[dict] = []

    def emit(ev: dict) -> None:
        out.append(ev)
        rep.emitted[ev["kind"]] += 1

    for n, o in _rows(data / "events.jsonl.gz", rep):
        d = o.get("data") if isinstance(o.get("data"), dict) else {}
        kind = d.get("actionType")
        try:
            t = village_time(o["created_at"])
        except (KeyError, TypeError, ValueError):
            rep.problems.append(f"events.jsonl.gz line {n}: bad created_at")
            continue
        if not _in_window(t, lo, hi):
            continue
        if kind != "AGENT_TALK":
            rep.skipped[f"event {kind}" if kind != "USER_TALK" else "event USER_TALK (human, never emitted)"] += 1
            continue
        speaker, text = d.get("speakerId"), d.get("content")
        if speaker not in names or not isinstance(text, str) or not isinstance(o.get("id"), str):
            rep.problems.append(f"events.jsonl.gz line {n}: AGENT_TALK without a known speaker or content")
            continue
        emit({"id": o["id"], "t": t, "agent": names[speaker], "kind": "message",
              "to": [m for m in mentioned(text, longest_first) if m != names[speaker]], "text": text,
              "src": {"file": "events.jsonl.gz", "line": n}})

    turn_file = next((data / f for f in TURN_FILES if (data / f).exists()), None)
    if turn_file is None:
        rep.problems.append(f"no turns file ({' or '.join(TURN_FILES)}) in {data}")
    else:
        for n, o in _rows(turn_file, rep):
            try:
                t = village_time(o["created_at"])
            except (KeyError, TypeError, ValueError):
                rep.problems.append(f"{turn_file.name} line {n}: bad created_at")
                continue
            if not _in_window(t, lo, hi):
                continue
            agent_id = session_agent.get(o.get("session_id"))
            if agent_id not in names or not isinstance(o.get("id"), str):
                rep.problems.append(f"{turn_file.name} line {n}: turn without a resolvable agent or id")
                continue
            agent, src = names[agent_id], {"file": turn_file.name, "line": n}
            act = o.get("agent_action")
            if isinstance(act, dict) and isinstance(act.get("command"), str):
                cmd = act["command"].strip()
                emit({"id": o["id"], "t": t, "agent": agent, "kind": "action", "to": [], "text": cmd,
                      "tool": "command", "target": cmd, "src": src})
            elif isinstance(act, dict) and act.get("action") in GUI_ACTIONS:
                what = act.get("text") if isinstance(act.get("text"), str) else ""
                desc = f"{act['action']} {what}".strip()
                emit({"id": o["id"], "t": t, "agent": agent, "kind": "action", "to": [], "text": desc,
                      "tool": "gui", "target": what, "src": src})
            elif act is None:
                rep.skipped["turn with no action"] += 1
            elif isinstance(act, dict) and isinstance(act.get("action"), str):
                rep.skipped[f"turn {act['action']} (no effect)"] += 1
            # The three shapes below were found by a census of the whole turns table (2026-09-28);
            # anything else still fails the run.
            elif isinstance(act, dict) and act.get("restart") is True and set(act) <= {"restart", "command"} \
                    and act.get("command") is None:
                rep.skipped["turn shell restart (no effect)"] += 1
            elif act == {}:
                rep.skipped["turn empty action"] += 1
            elif isinstance(act, dict) and set(act) == {"coordinate", "text"}:
                rep.skipped["turn action without a name (not decidable)"] += 1
            else:
                rep.problems.append(f"{turn_file.name} line {n}: unrecognized agent_action shape")
                continue
            for key in ("output", "error"):
                val = o.get(key)
                if isinstance(val, str) and val.strip():
                    emit({"id": f"{o['id']}:{key}", "t": t, "agent": agent, "kind": "observation",
                          "to": [], "text": val, "src": src})

    out.sort(key=lambda e: (e["t"], KIND_RANK[e["kind"]], e["id"]))
    if not out and not rep.problems:
        rep.problems.append("no events in the window (an empty result is an error, not an answer)")
    return out, rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="saidvsdid.village")
    ap.add_argument("data", help="directory with the downloaded dataset tables")
    ap.add_argument("--since", required=True, help="ISO time with zone, inclusive, e.g. 2026-09-01T00:00:00Z")
    ap.add_argument("--until", required=True, help="ISO time with zone, exclusive")
    ap.add_argument("-o", "--out", required=True, help="normalized events.jsonl to write")
    a = ap.parse_args(argv)
    try:
        since = datetime.fromisoformat(a.since.replace("Z", "+00:00"))
        until = datetime.fromisoformat(a.until.replace("Z", "+00:00"))
        events, rep = convert(Path(a.data), since, until)
        if rep.problems:
            raise InputError(a.data, rep.problems)
    except (InputError, OSError, ValueError) as exc:
        print(f"saidvsdid.village: input error: {exc}", file=sys.stderr)
        for p in getattr(exc, "problems", [])[:50]:
            print(f"  - {p}", file=sys.stderr)
        return 2
    with open(a.out, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"wrote {len(events)} events to {a.out}")
    print("emitted: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.emitted.items())))
    print("rows read: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.rows_read.items())))
    print("skipped in window:")
    for k, v in sorted(rep.skipped.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
