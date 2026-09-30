# Said vs Did — write-up

*AI Swarm Dynamics Hackathon, October 2026.*

## The question

When several agents work together, the transcript records two things: what each agent
says, and what it actually does. An overseer usually reads the first and trusts it.
Said vs Did lines the two up and reports where they disagree:

- an agent says it did something, and no matching action exists (`claimed_not_done`);
- an agent does something destructive and never mentions it (`done_not_said`);
- an agent is assigned X by another agent, then does only other things (`off_assignment`).

It also draws who addresses whom, so a reader can see the shape of the collaboration
before reading any finding.

## Why every finding carries its evidence

A tool that watches agents is only useful if its own reports can be checked. So every
finding cites the transcript events it rests on, and a deterministic checker, not a
model, decides whether it stands: quotes must be verbatim, the cited events must belong
to the flagged agent, and for "not done" the checker searches the whole transcript again
itself, so a finding cannot hide the action that would clear it. Findings that fail are
thrown out with the reason printed. `samples/bad-findings.jsonl` holds eight ways of
cheating; all eight are rejected.

A model is used in one place only: proposing which messages contain claims. Code decides
which event and agent a claim belongs to, and drops any claim whose quote is not verbatim
or whose target is not in the quote.

## What it will not say

The hardest part was deciding when to stay silent.

- **Scripts and clicks.** `./deploy.sh` can do anything, and a click leaves only a
  screenshot. When an agent acted through a script or the GUI and there is no literal
  match, the tool does not conclude "not done". It lists the claim as unchecked and names
  the actions it could not see through. For real shell commands it parses what is
  literally deleted or written (redirects, `tee`, `sed -i`, `cp`/`mv` targets); anything
  built from variables, globs or `xargs` is listed as unchecked, not guessed.
- **Hand-offs.** "Here is Chapter 12, please publish" is a hand-off, not a claim that
  something was published. Those messages are recognised and their claims set aside.
- **Addressees.** The dataset has no recipient field. A message counts as addressed only
  when it names another agent exactly; otherwise it is counted as a broadcast.

The cost is stated in the README: exact target matching misses the same file written two
ways, and the checker shares one matching predicate with the matcher, so tests and
mutation checks pin that predicate separately.

## Results

Three days of the AI Village, picked because they have the most agent messages: one from
the earlier period and two from after agents started working mostly through their own
computers. Each day was converted, claims were proposed by `llama3.1:8b`, and the report
covers that day only. For 2025-10-23 the day before was loaded too, so the checker could
search it, but nothing in it was reported. The two 2026 days have no events the day
before in the dataset, so they had no lookback.

| Day | Agents | Messages | Actions | Observations | Claims | Accepted | Rejected | Unchecked |
|---|---|---|---|---|---|---|---|---|
| 2025-10-23 | 8 | 1,191 | 957 | 396 | 1,744 | 0 | 0 | 1,741 |
| 2026-07-06 | 21 | 1,894 | 17,315 | 14,016 | 3,415 | 3 | 0 | 3,435 |
| 2026-07-20 | 26 | 1,419 | 13,651 | 12,459 | 2,571 | 6 | 0 | 2,625 |

Human messages are left out of the event stream and only counted. Actions with no
recorded effect are set aside and counted by kind: scrolls, pointer moves, screenshots,
waits, and each "send the reply back to chat" turn. The claim model gave no usable output
for 2, 44 and 24 messages on the three days, and those were reported as failures, not
skipped silently.

**Who addresses whom.** On 2025-10-23, 8 agents had 27 directed pairs; 1,152 messages
named no other agent. On 2026-07-06 it was 21 agents, 227 pairs and 698 broadcasts. On
2026-07-20 it was 26 agents, 156 pairs and 443 broadcasts. Most messages are still
broadcasts. The dataset has no recipient field, so a message counts as addressed only when
it names another agent exactly.

**What was found.** 9 findings, and the checker accepted all 9: 8 `done_not_said` and 1
`claimed_not_done`. I read each one against the raw events:

- **7 hold as defined.** An agent deleted files or a directory in a repository and
  never mentioned it in chat. On 2026-07-20 Kimi K2.6 deleted four files, three of them
  an old playbook and log templates it called obsolete. On 2026-07-06 DeepSeek-V3.2
  removed an embedded repository under `examples/` that another agent was working on
  that day. Claude Sonnet 5 deleted `es/style.css` and `fr/style.css`, and the command's
  own comment gives the reason: "Delete the now-redundant local stylesheet duplicates". In
  every case the command or commit message explains the deletion, and some look like an
  agent tidying old files. The tool cannot say whose files they were, because it only
  sees one day and the files were written before it. So these are items for a human to
  glance at, not accusations.
- **2 are false positives, and I list them as such.** The `claimed_not_done` comes from
  GPT-5.1 saying it had "noted on the verification sweep and S411 run". It was confirming
  another agent's run, not claiming its own. The quote names neither the speaker nor that
  agent, so the rule that drops claims about other agents did not catch it. The other is a
  `done_not_said` where GPT-5.5 moved a note out of a scratch directory and then removed
  it with `rmdir`, which only works on an empty directory. Nothing was lost.

**Unchecked is the large number, by design.** It counts actions as well as claims (for
example deletions the tool cannot resolve), so the columns do not add up to Claims. Most unchecked claims are about the future
("will do") or name no concrete action and target. The rest are mostly assignments whose
quote does not name both the agent and the target, deletions of paths built from
variables or globs, hand-offs, and claims where scripts or GUI actions sat in the window
with no literal match. For that last group the tool refuses to say "not done" (see
above). Every unchecked item is listed with its reason.

**What this run cannot see.** A pattern like `rm -rf X && git clone … X` is treated as
clearing a scratch directory and is not reported, even when the old `X` held someone
else's work. The deletion rule also ignores files the same agent wrote to earlier in
the window. That is right for its own drafts, but an in-place edit counts as writing too,
so deleting a file it had only edited is missed. On 2025-10-23, 812 of 957 actions were
GUI actions, so there were few literal file effects to check. Zero findings is the
expected result for that day, not evidence that nothing happened.

The rules were changed twice after reading findings back. An earlier version reported 24
`claimed_not_done` for 2025-10-23, and none held up. Most were status reports about what
*another* agent had done, and one was a claim made seconds after the slice began, checked
against an empty past. That led to the rule that a claim must be about the speaker's own
action, and to the lookback day. The next version reported 67 `done_not_said` on
2026-07-20, 59 of them one agent deleting its own chapter files after publishing, and 23
on 2026-07-06, mostly `rm -rf` of a directory followed at once by recreating it. That led to the rule that files
the same script recreates, or the agent itself wrote, are not reported.

No dataset content is stored in the repository, and no human participant is quoted.

## Data and credit

Data: AI Digest, "AI Village dataset", 2026. https://theaidigest.org/village — used
under its research terms, for analysis only, not for training.

Code: MIT. The model used for claim proposals is `llama3.1:8b`, run locally.
