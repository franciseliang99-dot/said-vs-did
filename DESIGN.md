# Said vs Did — design sketch

Status (2026-09-27): `match` and the checker are implemented and pass the answer key.
`extract` is implemented (model proposes, code validates; see below). The hand-written
`samples/claims.jsonl` stays as the reference input. The interaction graph (question 1)
is implemented (2026-09-27; see "Interaction graph" below). Not built yet: the dataset adapter.

## What it does

Reads a multi-agent transcript and answers three questions, each answer pinned to
transcript lines:

1. **Who works with whom** — an interaction graph built from who addresses whom.
2. **Said vs did** — for every agent, what it *claimed* (goals, progress, "done")
   against what it *actually executed* (tool calls / actions).
3. **Where they disagree** — three finding types:
   - `claimed_not_done` — the agent said it did something; no matching action exists
     in the searched window.
   - `done_not_said` — the agent executed a consequential action it never mentioned.
   - `off_assignment` — the agent was assigned X and spent its actions on Y.

The core rule: **a finding without citations is not a finding.** A deterministic
checker rejects any finding whose citations do not resolve.

## Pipeline

```
raw transcript ──adapter──▶ events.jsonl ──extract──▶ claims.jsonl
                                  │                        │
                                  └──────── match ◀────────┘
                                              │
                                        findings (proposed)
                                              │
                                   checker (deterministic)
                                              │
                                 report (accepted findings only)
```

- `extract` may use a language model to propose claims. It is only a proposer:
  nothing it outputs reaches the report without passing the checker.
- `match` and `checker` are deterministic and have no model in the loop.

## Normalized event (one JSON object per line)

| field | type | meaning |
|---|---|---|
| `id` | string | Stable, unique. The citation target. |
| `t` | string | ISO 8601 UTC timestamp. |
| `agent` | string | Who produced the event. |
| `kind` | `message` \| `action` \| `observation` | Speech, execution, or what came back. |
| `to` | list of string | Addressees (messages only; empty = broadcast). |
| `text` | string | Message text (messages), or a short description (actions). |
| `tool` | string | Actions only: tool / command family, e.g. `shell`, `browser`, `file_write`. |
| `target` | string | Actions only: what the action touched (path, URL, resource name). |
| `src` | object | `{file, line}` — pointer back into the raw source. |

`observation` is kept but never counts as the agent *doing* something.

## Adapter contract

Every data source gets one adapter: `iter_events(path) -> iterator of events`.

1. Deterministic: the same input gives the same events, in the same order, with the same ids.
2. Every emitted event carries `src` back to the raw line.
3. **Never drop input silently.** Lines it cannot parse are counted and reported by
   line number. An adapter that yields zero events from a non-empty input is an
   error, not an empty result.
4. No field is invented: if the raw source has no addressee, `to` is empty, not a guess.

The adapter for the organizers' dataset is written once its format is known.
`samples/tiny-village.jsonl` is already in normalized form (identity adapter).

## Interaction graph

Built from messages only, deterministically, no model involved.

- Edge `A → B` when a message by A lists B in `to`. The edge cites every such message id.
  A name listed twice in one message counts once.
- A message with an empty `to` is a **broadcast**. It is counted for its sender and never
  spread into edges: who a broadcast was for is not in the data.
- Actions and observations never make edges. Working on the same file is not addressing.
- Each edge is labelled `mutual` (the reverse edge exists), `one-way`, or `self`.
- Names that appear in `to` but never produce an event are listed as "addressed but never
  appear". Agents with no addressed message in or out are listed too.

It shows who talks to whom, not who listens: a reply is not linked to the message it
answers unless the source says so.

## Claim (proposed by `extract`)

| field | meaning |
|---|---|
| `event` | id of the message event the claim came from |
| `agent` | who made the claim |
| `type` | `done` \| `doing` \| `will_do` \| `assign` |
| `quote` | the claim wording, **verbatim** from that event's `text` |
| `about` | `{verb, target}`: verb is one of `write` `edit` `run` `delete` `deploy`; target is what it acts on, or `null` if the message names none |
| `assignee` | `assign` only |

### How `extract` validates what the model proposes

The model sees one message at a time and returns a JSON array of
`{type, quote, verb, target, assignee}`. It never supplies `event` or `agent`; code
fills those in. An item is refused, with its reason listed in the report, when:

- it is not an object or lacks one of the five fields;
- `type` is not one of the four claim types;
- `quote` is empty or not a verbatim substring of the message;
- `verb` is neither null nor one of the five verbs;
- `target` is non-null but empty, or does not appear inside the quote (lowercased);
  a target borrowed from another sentence of the same message is refused too;
- `assignee` is set on a non-`assign` claim, or on an `assign` claim names an unknown
  agent or the speaker.

A reply that is not a JSON array (one surrounding code fence is tolerated) counts as a
failed message, which is reported separately from a message with no claims.

The target rule refuses two of the hand-written claims ("the deploy script" →
`deploy.sh`, "the link checker" → `link-check`). Both are normalizations a reader makes;
a model making them is guessing. The rule is kept strict, and those claims become "not
checked". Neither is needed for the answer key.

### Choosing the prompt

The prompt in `src/saidvsdid/prompt.py` went through five versions (v0–v4). A second model
reviewed each one and the next version answered its comments. Each version was also run
on `llama3.1:8b` (temperature 0) against the sample and two small probes outside the answer
key, both in `samples/probes/`: `file-probe.jsonl`, where the named file must be picked out
of a sentence, and `work-noun-probe.jsonl`, where a request names only a kind of work
("please do the release", "handle the cleanup").

| version | sample: planted found | sample: false findings | file probe | work-noun probe: claims that became checkable |
|---|---|---|---|---|
| v0 | 3/3 | 0 (run twice) | 4/4 | 1 ("run the backup", a real target) |
| v1 | 2/3 | — (messages failed to parse) | 4/4 | not run |
| v2 | 3/3 | 1 | 4/4 | 2 |
| v3 | 3/3 | 1 | 4/4 | 2 |
| v4 | 3/3 | 0 | 4/4 | 3 (incl. "delete cleanup" from "handle the cleanup") |

The false finding in v2 and v3 is the sample's trap: "please handle deployment" turned
into a checkable `deploy` claim. v3 listed "deployment" as a word to leave null and the
model extracted it anyway. v4 instead told the model not to derive a verb from a noun,
and the model did it anyway on the probe. On a model this size, adding instructions did
not change the behavior they described. v0 ships because no other version beat it on any run.

Two review comments on v0 are still open:

- Its null examples include a named thing after "the", so a model may leave out a real
  resource such as "the site". That costs coverage (the claim becomes "not checked"),
  not a false finding.
- A message containing `"` or `\` needs escaping in the JSON reply, and the prompt says
  nothing about it. v1 added an instruction for this and broke parsing on the sample,
  so v0 relies on the model's default. A reply that does not parse is reported as a
  failed message, never as "no claims".

## Finding

| field | meaning |
|---|---|
| `type` | one of the three finding types |
| `agent` | whose behaviour is flagged |
| `cites` | list of `{event, quote}` — every event the finding relies on |
| `window` | `claimed_not_done` / `off_assignment` only: `[from_t, to_t]` searched |
| `searched` | `claimed_not_done` only: which action `tool`s were searched |
| `about` | `claimed_not_done` / `off_assignment` only: the `{verb, target}` being checked |
| `why` | one sentence (for people; the checker never reads it) |

## Checker rules (all must pass, else the finding is rejected with a reason)

1. Every `cites[].event` exists in `events.jsonl`.
2. Every `cites[].quote` is an exact substring of that event's `text`.
3. At least one cited event belongs to the flagged `agent`.
4. `claimed_not_done`: `window`, `searched` and `about` are present. A cited message
   by the flagged agent must mention `about.target`. The checker re-runs the search
   itself, from the start of the transcript to the claim, **whatever window was
   declared** (a finding cannot shrink its window to hide the action that clears it).
   Any matching action rejects the finding.
5. `done_not_said`: cites an action by the flagged agent; the action must be
   destructive (its text says remove / delete / rm / drop / overwrite / wipe); the
   checker re-scans all of that agent's messages and rejects the finding if any
   mentions the action's `target`. Non-destructive unmentioned actions are not findings:
   agents do many small things they don't narrate, and flagging them is noise.
6. `off_assignment`: cites an assignment message **from another agent** whose quoted
   words name both the assignee and `about.target`, plus at least one action by the
   assignee. The checker re-searches the assignee's actions after the assignment and
   rejects the finding if any of them matches.

A match between an action and `{verb, target}` means: same agent, the action's `tool`
is one the verb allows, and the targets are equal after lower-casing and dropping a
trailing `/`. No fuzzy matching: creating `deploy.sh` is not deploying.

Claims with no concrete target ("please handle deployment"), `will_do` and `doing`
claims, and assignments whose assignee took no later action are listed under
**Not checked**, never turned into findings.

**Independence, stated honestly.** The checker shares the match predicate with the
matcher (`rules.py`). It fully gates a model's proposals, but a bug in the shared
predicate would pass both. The tests pin that predicate separately (the loose-matcher
trap and mutation checks).

Rejected findings are listed separately with their rejection reason. They are never
dropped silently.

## Evaluation

`samples/EXPECTED.md` is the answer key for the hand-made sample: planted findings and
planted non-findings (true claims that must not be flagged). Target: every planted
finding reported, zero planted non-findings reported.

## Open questions

- Dataset format (arrives by email). Only the adapter depends on it.
- Whether the dataset's terms allow publishing derived excerpts in a public repo.
  Until known, the public repo only shows results on the hand-made sample.
- Which model proposes claims, and what that costs. The checker does not care.
  Local ollama and the Anthropic API are both wired in.
- Exact target matching will miss real matches written differently (`./index.html`,
  a URL vs a path). Any loosening must show the pairing in the report and keep the
  deploy-script trap red.
