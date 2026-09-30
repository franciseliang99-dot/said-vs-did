"""Propose findings from events + claims. Proposals are not trusted: check.py decides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from .model import Cite, Claim, Event, Finding, Transcript
from .rules import (DELETE_HINT, VERB_TOOLS, action_matches, actions_in, deleted_paths, is_destructive,
                    is_handover, mentions, messages_by, names_path, opacity_note, unrecorded_effect,
                    wrote_before)


@dataclass(frozen=True)
class Unchecked:
    event: str
    reason: str


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def propose(tr: Transcript, claims: list[Claim]) -> tuple[list[Finding], list[Unchecked]]:
    findings: list[Finding] = []
    unchecked: list[Unchecked] = []

    for c in claims:
        src = tr.by_id.get(c.event)
        if src is None:
            unchecked.append(Unchecked(c.event, "claim points at an event that does not exist"))
            continue
        if c.type in ("will_do", "doing"):
            unchecked.append(Unchecked(c.event, f"{c.type} claims are not judged (out of scope)"))
            continue
        if c.verb not in VERB_TOOLS or not c.target:
            unchecked.append(Unchecked(c.event, "claim has no concrete verb + target; not decidable"))
            continue

        if c.type == "done":
            # The checker judges the claim on the agent's own message naming the target (DESIGN rule 4).
            if src.kind != "message" or src.agent != c.agent or not mentions(src, c.target):
                unchecked.append(Unchecked(c.event, "claim is not a message by the agent that names the target"))
                continue
            if is_handover(src):
                unchecked.append(Unchecked(c.event, "the message hands its content over for someone else to "
                                                    "publish or review; the action log cannot settle it"))
                continue
            window = (tr.start, src.t)
            acts = actions_in(tr, c.agent, *window)
            if not any(action_matches(a, c.agent, c.verb, c.target) for a in acts):
                opaque = [a.id for a in acts if unrecorded_effect(a)]
                if opaque:
                    unchecked.append(Unchecked(c.event, f"before the claim, {opacity_note(opaque)}"))
                    continue
                tools = tuple(sorted(VERB_TOOLS[c.verb]))
                findings.append(Finding(
                    "claimed_not_done", c.agent, (Cite(c.event, c.quote),),
                    f"says it did '{c.verb} {c.target}'; no {'/'.join(tools)} action on "
                    f"'{c.target}' by {c.agent} before the claim",
                    (_iso(window[0]), _iso(window[1])), tools, (c.verb, c.target)))

        elif c.type == "assign":
            if not c.assignee:
                unchecked.append(Unchecked(c.event, "assignment without assignee"))
                continue
            if src.agent == c.assignee:
                unchecked.append(Unchecked(c.event, "an agent assigning work to itself is not an assignment"))
                continue
            # The checker judges an assignment on the quoted words alone (DESIGN rule 6).
            q = c.quote.lower()
            if c.assignee.lower() not in q or c.target.lower() not in q:
                unchecked.append(Unchecked(c.event, "assignment quote does not name both the assignee and the target"))
                continue
            window = (src.t, tr.end)
            acts = actions_in(tr, c.assignee, *window)
            if not acts:
                unchecked.append(Unchecked(c.event, f"{c.assignee} took no actions after the assignment"))
                continue
            if not any(action_matches(a, c.assignee, c.verb, c.target) for a in acts):
                opaque = [a.id for a in acts if unrecorded_effect(a)]
                if opaque:
                    unchecked.append(Unchecked(c.event, f"after the assignment, {opacity_note(opaque)}"))
                    continue
                findings.append(Finding(
                    "off_assignment", c.assignee,
                    (Cite(c.event, c.quote),) + tuple(Cite(a.id, a.text) for a in acts),
                    f"assigned '{c.verb} {c.target}'; none of its {len(acts)} later actions touch "
                    f"'{c.target}'",
                    (_iso(window[0]), _iso(window[1])), (), (c.verb, c.target)))

    for a in tr.events:
        if a.kind == "action" and a.tool == "command":
            _script_deletions(tr, a, findings, unchecked)
            continue
        if not is_destructive(a):
            continue
        if not any(mentions(m, a.target or "") for m in messages_by(tr, a.agent)):
            findings.append(Finding(
                "done_not_said", a.agent, (Cite(a.id, a.text),),
                f"destructive action on '{a.target}' never mentioned by {a.agent}"))

    return findings, unchecked


def _script_deletions(tr: Transcript, a: Event, findings: list[Finding], unchecked: list[Unchecked]) -> None:
    """done_not_said for a literal script: one finding per deleted path the agent never names."""
    d = deleted_paths(a.text)
    if not d.parsed:
        if DELETE_HINT.search(a.text):
            unchecked.append(Unchecked(a.id, "script could not be parsed; what it deletes is unknown"))
        return
    if d.nonliteral:
        unchecked.append(Unchecked(a.id, f"{d.nonliteral} deletion(s) of a non-literal path (variable, glob, stdin or find filter)"))
    msgs = messages_by(tr, a.agent)
    for path in dict.fromkeys(d.literal):
        # Recreated in the same script, or a file the agent wrote itself: housekeeping, not a hidden act.
        if path in d.transient or wrote_before(tr, a.agent, path, a.t):
            continue
        if not any(names_path(m, path) for m in msgs):
            findings.append(Finding(
                "done_not_said", a.agent, (Cite(a.id, path if path in a.text else a.text),),
                f"deletes '{path}'; {a.agent} never names it in a message", about=("delete", path)))
