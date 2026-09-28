"""CLI. Exit codes: 0 report written · 2 input could not be trusted · 3 the matcher proposed a
finding its own checker rejected (the report is not trustworthy) · 4 extraction ran but some
messages produced no usable model output (the report covers fewer messages than the transcript)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .check import check
from .graph import build_graph, graph_to_json, render_graph
from .extract import Extraction, backend_from_spec, claim_to_json, extract
from .match import propose
from .model import InputError, finding_to_json, load_claims, load_events, load_findings


def render(verdicts, unchecked, events_path: str, ex: Extraction | None = None, graph_lines=None) -> str:
    acc = [v for v in verdicts if v.accepted]
    rej = [v for v in verdicts if not v.accepted]
    out = [f"# Said vs Did — {events_path}", "",
           f"{len(acc)} accepted · {len(rej)} rejected · {len(unchecked)} not checked", ""]
    if graph_lines:
        out += graph_lines + [""]
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
    if ex is not None:
        out += ["", "## Extraction",
                f"{ex.messages} messages · {len(ex.claims)} claims kept · {len(ex.rejected)} refused · "
                f"{len(ex.failed)} messages failed", "", "### Claims refused by the validator"]
        out += [f"- `{r.event}` — {r.reason}: {r.item}" for r in ex.rejected] or ["(none)"]
        out += ["", "### Messages with no usable model output"]
        out += [f"- `{e}` — {why}" for e, why in ex.failed] or ["(none)"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="saidvsdid")
    ap.add_argument("events", help="normalized events.jsonl")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--claims", help="claims.jsonl, hand-written or saved from an earlier --extract run")
    src.add_argument("--extract", metavar="BACKEND",
                     help="extract claims with a model: ollama:<model> or anthropic[:<model>]")
    ap.add_argument("--save-claims", metavar="PATH", help="with --extract: write the kept claims here")
    ap.add_argument("--findings", help="check these findings instead of proposing (e.g. a model's output)")
    ap.add_argument("--json", action="store_true", help="print machine-readable verdicts")
    ap.add_argument("--graph", action="store_true",
                    help="print only the interaction graph (who addresses whom); needs no claims")
    a = ap.parse_args(argv)
    if a.graph and (a.claims or a.extract or a.findings or a.save_claims):
        ap.error("--graph takes only the events file")
    if not (a.graph or a.claims or a.extract or a.findings):
        ap.error("give --claims, --extract, --findings, or a claims source plus --findings")
    if a.save_claims and not a.extract:
        ap.error("--save-claims needs --extract")

    try:
        tr = load_events(Path(a.events))
        if a.graph:
            g = build_graph(tr)
            if a.json:
                print(json.dumps(graph_to_json(g), indent=2))
            else:
                sys.stdout.write("\n".join([f"# Said vs Did — {a.events}", ""] + render_graph(g)) + "\n")
            return 0
        ex = None
        claims = load_claims(Path(a.claims)) if a.claims else []
        if a.extract:
            ex = extract(tr, backend_from_spec(a.extract))
            claims = ex.claims
            if a.save_claims:
                Path(a.save_claims).write_text("".join(json.dumps(claim_to_json(c), ensure_ascii=False) + "\n"
                                                       for c in claims), encoding="utf-8")
        proposed, unchecked = propose(tr, claims) if (a.claims or a.extract) else ([], [])
        given = load_findings(Path(a.findings)) if a.findings else []
    except (InputError, OSError, ValueError, RuntimeError) as exc:
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
        sys.stdout.write(render(verdicts, unchecked, a.events, ex, render_graph(build_graph(tr))))

    bad = [v for v in own if not v.accepted]
    if bad:
        print(f"saidvsdid: {len(bad)} finding(s) proposed by the matcher were rejected by the checker; "
              "the report cannot be trusted", file=sys.stderr)
        return 3
    if ex is not None and ex.failed:
        print(f"saidvsdid: {len(ex.failed)} of {ex.messages} message(s) produced no usable model output; "
              "the report covers only the rest", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
