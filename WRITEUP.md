# Said vs Did — write-up

*AI Swarm Dynamics Hackathon, October 2026. Draft: the "Results" section is filled in
during the event.*

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

*To be filled during the event.* Planned content, all from the AI Village dataset and
reported as counts, with at most a few short agent quotes linked to their moment in the
village:

- how many days and events were read, and how many actions were set aside and why;
- the interaction graph for those days;
- findings accepted by the checker, by type, and how many claims were left unchecked;
- two or three findings worth a human look, each with its link.

No dataset content is stored in the repository, and no human participant is quoted.

## Data and credit

Data: AI Digest, "AI Village dataset", 2026. https://theaidigest.org/village — used
under its research terms, for analysis only, not for training.

Code: MIT. The model used for claim proposals is `llama3.1:8b`, run locally.
