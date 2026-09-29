"""The AI Village adapter, pinned against a hand-made miniature of the dataset's tables.

Every row below is invented. Table and column names follow the dataset's SCHEMA.md."""

import gzip
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from saidvsdid.graph import build_graph
from saidvsdid.model import load_events
from saidvsdid.village import convert, main, mentioned, village_time

A1, A2, A3 = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b", \
    "00000000-0000-0000-0000-00000000000c"
S1, S2 = "10000000-0000-0000-0000-000000000001", "10000000-0000-0000-0000-000000000002"

AGENTS = [{"id": A1, "name": "Opus 4.5"}, {"id": A2, "name": "Opus 4"}, {"id": A3, "name": "Gemini"}]
SESSIONS = [{"id": S1, "agent_id": A1}, {"id": S2, "agent_id": A3}]
EVENTS = [
    {"id": "e-before", "created_at": "2026-10-02 23:59:59.999999",
     "data": {"actionType": "AGENT_TALK", "speakerId": A1, "content": "outside the window"}},
    {"id": "e1", "created_at": "2026-10-03 00:00:00",
     "data": {"actionType": "AGENT_TALK", "speakerId": A1,
              "content": "@Opus 4 please check. @Gemini too. Signed, @Opus 4.5"}},
    {"id": "e2", "created_at": "2026-10-03 00:01:00.5",
     "data": {"actionType": "USER_TALK", "speakerName": "someone", "content": "a human line"}},
    {"id": "e3", "created_at": "2026-10-03 00:02:00",
     "data": {"actionType": "WAIT"}},
    {"id": "e4", "created_at": "2026-10-03 00:03:00",
     "data": {"actionType": "AGENT_TALK", "speakerId": A3, "content": "@Opus 4.5 done, I ran it"}},
    {"id": "e-at-until", "created_at": "2026-10-04 00:00:00",
     "data": {"actionType": "AGENT_TALK", "speakerId": A3, "content": "exactly at --until"}},
]
TURNS = [
    {"id": "t1", "session_id": S1, "created_at": "2026-10-03 00:00:30",
     "agent_action": {"command": "  rm -rf build  "}, "output": "", "error": "rm: build: not found"},
    {"id": "t2", "session_id": S2, "created_at": "2026-10-03 00:02:30",
     "agent_action": {"action": "type", "text": "hello"}, "output": None, "error": None},
    {"id": "t3", "session_id": S2, "created_at": "2026-10-03 00:02:40",
     "agent_action": {"action": "left_click", "coordinate": [1, 2]}, "output": None, "error": None},
    {"id": "t4", "session_id": S2, "created_at": "2026-10-03 00:02:50",
     "agent_action": {"action": "screenshot"}, "output": None, "error": None},
    {"id": "t5", "session_id": S1, "created_at": "2026-10-03 00:02:55",
     "agent_action": None, "output": None, "error": None},
]
SINCE = datetime(2026, 10, 3, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 4, tzinfo=timezone.utc)


def write_gz(path: Path, rows, raw_lines=()):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        for line in raw_lines:
            f.write(line + "\n")


def make_dataset(turn_file="turns.slim.jsonl.gz", events=EVENTS, turns=TURNS, agents=AGENTS, bad_event_line=None):
    d = Path(tempfile.mkdtemp())
    write_gz(d / "agents.jsonl.gz", agents)
    write_gz(d / "computer_use_sessions.jsonl.gz", SESSIONS)
    write_gz(d / "events.jsonl.gz", events, [bad_event_line] if bad_event_line else [])
    write_gz(d / turn_file, turns)
    return d


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


class Mapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events, cls.rep = convert(make_dataset(), SINCE, UNTIL)
        cls.by_id = {e["id"]: e for e in cls.events}

    def test_exact_output(self):
        self.assertEqual([(e["id"], e["kind"], e["agent"]) for e in self.events], [
            ("e1", "message", "Opus 4.5"),
            ("t1", "action", "Opus 4.5"),
            ("t1:error", "observation", "Opus 4.5"),
            ("t2", "action", "Gemini"),
            ("t3", "action", "Gemini"),
            ("e4", "message", "Gemini"),
        ])
        self.assertEqual(self.rep.problems, [])

    def test_window_is_inclusive_then_exclusive(self):
        self.assertIn("e1", self.by_id)           # exactly at --since, written without a fraction
        self.assertNotIn("e-before", self.by_id)
        self.assertNotIn("e-at-until", self.by_id)

    def test_humans_are_counted_never_emitted(self):
        self.assertNotIn("e2", self.by_id)
        self.assertEqual(self.rep.skipped["event USER_TALK (human, never emitted)"], 1)
        self.assertNotIn("a human line", json.dumps(self.events))

    def test_every_skip_is_counted(self):
        self.assertEqual(dict(self.rep.skipped), {
            "event USER_TALK (human, never emitted)": 1, "event WAIT": 1,
            "turn screenshot (no effect)": 1, "turn with no action": 1})

    def test_actions(self):
        self.assertEqual({k: self.by_id["t1"][k] for k in ("tool", "target", "text")},
                         {"tool": "command", "target": "rm -rf build", "text": "rm -rf build"})
        self.assertEqual({k: self.by_id["t2"][k] for k in ("tool", "target", "text")},
                         {"tool": "gui", "target": "hello", "text": "type hello"})
        self.assertEqual(self.by_id["t3"]["target"], "")
        self.assertNotIn("t1:output", self.by_id)  # empty output is not an observation

    def test_mentions_fill_to_longest_name_first_and_never_self(self):
        self.assertEqual(self.by_id["e1"]["to"], ["Opus 4", "Gemini"])
        self.assertEqual(self.by_id["e4"]["to"], ["Opus 4.5"])

    def test_times_and_src(self):
        self.assertEqual(self.by_id["e1"]["t"], "2026-10-03T00:00:00.000000Z")
        self.assertEqual(self.by_id["t1"]["src"], {"file": "turns.slim.jsonl.gz", "line": 1})
        self.assertEqual(self.by_id["e4"]["src"], {"file": "events.jsonl.gz", "line": 5})

    def test_output_loads_and_graphs(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("".join(json.dumps(e) + "\n" for e in self.events))
        tr = load_events(Path(f.name))
        self.assertEqual(len(tr.events), 6)
        g = build_graph(tr)
        self.assertEqual(sorted((e.src, e.dst) for e in g.edges),
                         [("Gemini", "Opus 4.5"), ("Opus 4.5", "Gemini"), ("Opus 4.5", "Opus 4")])

    def test_deterministic(self):
        self.assertEqual(convert(make_dataset(), SINCE, UNTIL)[0], self.events)

    def test_full_turns_file_is_read_too(self):
        events, _ = convert(make_dataset(turn_file="computer_use_turns.jsonl.gz"), SINCE, UNTIL)
        self.assertEqual([e["id"] for e in events], [e["id"] for e in self.events])


class Mentions(unittest.TestCase):
    NAMES = ["Opus 4.5", "Opus 4", "Gemini"]

    def test_boundaries(self):
        self.assertEqual(mentioned("@Opus 4.5 hi", self.NAMES), ["Opus 4.5"])
        self.assertEqual(mentioned("@Opus 4, hi", self.NAMES), ["Opus 4"])
        self.assertEqual(mentioned("thanks @Opus 4.", self.NAMES), ["Opus 4"])  # sentence end, not a version
        self.assertEqual(mentioned("@Opus 45", self.NAMES), [])
        self.assertEqual(mentioned("@Geminis", self.NAMES), [])
        self.assertEqual(mentioned("@gemini", self.NAMES), [])
        self.assertEqual(mentioned("mail x@Gemini", self.NAMES), ["Gemini"])  # exact text, no guessing

    def test_a_version_suffix_is_not_a_boundary_even_without_the_longer_name(self):
        # "@Opus 4.5" names an agent missing from this roster; it must not be read as "Opus 4".
        self.assertEqual(mentioned("@Opus 4.5 hi", ["Opus 4"]), [])

    def test_a_matched_span_is_not_matched_again_by_a_shorter_name(self):
        self.assertEqual(mentioned("@Opus 4 hi", ["Opus 4", "Opus"]), ["Opus 4"])
        self.assertEqual(mentioned("@Opus hi @Opus 4", ["Opus 4", "Opus"]), ["Opus", "Opus 4"])

    def test_village_time(self):
        self.assertEqual(village_time("2026-10-03 00:00:00"), "2026-10-03T00:00:00.000000Z")
        self.assertEqual(village_time("2025-12-29 18:49:21.291984"), "2025-12-29T18:49:21.291984Z")
        with self.assertRaises(ValueError):
            village_time("2026-10-03T00:00:00+00:00")


class Refusals(unittest.TestCase):
    def cli(self, data):
        out = Path(tempfile.mkdtemp()) / "events.jsonl"
        rc, stdout, err = run(data, "--since", "2026-10-03T00:00:00Z", "--until", "2026-10-04T00:00:00Z", "-o", out)
        return rc, stdout, err, out

    def test_good_run_prints_counts_not_text(self):
        rc, stdout, err, out = self.cli(make_dataset())
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.exists())
        self.assertIn("wrote 6 events", stdout)
        for text in ("please check", "rm -rf", "hello", "a human line"):
            self.assertNotIn(text, stdout + err)

    def test_unparseable_line_fails_and_writes_nothing(self):
        rc, _, err, out = self.cli(make_dataset(bad_event_line="{not json"))
        self.assertEqual(rc, 2)
        # The bad line is written after every good row, so its number is len(EVENTS) + 1.
        self.assertIn(f"events.jsonl.gz line {len(EVENTS) + 1}: not JSON", err)
        self.assertFalse(out.exists())

    def test_empty_window_is_an_error(self):
        rc, _, err, out = self.cli(make_dataset(events=EVENTS[:1], turns=[]))
        self.assertEqual(rc, 2)
        self.assertIn("no events in the window", err)
        self.assertFalse(out.exists())

    def test_unknown_speaker_is_a_problem(self):
        ev = [{"id": "x", "created_at": "2026-10-03 01:00:00",
               "data": {"actionType": "AGENT_TALK", "speakerId": "nobody", "content": "hi"}}]
        rc, _, err, _ = self.cli(make_dataset(events=ev))
        self.assertEqual(rc, 2)
        self.assertIn("without a known speaker", err)

    def test_unrecognized_action_shape_is_a_problem(self):
        tu = [{"id": "x", "session_id": S1, "created_at": "2026-10-03 01:00:00",
               "agent_action": ["not", "an", "object"], "output": None, "error": None}]
        rc, _, err, _ = self.cli(make_dataset(turns=tu))
        self.assertEqual(rc, 2)
        self.assertIn("unrecognized agent_action shape", err)

    def test_known_no_effect_shapes_are_counted_not_failed(self):
        shapes = [{"restart": True}, {"restart": True, "command": None}, {},
                  {"coordinate": None, "text": None}, {"coordinate": [1, 2], "text": None}]
        tu = [{"id": f"x{i}", "session_id": S1, "created_at": "2026-10-03 01:00:00",
               "agent_action": a, "output": None, "error": None} for i, a in enumerate(shapes)]
        events, rep = convert(make_dataset(turns=tu), SINCE, UNTIL)
        self.assertEqual(rep.problems, [])
        self.assertEqual({k: v for k, v in rep.skipped.items() if k.startswith("turn")}, {
            "turn shell restart (no effect)": 2, "turn empty action": 1,
            "turn action without a name (not decidable)": 2})
        self.assertEqual([e for e in events if e["id"].startswith("x")], [])

    def test_near_miss_shapes_still_fail(self):
        for a in ({"restart": False}, {"restart": True, "extra": 1},
                  {"coordinate": None}, {"text": "x"}):
            tu = [{"id": "x", "session_id": S1, "created_at": "2026-10-03 01:00:00",
                   "agent_action": a, "output": None, "error": None}]
            with self.subTest(a=a):
                _, rep = convert(make_dataset(turns=tu), SINCE, UNTIL)
                self.assertTrue(any("unrecognized agent_action shape" in p for p in rep.problems))

    def test_duplicate_agent_names_are_a_problem(self):
        rc, _, err, _ = self.cli(make_dataset(agents=AGENTS + [{"id": "dup", "name": "Gemini"}]))
        self.assertEqual(rc, 2)
        self.assertIn("duplicate display names", err)

    def test_naive_window_is_refused(self):
        with self.assertRaises(ValueError):
            convert(make_dataset(), datetime(2026, 10, 3), UNTIL)


if __name__ == "__main__":
    unittest.main()
