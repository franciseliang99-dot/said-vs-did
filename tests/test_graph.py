"""Question 1: the interaction graph, pinned against the hand-made sample."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from saidvsdid.__main__ import main
from saidvsdid.graph import build_graph, graph_to_json
from saidvsdid.model import load_events

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EVENTS = SAMPLES / "tiny-village.jsonl"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


def write_events(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for i, r in enumerate(rows):
        f.write(json.dumps({"id": f"e{i}", "t": f"2026-10-03T00:00:{i:02d}Z", "kind": "message", **r}) + "\n")
    f.close()
    return Path(f.name)


class SampleGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = build_graph(load_events(EVENTS))

    def test_edges_and_their_citations(self):
        got = {(e.src, e.dst): e.events for e in self.g.edges}
        self.assertEqual(got, {
            ("agent-a", "agent-b"): ("tv-001", "tv-006"),
            ("agent-a", "agent-c"): ("tv-001", "tv-006", "tv-017"),
            ("agent-b", "agent-a"): ("tv-002", "tv-010", "tv-016"),
            ("agent-b", "agent-c"): ("tv-016",),
            ("agent-c", "agent-a"): ("tv-003", "tv-009", "tv-019"),
        })

    def test_broadcast_is_counted_not_spread(self):
        # tv-020 has an empty `to`. Turning it into edges to everyone would add agent-a -> agent-b
        # a third citation; the edge test above would catch that too.
        self.assertEqual(self.g.broadcasts, {"agent-a": ("tv-020",)})

    def test_pair_kinds(self):
        kinds = {(e["from"], e["to"]): e["kind"] for e in graph_to_json(self.g)["edges"]}
        self.assertEqual(kinds[("agent-a", "agent-b")], "mutual")
        self.assertEqual(kinds[("agent-b", "agent-c")], "one-way")

    def test_every_cited_event_is_a_message_by_the_source_naming_the_target(self):
        tr = load_events(EVENTS)
        for e in self.g.edges:
            for i in e.events:
                ev = tr.by_id[i]
                self.assertEqual((ev.kind, ev.agent), ("message", e.src))
                self.assertIn(e.dst, ev.to)

    def test_actions_never_make_edges(self):
        self.assertTrue(all(e.src != e.dst for e in self.g.edges))
        self.assertEqual(self.g.only_addressed, ())


class EdgeCases(unittest.TestCase):
    def test_unknown_addressee_and_duplicate_name(self):
        p = write_events([{"agent": "x", "text": "hi", "to": ["ghost", "ghost"]}])
        g = build_graph(load_events(p))
        self.assertEqual([(e.src, e.dst, e.events) for e in g.edges], [("x", "ghost", ("e0",))])
        self.assertEqual(g.only_addressed, ("ghost",))

    def test_self_address_is_kept_and_labelled(self):
        p = write_events([{"agent": "x", "text": "note to self", "to": ["x"]}])
        g = build_graph(load_events(p))
        self.assertEqual(graph_to_json(g)["edges"][0]["kind"], "self")


class Cli(unittest.TestCase):
    def test_graph_only_needs_no_claims(self):
        rc, out, err = run(EVENTS, "--graph")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("agent-b → agent-c · 1 message(s) · one-way — `tv-016`", out)
        self.assertIn("5 directed edges · 1 broadcast messages", out)

    def test_graph_json(self):
        rc, out, _ = run(EVENTS, "--graph", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)["edges"]), 5)

    def test_graph_refuses_claims(self):
        with self.assertRaises(SystemExit) as cm:
            run(EVENTS, "--graph", "--claims", SAMPLES / "claims.jsonl")
        self.assertEqual(cm.exception.code, 2)

    def test_full_report_includes_the_graph(self):
        rc, out, _ = run(EVENTS, "--claims", SAMPLES / "claims.jsonl")
        self.assertEqual(rc, 0)
        self.assertIn("## Who addresses whom", out)
        self.assertIn("## Findings", out)

    def test_no_source_still_refused(self):
        with self.assertRaises(SystemExit):
            run(EVENTS)


if __name__ == "__main__":
    unittest.main()
