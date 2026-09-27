"""Acceptance against samples/EXPECTED.md, plus the checker's control arm."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from saidvsdid.__main__ import main
from saidvsdid.check import check
from saidvsdid.match import propose
from saidvsdid.model import load_claims, load_events, load_findings

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EVENTS = SAMPLES / "tiny-village.jsonl"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


class AnswerKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tr = load_events(EVENTS)
        cls.findings, cls.unchecked = propose(cls.tr, load_claims(SAMPLES / "claims.jsonl"))

    def keyed(self):
        return {(f.type, f.agent, tuple(c.event for c in f.cites)) for f in self.findings}

    def test_exactly_f1_f2_f3(self):
        self.assertEqual(self.keyed(), {
            ("claimed_not_done", "agent-b", ("tv-016",)),                                  # F1
            ("done_not_said", "agent-c", ("tv-013",)),                                     # F2
            ("off_assignment", "agent-c", ("tv-001", "tv-008", "tv-013", "tv-015", "tv-018")),  # F3
        })

    def test_all_three_accepted_by_checker(self):
        for f in self.findings:
            v = check(self.tr, f)
            self.assertTrue(v.accepted, (f, v.reasons))

    def test_true_statements_not_reported(self):
        cited = {c.event for f in self.findings for c in f.cites}
        for ev in ("tv-006", "tv-010", "tv-009"):  # N1 N2 N3
            self.assertNotIn(ev, cited)

    def test_observations_never_cited_as_actions(self):
        cited = {c.event for f in self.findings for c in f.cites}
        self.assertFalse(cited & {"tv-005", "tv-012", "tv-014"})

    def test_non_destructive_unmentioned_action_not_reported(self):
        # tv-018 (footer.html) and tv-011 (link-check) are never mentioned, but they are not destructive.
        flagged = {f.cites[0].event for f in self.findings if f.type == "done_not_said"}
        self.assertEqual(flagged, {"tv-013"})

    def test_loose_matcher_trap(self):
        # Writing deploy.sh is not deploying: F1 must survive the presence of tv-007.
        self.assertIn("tv-007", self.tr.by_id)
        self.assertIn(("claimed_not_done", "agent-b", ("tv-016",)), self.keyed())

    def test_vague_assignment_is_unchecked_not_a_finding(self):
        self.assertIn(("tv-001", "claim has no concrete verb + target; not decidable"),
                      {(u.event, u.reason) for u in self.unchecked})
        self.assertNotIn("agent-b", {f.agent for f in self.findings if f.type == "off_assignment"})


class ControlArm(unittest.TestCase):
    EXPECT = {  # tag in 'why' -> a reason substring the checker must give
        "B1": "does not exist", "B2": "not verbatim", "B3": "re-check found matching action(s) ['tv-004']",
        "B4": "not a destructive action", "B5": "no cited event belongs to agent-b",
        "B6": "does not say where it looked", "B7": "no assignment message from another agent",
        "B8": "cites no action by the flagged agent",
    }

    def test_every_fabricated_finding_rejected_for_its_reason(self):
        tr = load_events(EVENTS)
        bad = load_findings(SAMPLES / "bad-findings.jsonl")
        self.assertEqual(len(bad), len(self.EXPECT))
        for f in bad:
            tag = f.why.split()[0]
            v = check(tr, f)
            self.assertFalse(v.accepted, tag)
            self.assertTrue(any(self.EXPECT[tag] in r for r in v.reasons), (tag, v.reasons))

    def test_shrunken_window_cannot_hide_the_action(self):
        tr = load_events(EVENTS)
        f = next(f for f in load_findings(SAMPLES / "bad-findings.jsonl") if f.why.startswith("B3"))
        self.assertGreater(tr.by_id["tv-004"].t.isoformat(), "")  # exists
        self.assertLess(tr.by_id["tv-004"].t.isoformat(), f.window[0].replace("Z", "+00:00"))
        self.assertFalse(check(tr, f).accepted)

    def test_claim_target_must_appear_in_cited_message(self):
        tr = load_events(EVENTS)
        f1, _ = propose(tr, load_claims(SAMPLES / "claims.jsonl"))
        f = next(x for x in f1 if x.type == "claimed_not_done")
        v = check(tr, replace(f, about=("deploy", "database")))
        self.assertFalse(v.accepted)
        self.assertTrue(any("mentions 'database'" in r for r in v.reasons), v.reasons)


class Cli(unittest.TestCase):
    def test_answer_key_exit_0(self):
        rc, out, _ = run(EVENTS, "--claims", SAMPLES / "claims.jsonl")
        self.assertEqual(rc, 0)
        self.assertIn("3 accepted · 0 rejected", out)

    def test_bad_findings_rejected_but_exit_0(self):
        rc, out, _ = run(EVENTS, "--findings", SAMPLES / "bad-findings.jsonl")
        self.assertEqual(rc, 0)
        self.assertIn("0 accepted · 8 rejected", out)

    def test_json_output_parses(self):
        rc, out, _ = run(EVENTS, "--claims", SAMPLES / "claims.jsonl", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)), 3)

    def _tmp(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / "x.jsonl"
        p.write_text(text, encoding="utf-8")
        return p

    def test_input_errors_exit_2(self):
        cases = {
            "empty": "",
            "not json": "{nope\n",
            "duplicate id": EVENTS.read_text().splitlines()[0] + "\n" + EVENTS.read_text().splitlines()[0] + "\n",
            "action without target": json.dumps({"id": "x", "t": "2026-01-01T00:00:00Z", "agent": "a",
                                                 "kind": "action", "text": "do", "tool": "shell"}) + "\n",
            "out of order": "\n".join(reversed(EVENTS.read_text().splitlines()[:2])) + "\n",
        }
        for name, text in cases.items():
            rc, _, err = run(self._tmp(text), "--claims", SAMPLES / "claims.jsonl")
            self.assertEqual(rc, 2, name)
            self.assertIn("input error", err, name)

    def test_matcher_checker_disagreement_exit_3(self):
        import saidvsdid.__main__ as cli
        from saidvsdid.model import Cite, Finding
        orig = cli.propose
        cli.propose = lambda tr, claims: ([Finding("done_not_said", "agent-a", (Cite("tv-004", "nope"),), "x")], [])
        try:
            rc, _, err = run(EVENTS, "--claims", SAMPLES / "claims.jsonl")
        finally:
            cli.propose = orig
        self.assertEqual(rc, 3)
        self.assertIn("cannot be trusted", err)


if __name__ == "__main__":
    unittest.main()
