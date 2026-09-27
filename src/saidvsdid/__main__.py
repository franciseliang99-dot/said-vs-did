"""CLI. Exit codes: 0 report written · 2 input could not be trusted · 3 the matcher proposed a
finding its own checker rejected (the report is not trustworthy)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .check import check
from .match import propose
from .model import InputError, finding_to_json, load_claims, load_events, load_findings


def render(verdicts, unchecked, events_path: str) -> str:
    acc = [v for v in verdicts if v.accepted]
    rej = [v for v in verdicts if not v.accepted]
    out = [f"# Said vs Did — {events_path}", "",
           f"{len(acc)} accepted · {len(rej)} rejected · {len(unchecked)} not checked", ""]
    out.append("## Findings")
    if not acc:
        out.append("(none)")
    for v in acc:
        f = v.finding
        out.append(f"- **{f.type}** · {f.agent} — {f.why}")
        for c in f.cites:
            out.append(f"  - `{c.event}` “{c.quote}”")
    out += ["", "## Rejected by the checker"]
    if not rej:
        out.append("(none)")
    for v in rej:
        f = v.finding
        out.append(f"- {f.type} · {f.agent} · cites {[c.event for c in f.cites]}")
        for reason in v.reasons:
            out.append(f"  - {reason}")
    out += ["", "## Not checked"]
    if not unchecked:
        out.append("(none)")
    for u in unchecked:
        out.append(f"- `{u.event}` — {u.reason}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="saidvsdid")
    ap.add_argument("events", help="normalized events.jsonl")
    ap.add_argument("--claims", help="claims.jsonl (from the extract stage)")
    ap.add_argument("--findings", help="check these findings instead of proposing (e.g. a model's output)")
    ap.add_argument("--json", action="store_true", help="print machine-readable verdicts")
    a = ap.parse_args(argv)
    if not a.claims and not a.findings:
        ap.error("give --claims, --findings, or both")

    try:
        tr = load_events(Path(a.events))
        proposed, unchecked = propose(tr, load_claims(Path(a.claims))) if a.claims else ([], [])
        given = load_findings(Path(a.findings)) if a.findings else []
    except (InputError, OSError) as exc:
        print(f"saidvsdid: input error: {exc}", file=sys.stderr)
        for p in getattr(exc, "problems", []):
            print(f"  - {p}", file=sys.stderr)
        return 2

    own = [check(tr, f) for f in proposed]
    ext = [check(tr, f) for f in given]
    verdicts = own + ext
    if a.json:
        print(json.dumps([{"finding": finding_to_json(v.finding), "accepted": v.accepted,
                           "reasons": list(v.reasons)} for v in verdicts], indent=2))
    else:
        sys.stdout.write(render(verdicts, unchecked, a.events))

    bad = [v for v in own if not v.accepted]
    if bad:
        print(f"saidvsdid: {len(bad)} finding(s) proposed by the matcher were rejected by the checker; "
              "the report cannot be trusted", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
