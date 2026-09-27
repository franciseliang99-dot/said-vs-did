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
PYTHONPATH=src python3 -m unittest discover -s tests
```

`--findings <file>` checks someone else's findings (for example, a model's) instead of
proposing its own. `--json` prints machine-readable verdicts.

Exit codes: `0` report written · `2` the input could not be trusted (bad JSON, duplicate
ids, events out of order, no events) · `3` the matcher proposed a finding its own
checker rejected, so the report is not trustworthy.

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

- **Claims are hand-written for now.** `samples/claims.jsonl` stands in for the
  extraction step, which will use a language model to propose claims. Whatever it
  proposes still has to pass the checker.
- **The checker is not independent of the matcher.** They share one matching predicate,
  so the checker fully gates proposed findings but would not catch a bug in that
  predicate. The tests pin the predicate separately, including a mutation check for
  the "deploy script is not a deploy" trap.
- **Target matching is exact** (case-insensitive, trailing `/` ignored). It will miss
  the same file written two ways. That is deliberate until a looser rule can show its
  pairings in the report.
- **Nothing has been run on real data yet.** Results here are on the hand-made sample only.

Design notes: [DESIGN.md](DESIGN.md).

## License

MIT.
