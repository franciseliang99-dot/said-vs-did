"""Extraction with a fake backend: the model's replies are scripted, the validator is not."""

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saidvsdid import __main__ as cli  # noqa: E402
from saidvsdid.check import check  # noqa: E402
from saidvsdid.extract import extract, parse_items, render_prompt, validate  # noqa: E402
from saidvsdid.match import propose  # noqa: E402
from saidvsdid.model import Event, load_events  # noqa: E402
from saidvsdid.prompt import EXTRACT_PROMPT  # noqa: E402

SAMPLE = ROOT / "samples" / "tiny-village.jsonl"
HAND = ROOT / "samples" / "claims.jsonl"
AGENTS = ["agent-a", "agent-b", "agent-c"]


def scripted_from_hand_claims():
    """Replay the hand-written claims as if a model had produced them, keyed by message text."""
    tr = load_events(SAMPLE)
    by_event: dict[str, list] = {}
    for line in HAND.read_text().splitlines():
        o = json.loads(line)
        by_event.setdefault(o["event"], []).append(
            {"type": o["type"], "quote": o["quote"], "verb": o["about"]["verb"],
             "target": o["about"]["target"], "assignee": o.get("assignee")})
    replies = {tr.by_id[e].text: json.dumps(items) for e, items in by_event.items()}

    def backend(prompt: str) -> str:
        msg = prompt.rsplit("<message>\n", 1)[1].split("\n</message>", 1)[0]  # the real one is last
        return replies.get(msg, "[]")
    return backend


def ev(text, agent="agent-a"):
    return Event("m1", None, agent, "message", text)


class Validator(unittest.TestCase):
    OK = {"type": "done", "quote": "I pushed build.zip", "verb": "deploy", "target": "build.zip", "assignee": None}
    E = ev("Morning. I pushed build.zip to prod.")

    def reason(self, **over):
        claim, why = validate({**self.OK, **over}, self.E, AGENTS)
        self.assertIsNone(claim)
        return why

    def test_good_item_is_kept_with_event_and_agent_from_code(self):
        claim, why = validate(self.OK, self.E, AGENTS)
        self.assertEqual(why, "")
        self.assertEqual((claim.event, claim.agent), ("m1", "agent-a"))

    def test_each_rule_refuses_for_its_own_reason(self):
        self.assertIn("unknown type", self.reason(type="finished"))
        self.assertIn("not a verbatim", self.reason(quote="I pushed Build.zip"))
        self.assertIn("empty quote", self.reason(quote=" "))
        self.assertIn("verb", self.reason(verb="push"))
        self.assertIn("does not appear", self.reason(target="site"))
        self.assertIn("only allowed on assign", self.reason(assignee="agent-b"))
        self.assertIn("not a known agent", self.reason(type="assign", assignee="agent-z"))
        self.assertIn("is the speaker", self.reason(type="assign", assignee="agent-a"))
        self.assertIn("missing field", validate({"type": "done"}, self.E, AGENTS)[1])
        self.assertIn("not an object", validate("done", self.E, AGENTS)[1])

    def test_target_borrowed_from_another_sentence_is_refused(self):
        # "prod" is in the message, but not in the words that make the claim.
        self.assertIn("does not appear in the quote", self.reason(target="prod"))

    def test_nulls_are_allowed(self):
        claim, _ = validate({**self.OK, "verb": None, "target": None}, self.E, AGENTS)
        self.assertIsNotNone(claim)


class Parsing(unittest.TestCase):
    def test_one_fence_is_tolerated(self):
        self.assertEqual(parse_items('```json\n[]\n```'), [])

    def test_prose_or_objects_are_not(self):
        for raw in ("Here you go: []", '{"claims": []}', ""):
            with self.assertRaises(ValueError):
                parse_items(raw)


class Pipeline(unittest.TestCase):
    def test_replayed_hand_claims_reproduce_the_answer_key(self):
        tr = load_events(SAMPLE)
        ex = extract(tr, scripted_from_hand_claims())
        self.assertEqual(ex.messages, 10)
        self.assertEqual(ex.failed, [])
        # The hand claims name two targets the messages never say ("the deploy script" -> deploy.sh,
        # "the link checker" -> link-check). Those are normalizations, i.e. guesses: refused, not passed on.
        self.assertEqual([(r.event, "does not appear" in r.reason) for r in ex.rejected],
                         [("tv-002", True), ("tv-010", True)])
        findings, _ = propose(tr, ex.claims)
        verdicts = [check(tr, f) for f in findings]
        self.assertTrue(all(v.accepted for v in verdicts))
        self.assertEqual(sorted((f.type, f.agent) for f in findings),
                         [("claimed_not_done", "agent-b"), ("done_not_said", "agent-c"), ("off_assignment", "agent-c")])

    def test_unusable_reply_is_a_failure_not_an_empty_list(self):
        tr = load_events(SAMPLE)

        def backend(prompt):
            if "Wrapping up" in prompt:
                raise RuntimeError("cannot reach backend")
            return "Sure! Here are the claims." if "Great, thanks" in prompt else "[]"
        ex = extract(tr, backend)
        self.assertEqual(sorted(e for e, _ in ex.failed), ["tv-017", "tv-020"])
        self.assertEqual(ex.claims, [])


class Prompt(unittest.TestCase):
    def test_placeholders_filled_and_message_braces_survive(self):
        p = render_prompt(ev("set {x} in cfg.json"), AGENTS)
        self.assertIn("Speaker: agent-a\nAgents: agent-a, agent-b, agent-c\n<message>\nset {x} in cfg.json\n</message>", p)
        self.assertNotIn("{speaker}", p)

    @staticmethod
    def leaked(prompt: str) -> list[str]:
        """Sample sentences of three or more words that appear, as whole words, in the prompt.
        Shorter ones ("On it.") are ordinary phrases, not a leak of the answer key."""
        low = prompt.lower()
        out = []
        for e in load_events(SAMPLE).events:
            if e.kind == "message":
                for sentence in re.split(r"(?<=[.?!])\s+", e.text):
                    s = sentence.lower().strip(" .?!")
                    if len(s.split()) >= 3 and re.search(r"(?<!\w)" + re.escape(s) + r"(?!\w)", low):
                        out.append(s)
        return out

    def test_no_sample_sentence_leaks_into_the_prompt(self):
        self.assertEqual(self.leaked(EXTRACT_PROMPT), [])

    def test_leak_check_catches_a_planted_sentence(self):
        planted = EXTRACT_PROMPT + "\nI've deployed the digest page to the site.\n"
        self.assertEqual(self.leaked(planted), ["i've deployed the digest page to the site"])


class Cli(unittest.TestCase):
    def run_cli(self, argv, backend):
        saved = cli.backend_from_spec
        cli.backend_from_spec = lambda spec: backend
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv)
        finally:
            cli.backend_from_spec = saved
        return rc, out.getvalue(), err.getvalue()

    def test_extract_writes_report_and_saves_claims(self):
        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / "claims.jsonl"
            rc, out, _ = self.run_cli([str(SAMPLE), "--extract", "fake", "--save-claims", str(saved)],
                                      scripted_from_hand_claims())
            self.assertEqual(rc, 0)
            self.assertIn("3 accepted · 0 rejected", out)
            self.assertIn("10 messages · 7 claims kept · 2 refused · 0 messages failed", out)
            self.assertEqual(len(saved.read_text().splitlines()), 7)
            # Saved claims round-trip through the hand-written path to the same verdict.
            rc2, out2, _ = self.run_cli([str(SAMPLE), "--claims", str(saved)], None)
            self.assertEqual((rc2, "3 accepted · 0 rejected" in out2), (0, True))

    def test_failed_messages_exit_4(self):
        def backend(prompt):
            raise RuntimeError("down")
        rc, out, err = self.run_cli([str(SAMPLE), "--extract", "fake"], backend)
        self.assertEqual(rc, 4)
        self.assertIn("10 of 10 message(s)", err)
        self.assertIn("10 messages failed", out)

    def test_claims_and_extract_are_exclusive(self):
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            cli.main([str(SAMPLE), "--claims", str(HAND), "--extract", "ollama:x"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
