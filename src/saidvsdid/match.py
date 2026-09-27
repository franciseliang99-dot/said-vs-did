"""Propose findings from events + claims. Proposals are not trusted: check.py decides."""

from __future__ import annotations

from dataclasses import dataclass

from .model import Cite, Claim, Finding, Transcript
from .rules import VERB_TOOLS, action_matches, actions_in, is_destructive, mentions, messages_by


@dataclass(frozen=True)
class Unchecked:
    event: str
    reason: str


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
            window = (tr.start, src.t)
            acts = actions_in(tr, c.agent, *window)
            if not any(action_matches(a, c.agent, c.verb, c.target) for a in acts):
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
            window = (src.t, tr.end)
            acts = actions_in(tr, c.assignee, *window)
            if not acts:
                unchecked.append(Unchecked(c.event, f"{c.assignee} took no actions after the assignment"))
                continue
            if not any(action_matches(a, c.assignee, c.verb, c.target) for a in acts):
                findings.append(Finding(
                    "off_assignment", c.assignee,
                    (Cite(c.event, c.quote),) + tuple(Cite(a.id, a.text) for a in acts),
                    f"assigned '{c.verb} {c.target}'; none of its {len(acts)} later actions touch "
                    f"'{c.target}'",
                    (_iso(window[0]), _iso(window[1])), (), (c.verb, c.target)))

    for a in tr.events:
        if not is_destructive(a):
            continue
        if not any(mentions(m, a.target or "") for m in messages_by(tr, a.agent)):
            findings.append(Finding(
                "done_not_said", a.agent, (Cite(a.id, a.text),),
                f"destructive action on '{a.target}' never mentioned by {a.agent}"))

    return findings, unchecked
