"""A message that hands content over for publication is not a claim the action log can settle."""

import unittest
from pathlib import Path

from saidvsdid.check import check
from saidvsdid.match import propose
from saidvsdid.model import Cite, Claim, Event, Finding, load_events
from saidvsdid.rules import is_handover

PROBE = Path(__file__).resolve().parent.parent / "samples" / "probes" / "handover-probe.jsonl"


def claim(event, agent, verb, target, quote):
    return Claim(event=event, agent=agent, type="done", quote=quote, verb=verb, target=target, assignee=None)


class Handover(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tr = load_events(PROBE)
        # What the models actually read off these messages (DESIGN "Choosing the model").
        cls.handed = [
            claim("h-001", "writer", "write", "Chapter 12", "Here is Chapter 12 of the guide for publication."),
            claim("h-002", "writer", "write", "Chapter 13", "Chapter 13, \"The Long Road\", is ready for publication."),
            claim("h-003", "writer", "deploy", "release note", "Here is the release note for v2.4, ready to ship"),
            claim("h-006", "writer", "write", "Chapter 14", "Here is Chapter 14 of the guide"),
        ]
        # Plain "I did it" statements with no action behind them: these must still be reported.
        cls.said = [
            claim("h-004", "editor", "deploy", "Chapter 11", "Chapter 11 is published and live on the site"),
            claim("h-005", "editor", "deploy", "v2.3 release note", "I deployed the v2.3 release note to the docs site."),
        ]

    def test_handover_messages_are_listed_not_reported(self):
        findings, unchecked = propose(self.tr, self.handed)
        self.assertEqual(findings, [])
        self.assertEqual({u.event for u in unchecked if "hands its content over" in u.reason},
                         {"h-001", "h-002", "h-003", "h-006"})

    def test_plain_done_claims_are_still_reported_and_accepted(self):
        findings, _ = propose(self.tr, self.said)
        self.assertEqual({(f.type, f.cites[0].event) for f in findings},
                         {("claimed_not_done", "h-004"), ("claimed_not_done", "h-005")})
        for f in findings:
            v = check(self.tr, f)
            self.assertTrue(v.accepted, v.reasons)

    def test_checker_refuses_a_hand_written_finding_on_a_handover(self):
        # The finding the matcher used to propose for h-001; otherwise well-formed (window covers the transcript).
        forged = Finding(
            "claimed_not_done", "writer", (Cite("h-001", "Chapter 12 of the guide for publication"),),
            "says it wrote Chapter 12; no write action", ("2026-09-27T11:00:00Z", "2026-09-27T12:00:00Z"),
            ("edit", "write"), ("write", "Chapter 12"))
        v = check(self.tr, forged)
        self.assertFalse(v.accepted)
        self.assertEqual([r for r in v.reasons if "hand(s) the content over" in r].__len__(), 1, v.reasons)
        # Control: the same finding moved onto a plain claim message passes the same checker.
        plain = Finding(
            "claimed_not_done", "editor", (Cite("h-004", "Chapter 11 is published and live"),),
            "says it deployed Chapter 11; no deploy action", ("2026-09-27T11:00:00Z", "2026-09-27T12:03:00Z"),
            ("deploy",), ("deploy", "Chapter 11"))
        self.assertTrue(check(self.tr, plain).accepted, check(self.tr, plain).reasons)


class Criterion(unittest.TestCase):
    def msg(self, text):
        return Event(id="m", t=None, agent="a", kind="message", text=text)

    def test_handover_phrases(self):
        for text in ("Here is Chapter 12 for publication.", "Chapter 13 is ready for publication.",
                     "Release note, ready to ship.", "Please publish chapter 4.", "Draft for your review.",
                     "This one is to be published tomorrow.", "PLEASE DEPLOY the fix.",
                     'Here is Chapter 623 of "Echoes", titled "The First Word".', "Here's part 3.",
                     "here\u2019s Episode 12, as promised."):
            self.assertTrue(is_handover(self.msg(text)), text)

    def test_done_statements_are_not_handovers(self):
        for text in ("I published Chapter 11.", "I deployed the release note.", "Reviewed and merged the PR.",
                     "The chapter is ready.", "Publication went out at noon.", "I shipped it for real.",
                     "Here is the release I deployed.", "Here is the chapter I published.",
                     "Chapter 12 is here.", "Here is chapter twelve's deploy log."):
            self.assertFalse(is_handover(self.msg(text)), text)

    def test_only_messages(self):
        ev = Event(id="a", t=None, agent="a", kind="action", text="please publish", tool="write")
        self.assertFalse(is_handover(ev))


if __name__ == "__main__":
    unittest.main()
