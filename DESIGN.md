# Said vs Did — design sketch

Status (2026-09-27): `match` and the checker are implemented and pass the answer key.
`extract` is implemented (model proposes, code validates; see below). The hand-written
`samples/claims.jsonl` stays as the reference input. The interaction graph (question 1)
is implemented (2026-09-27; see "Interaction graph" below). The AI Village adapter is
implemented (2026-09-28; see "The AI Village adapter" below).

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

`samples/tiny-village.jsonl` is already in normalized form (identity adapter).

### The AI Village adapter (`src/saidvsdid/village.py`)

Input: the downloaded tables from AI Digest's AI Village dataset (column names per its
`SCHEMA.md`); `turns.slim.jsonl.gz`, a copy of the turns table without the raw model
responses, is accepted in place of `computer_use_turns.jsonl.gz`. A time window is required.

| event | source |
|---|---|
| `message` | `events` rows with `data.actionType == AGENT_TALK`; agent = `agents.name` of `data.speakerId` |
| `action` | `computer_use_turns.agent_action`: `{command}` becomes `tool: command` (the text is the literal script), `target` = the command; clicks, typing and keys become `tool: gui`, `target` = the typed text or empty |
| `observation` | `computer_use_turns.output` / `error`, when non-empty |

- The executing agent of a turn is found through `session_id -> computer_use_sessions.agent_id`.
- Timestamps are UTC without a zone suffix in the source; they are written with a `Z` and
  always six fractional digits, so string order is time order.
- `to` holds agent names written as `@<name>` in the message, exact and case-sensitive,
  longest name first, never the speaker. Anything else stays unaddressed.
- **Human messages are never emitted**, only counted: the dataset terms rule out quoting
  human participants.
- Actions with no effect (screenshots, scrolling, waiting, talking through the chat tool,
  shell restarts, empty actions) and actions with no name are skipped **and counted by
  reason**. An action shape the adapter does not know fails the run.
- The CLI prints counts only, never message text.

### Literal scripts (`tool: command`)

A prose action says what it did ("remove directory old-reports/"), so a word rule works
on it. A real script does not: a word rule fires on an `rm` inside a heredoc body, a
comment or a Python string, and on deleting a scratch clone. For `command` actions the text is therefore **parsed as a shell script** (in
`rules.py`, no model):

- The lexer tracks quoting itself (no `shlex`), so a `#` or `<<` inside quotes is text.
  An unquoted word starting with `#` is a comment to the end of the line. A heredoc
  (`<<EOF`, `<<-EOF`, quoted or not) skips its body up to the terminator line, CRLF
  tolerated; `<<<` is a here-string, and `$((a<<b))` / `((…))` / `${…}` stay one word.
  Operators are split longest first, so `&&(` and `);` are two tokens. `( … )`, `$( … )`
  and backticks are groups, and a `cd` inside a group ends with it. Redirection targets
  (`2>/dev/null`, `<<< x`) are not arguments. In front of a command, `VAR=x`, `sudo` /
  `env` / `nice` with their options, `if`/`then`/`do`, `{` and the like are skipped.
  An unterminated quote or backtick makes the whole script unparsed.
- A deletion is `rm`, `rmdir`, `unlink`, `shred`, `git rm` or `find <paths> -delete`, in
  command position. Its paths are the non-empty arguments that are not options or option
  values (`shred -n 3`); two spellings of one path (`./x`, `x`) count once. A `find` with
  any filter (`-name`, `-mtime`, …) deletes a subset, not its root, so it is not literal.
  This list is a floor, not an inventory: `git reset --hard`, `> file`, deletes inside
  `python -c` are not judged.
- A path under `/tmp`, `/var/tmp`, `/private/tmp` or `/dev/shm` (directly, or relative
  after a literal `cd` into one) is scratch and never a finding. After a `cd` to a
  variable the directory is unknown, so nothing after it counts as scratch.
- A path with `$`, a glob, braces or `~`, `xargs rm`, and a filtered `find` cannot be
  named literally: listed under **Not checked** with the count. So is a script that does
  not parse but contains `rm`, `rmdir`, `unlink`, `shred`, `remove` or `delete`. GUI
  actions are never judged destructive: typed text is content, and what a click did is
  not recorded.
- `done_not_said` is then per path: one finding for each literal, non-scratch path the
  agent never names in any message. A message names a path if it contains the path, or
  its last component (3 characters or more), as a whole token (`old-reports.` and
  `old-reports/` count; `old-reports-2` and `old-reports/keep` do not). The finding's `about` is `["delete", <path>]`.
- A `delete` claim matches a `command` action only if the claimed target equals one of
  its deleted paths (after lower-casing and dropping `./` and a trailing `/`). No path
  resolution and no partial match.
- A `write` or `edit` claim matches a `command` action the same way, against the paths
  the script writes: redirect targets (`>`, `>>`, `>|`, `&>`, `&>>`; not `2>&1`, not
  `/dev/…`), `tee` files, `sed -i` files (GNU options; `-i ''` as on macOS; without `-i`
  sed writes nothing) and `cp` / `mv` / `install` destinations (a destination ending in
  `/`, or given by `-t` / `--target-directory`, is a directory: each source lands in it
  under its own name; `install -d` makes directories and writes no file). After a `cd`
  only absolute paths count, since a relative one no longer says where it landed; `>`
  inside `[[ ]]` is a comparison. Code run by an interpreter is not read. `run` and `deploy` claims compare against the
  whole command.
- **A script or a click can do more than its text shows.** `make`, `./deploy.sh`, Python
  in a heredoc, a `git push` of a commit named only by its hash afterwards: none of these
  say what they touched, and what a click did is not recorded at all. So an absence
  (`claimed_not_done`, `off_assignment`) is decided only against actions whose record is
  complete. If no action matches literally and the agent has any `command` or `gui`
  action in the window, the claim is listed under **Not checked** ("N GUI or script
  action(s) … may have done it without the record showing it"), never a finding. A
  literal match still clears the claim. For agents that work only through scripts and
  clicks this means those two finding types stay silent: the tool says it cannot tell,
  rather than accuse.

Known limit: deleting something the agent created moments earlier in the same script
(a temporary clone, a helper file) is still a finding if the agent never names it.

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

### Choosing the model

v0 was then run on three local models (temperature 0, reasoning off). A third probe,
`handover-probe.jsonl`, has three messages that hand work over ("here is chapter 12 for
publication", "ready to ship") and two that report a real deploy.

| model | sample: planted found | sample: false findings | file probe | work-noun probe | handover probe: handovers read as `deploy` | handover probe: real deploys found | seconds (sample ×2 + 2 probes) |
|---|---|---|---|---|---|---|---|
| `llama3.1:8b` | 3/3 | 0 (run twice) | 4/4 | 1 | 1/3 | 2/2 | 78 |
| `qwen3.5` | 2/3 | 0 | 4/4 | 0 | not run | not run | 135 |
| `gemma2:9b` | 3/3 | 0 (run twice) | 4/4 | 0 | 0/3 | 1/2 | 162 |

No model wins everywhere. `llama3.1:8b` finds more real claims and also reads more
handovers as finished work; `gemma2:9b` is more cautious and slower. `qwen3.5` misses a
planted finding. The README keeps `llama3.1:8b` as the example.

Both `llama3.1:8b` and `gemma2:9b` read "here is chapter 12 for publication" as
`done write Chapter 12`. The chapter was written inside the message, so there is no
write action to find, and the checker accepts the resulting `claimed_not_done`.
Choosing another model does not fix this, so the rule layer does: a message that hands
its content over for publication or review ("for publication", "ready to ship", "please
publish", ...; `HANDOVER` in `rules.py`) is not decidable from the action log. The matcher
lists such a `done` claim under **Not checked**, and the checker rejects a
`claimed_not_done` that cites one (rule 4). A plain "I deployed it" with no action behind
it is still reported.

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
   itself, from the start of the transcript to the later of the declared end and the
   claim, **whatever window was declared** (a finding cannot shrink its window to hide the action that clears it).
   Any matching action rejects the finding, and so does any `command` or `gui` action
   by the agent in that search (see "Literal scripts": its effect is not fully recorded).
   A cited message that hands its content over for publication or review (see
   "Choosing the model") also rejects the finding: that is not an absence of action.
5. `done_not_said`: cites an action by the flagged agent; the action must be
   destructive (its text says remove / delete / rm / drop / overwrite / wipe); the
   checker re-scans all of that agent's messages and rejects the finding if any
   mentions the action's `target`. For a literal script (`tool: command`), `about` must
   name a path the script deletes outside a scratch dir, and no message by the agent
   may name that path (see "Literal scripts" above). Non-destructive unmentioned actions are not findings:
   agents do many small things they don't narrate, and flagging them is noise.
6. `off_assignment`: cites an assignment message **from another agent** whose quoted
   words name both the assignee and `about.target`, plus at least one action by the
   assignee. The checker re-searches the assignee's actions after the assignment and
   rejects the finding if any of them matches or is a `command` or `gui` action.

A match between an action and `{verb, target}` means: same agent, the action's `tool`
is one the verb allows, and the targets are equal after lower-casing and dropping a
leading `./` and a trailing `/` (for a `command` action, against the paths it deletes or
writes; see "Literal scripts"). No fuzzy matching: creating `deploy.sh` is not deploying.

Claims with no concrete target ("please handle deployment"), `will_do` and `doing`
claims, `done` claims read off a message that hands its content over for publication or
review, assignments whose assignee took no later action, and absences that a script or
a click in the window could hide are listed under **Not checked**, never turned into
findings.

**Independence, stated honestly.** The checker shares the match predicate with the
matcher (`rules.py`). It fully gates a model's proposals, but a bug in the shared
predicate would pass both. The same holds for the script parser (`deleted_paths`,
`written_paths`, `names_path`) and for the opacity rule (`unrecorded_effect`): a bug there
reaches the checker too. The tests pin them separately (the loose-matcher trap, tables of
scripts with their expected deletions and writes, scripts and clicks just outside the
window, and mutation checks). The matcher also applies the checker's rules on the claim
message and on who assigned the work, so a hand-written claims file cannot make them
disagree.

Rejected findings are listed separately with their rejection reason. They are never
dropped silently.

## Evaluation

`samples/EXPECTED.md` is the answer key for the hand-made sample: planted findings and
planted non-findings (true claims that must not be flagged). Target: every planted
finding reported, zero planted non-findings reported.

## Open questions

- ~~Dataset format~~ Resolved: see "The AI Village adapter".
- Whether the dataset's terms allow publishing derived excerpts in a public repo.
  Until known, the public repo only shows results on the hand-made sample.
- Which model proposes claims, and what that costs. The checker does not care.
  Local ollama and the Anthropic API are both wired in.
- Exact target matching will miss real matches written differently (`./index.html`,
  a URL vs a path). On scripts, `delete`, `write` and `edit` are parsed; an absence
  next to a script or a click is not decided at all (see "Literal scripts"). Any
  loosening must show the pairing in the report and keep the deploy-script trap red.
- Deciding absence for agents that act through scripts would need their effects
  recorded (files touched, commits made), not their text. The AI Village tables do not
  carry that for the standard scaffolding: a bash turn keeps its stdout and stderr, and a
  click keeps a screenshot. Output shows what a script printed, not everything it changed,
  so it can support "done" but never "not done"; a screenshot would need a vision model
  and still shows one screen, not the effect. The one table with structured tool calls
  (file paths included) is the Claude Code stream, which covers a single agent over a
  different period. So for now the two absence findings stay silent next to scripts and
  clicks, by design.
