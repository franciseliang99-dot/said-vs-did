# Said vs Did

Reads a multi-agent transcript and reports where an agent's words and its actions
disagree. Every finding cites the transcript lines it rests on, and a deterministic
checker throws out any finding whose citations don't hold up.

Three kinds of finding:

| type | meaning |
|---|---|
| `claimed_not_done` | The agent said it did something. No matching action exists before the claim. |
| `done_not_said` | The agent did something destructive (delete, overwrite, …) and never mentioned it. |
| `off_assignment` | Another agent assigned it X. Everything it did afterwards was something else. |

## Run it

Python 3.11+, standard library only.

```
PYTHONPATH=src python3 -m saidvsdid samples/tiny-village.jsonl --claims samples/claims.jsonl
PYTHONPATH=src python3 -m saidvsdid samples/tiny-village.jsonl --extract ollama:llama3.1:8b
PYTHONPATH=src python3 -m saidvsdid samples/tiny-village.jsonl --graph
PYTHONPATH=src python3 -m unittest discover -s tests
```

On the AI Village dataset (downloaded separately; see DESIGN.md), convert a time window
first, then run on the result:

```
PYTHONPATH=src python3 -m saidvsdid.village DATA_DIR --since 2026-09-17T00:00:00Z --until 2026-09-18T00:00:00Z -o day.jsonl
PYTHONPATH=src python3 -m saidvsdid day.jsonl --graph
```

Every report starts with **who addresses whom**: a directed edge per sender and addressee,
citing the messages it rests on. Messages with no addressee are counted as broadcasts, not
guessed into edges. `--graph` prints only that part and needs no claims; add `--json` for
the full edge list.

`--extract BACKEND` has a model propose the claims instead of reading them from a file:
`ollama:<model>` (local, `OLLAMA_HOST` or `localhost:11434`) or `anthropic[:<model>]`
(needs `ANTHROPIC_API_KEY`). One call per message. `--save-claims <file>` writes the
claims that survived validation, in the same format `--claims` reads.

`--report-from TIME` (ISO, with a time zone) is for a slice of a longer run. Convert the
slice with some lookback before it (a day is enough on the Village data), then pass the
slice start here. The checker searches the lookback, so a claim made just after the start
is judged against the actions before it; the report (findings, unchecked items and the
graph) covers only what happens from `TIME` on, and `--extract` sends only those messages
to the model. Without lookback, a claim made a minute into a slice is judged against an
empty past and looks unbacked.

`--findings <file>` checks someone else's findings (for example, a model's) instead of
proposing its own. `--json` prints machine-readable verdicts.

Exit codes: `0` report written · `2` the input could not be trusted (bad JSON, duplicate
ids, events out of order, no events) · `3` the matcher proposed a finding its own
checker rejected, so the report is not trustworthy · `4` (with `--extract`) at least one
message got no usable model output, so claims from it are missing, not absent.

## What the checker enforces

A finding is rejected, with the reason printed, unless:

1. every cited event exists and every quote is verbatim from it;
2. at least one cited event belongs to the flagged agent;
3. for `claimed_not_done`: it says where it looked, the cited message names the target,
   and a fresh search from the start of the transcript finds no matching action (a
   finding can't narrow its own window to hide the action that clears it);
4. for `done_not_said`: the action is destructive and the agent never mentions its target;
5. for `off_assignment`: the assignment came from another agent, names this agent and the
   target, and none of the assignee's later actions match.

`samples/bad-findings.jsonl` holds eight fabricated findings, one per way of cheating.
All eight are rejected, each for its own reason; the tests pin which reason.

## The sample

`samples/tiny-village.jsonl` is a hand-made 20-event transcript with three planted
findings and three planted true statements that must not be flagged.
`samples/EXPECTED.md` is the answer key and lists the traps it is built to catch.

## Limits, stated plainly

- **Extracted claims are validated, not trusted.** Code, not the model, decides which
  event and agent a claim belongs to. A claim is refused, and listed with the reason,
  if its quote is not verbatim, its type or verb is out of range, its assignee is
  unknown or the speaker, or its target does not appear inside the quote. That last rule
  is strict on purpose: a target the message never says is a guess, so "the deploy
  script" cannot become `deploy.sh`. A `done`, `doing` or `will_do` claim is also
  refused if it is not the speaker's own act: a quote with no first-person word whose
  subject is another agent or a noun phrase ("GPT-5 deployed the site", "The checklist
  has been recreated") is a status report, and a `done` worded in the future ("I'll
  check the site now") is not done. The cost is that such claims are not checked.
  On one real day this refused about a quarter of the model's claims; among them are a
  few passive-voice statements of the speaker's own work, which go unchecked rather
  than cleared or accused.
  Whatever survives still has to pass the checker.
- **The checker is not independent of the matcher.** They share one matching predicate,
  so the checker fully gates proposed findings but would not catch a bug in that
  predicate. The tests pin the predicate separately, including a mutation check for
  the "deploy script is not a deploy" trap.
- **Target matching is exact** (case-insensitive, trailing `/` ignored). It will miss
  the same file written two ways. That is deliberate until a looser rule can show its
  pairings in the report.
- **The prompt was picked by running it, not by reading it.** Later drafts that answered
  review comments did worse on `llama3.1:8b`, so the first draft ships. Two review
  comments on it are still open. Both are in DESIGN.md, "Choosing the prompt".
- **No dataset content is in this repository.** The adapter runs on the AI Village
  dataset, but the dataset is access-reviewed, so every result shown here is on the
  hand-made sample. Real-data results are summarised as counts in WRITEUP.md.

Design notes: [DESIGN.md](DESIGN.md).

## License

MIT.
