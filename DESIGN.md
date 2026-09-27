# Said vs Did — design sketch

Status (2026-09-26): `match` and the checker are implemented and pass the answer key.
Not built yet: `extract` (for now `samples/claims.jsonl` is written by hand in its
place), the dataset adapter, and the interaction graph (question 1).

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

## Claim (proposed by `extract`)

| field | meaning |
|---|---|
| `event` | id of the message event the claim came from |
| `agent` | who made the claim |
| `type` | `done` \| `doing` \| `will_do` \| `assign` |
| `quote` | the claim wording, **verbatim** from that event's `text` |
| `about` | `{verb, target}`: verb is one of `write` `edit` `run` `delete` `deploy`; target is what it acts on, or `null` if the message names none |
| `assignee` | `assign` only |

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
- Exact target matching will miss real matches written differently (`./index.html`,
  a URL vs a path). Any loosening must show the pairing in the report and keep the
  deploy-script trap red.
