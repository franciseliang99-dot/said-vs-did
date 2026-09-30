"""--report-from: earlier events are lookback. The checker searches them; the report does not cover them."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saidvsdid import __main__ as cli  # noqa: E402
from saidvsdid.extract import extract  # noqa: E402
from saidvsdid.model import load_events, parse_time  # noqa: E402

DAY2 = "2026-09-21T00:00:00Z"
EVENTS = [
    # day 1 (lookback): agent-a deploys; agent-b deletes a file it never mentions
    {"id": "d1-a", "t": "2026-09-20T09:00:00Z", "agent": "agent-a", "kind": "action", "to": [],
     "text": "deploy site.zip", "tool": "deploy", "target": "site.zip"},
    {"id": "d1-b", "t": "2026-09-20T09:05:00Z", "agent": "agent-b", "kind": "action", "to": [],
     "text": "delete old.txt", "tool": "file_delete", "target": "old.txt"},
    {"id": "d1-m", "t": "2026-09-20T09:10:00Z", "agent": "agent-b", "kind": "message", "to": [],
     "text": "Morning all."},
    # day 2 (reported): a plan exactly at the start, then agent-a says it deployed, a day after doing it
    {"id": "d2-0", "t": DAY2, "agent": "agent-c", "kind": "message", "to": [],
     "text": "I will write notes.md."},
    {"id": "d2-m", "t": "2026-09-21T10:00:00Z", "agent": "agent-a", "kind": "message", "to": [],
     "text": "I deployed site.zip."},
]
CLAIM = {"event": "d2-m", "agent": "agent-a", "type": "done", "quote": "I deployed site.zip",
         "about": {"verb": "deploy", "target": "site.zip"}}
# Not judged either way, so they land in "Not checked": one in the lookback, one at the start instant.
PLANS = [{"event": "d1-m", "agent": "agent-b", "type": "will_do", "quote": "Morning all",
          "about": {"verb": None, "target": None}},
         {"event": "d2-0", "agent": "agent-c", "type": "will_do", "quote": "I will write notes.md",
          "about": {"verb": "write", "target": "notes.md"}}]


class ReportFrom(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.full = d / "full.jsonl"
        self.day2 = d / "day2.jsonl"
        self.claims = d / "claims.jsonl"
        self.full.write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
        self.day2.write_text("".join(json.dumps(e) + "\n" for e in EVENTS if e["t"] >= DAY2))
        self.claims.write_text(json.dumps(CLAIM) + "\n")
        self.plans = d / "plans.jsonl"
        self.plans.write_text("".join(json.dumps(c) + "\n" for c in [CLAIM] + PLANS))

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def findings(self, *argv):
        rc, out, _ = self.run_cli(*argv, "--json")
        self.assertEqual(rc, 0)
        return [(v["finding"]["type"], v["finding"]["agent"]) for v in json.loads(out) if v["accepted"]]

    def test_slice_without_lookback_accuses_the_agent_that_did_it(self):
        # The failure this flag exists for: the deploy is before the slice, so it looks absent.
        self.assertEqual(self.findings(self.day2, "--claims", self.claims), [("claimed_not_done", "agent-a")])

    def test_lookback_clears_the_claim(self):
        self.assertEqual(self.findings(self.full, "--claims", self.claims, "--report-from", DAY2), [])

    def test_lookback_findings_are_not_reported(self):
        # Control arm: without the flag the same transcript reports agent-b's day-1 deletion.
        self.assertEqual(self.findings(self.full, "--claims", self.claims), [("done_not_said", "agent-b")])
        self.assertNotIn(("done_not_said", "agent-b"),
                         self.findings(self.full, "--claims", self.claims, "--report-from", DAY2))

    def test_report_says_where_it_starts(self):
        rc, out, _ = self.run_cli(self.full, "--claims", self.claims, "--report-from", DAY2)
        self.assertEqual(rc, 0)
        self.assertIn(f"Reporting from {DAY2}; earlier events are lookback", out)

    def test_graph_covers_only_the_reported_window(self):
        graph = lambda out: out.split("## Who addresses whom", 1)[1].split("## Findings", 1)[0]
        rc, out, _ = self.run_cli(self.full, "--claims", self.claims, "--report-from", DAY2)
        self.assertEqual(rc, 0)
        self.assertIn("2 agents", graph(out))
        self.assertNotIn("`d1-m`", graph(out))
        # control arm: without the flag the lookback broadcast and its agent are in the graph
        full = graph(self.run_cli(self.full, "--claims", self.claims)[1])
        self.assertIn("3 agents", full)
        self.assertIn("`d1-m`", full)

    def test_start_after_the_last_event_is_an_input_error(self):
        rc, _, err = self.run_cli(self.full, "--claims", self.claims, "--report-from", "2026-09-22T00:00:00Z")
        self.assertEqual(rc, 2)
        self.assertIn("after the last event", err)

    def test_start_without_a_time_zone_is_an_input_error(self):
        rc, _, err = self.run_cli(self.full, "--claims", self.claims, "--report-from", "2026-09-21T00:00:00")
        self.assertEqual(rc, 2)
        self.assertIn("no time zone", err)

    def test_extraction_skips_lookback_messages(self):
        seen = []
        ex = extract(load_events(self.full), lambda p: seen.append(p) or "[]", parse_time(DAY2))
        self.assertEqual(ex.messages, 2)
        self.assertEqual(len(seen), 2)
        self.assertFalse(any("Morning all." in p for p in seen))

    def test_not_checked_covers_the_start_instant_but_not_the_lookback(self):
        rc, out, _ = self.run_cli(self.full, "--claims", self.plans, "--report-from", DAY2)
        self.assertEqual(rc, 0)
        not_checked = out.split("## Not checked", 1)[1]
        self.assertIn("`d2-0`", not_checked)
        self.assertNotIn("`d1-m`", not_checked)
        # control arm: without the flag the lookback plan is listed
        self.assertIn("`d1-m`", self.run_cli(self.full, "--claims", self.plans)[1].split("## Not checked", 1)[1])

    def test_cli_extracts_only_from_the_start(self):
        seen = []
        saved = cli.backend_from_spec
        cli.backend_from_spec = lambda spec: (lambda p: seen.append(p) or "[]")
        try:
            rc, _, _ = self.run_cli(self.full, "--extract", "fake", "--report-from", DAY2)
        finally:
            cli.backend_from_spec = saved
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 2)  # d2-0 and d2-m, not d1-m
        self.assertFalse(any("Morning all." in p for p in seen))
