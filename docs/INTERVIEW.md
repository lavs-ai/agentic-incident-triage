# Interview notes — agentic incident triage

Short, strong answers for the questions this project is most likely to
prompt. Say them in your own words; the code in `app/agent.py` is the
evidence for each one.

## 1. "How do you stop an agent from doing something dangerous?"

Three layers, not one. First, the model never executes anything — it
only drafts plan text. Second, the approval gate in `execute_action()`
denies any action without a named human approver. Third, the runbook
allow-list: even an approved action is denied if it is not on the
runbook's `safe_actions`, and destructive actions such as
`delete_backup` and `run_migrations` are on an explicit unsafe list.
A denied action is written to the audit log, so it is visible, not
silent. Defence in depth, and every layer is testable — see
`test_approval_gate_blocks_unsafe_actions_even_when_approved`.

## 2. "What about hallucination — what if the model invents a step?"

Ground the model in the runbook. The runbook is the source of truth:
the plan is built from its checks and safe actions, and the model only
drafts the summary around them. Severity is not a model judgement at
all — it is a deterministic rule, so it is reproducible and auditable.
And because a human approves the plan before anything runs, a bad
draft is caught at the gate instead of in production. If the model
output ever disagreed with the runbook, the runbook would win.

## 3. "How do you control cost?"

Three ways. The demo adapter costs nothing — it is rule-based. A real
model is capped at $0.05 per invocation (`MODEL_COST_CAP_USD`), and
the Bedrock and OpenAI adapters refuse to call a model without a cost
estimate rather than guessing. And the agent is only invoked on an
alert, not polled continuously, so cost scales with incidents, not
with time. If I needed more, I would add a per-day budget and an
alert when it is approached, the same way I would for any AWS spend.

## 4. "How would you evaluate whether this agent is any good?"

Not by vibes — by a labelled set of past alerts. I would build a test
set of real (anonymised) alerts with the correct severity, runbook,
and action, then measure: severity accuracy, runbook match rate, and
— most importantly — the rate at which the gate blocked an unsafe or
unapproved action. I would also track the false-positive rate on
approvals, because an agent that cries wolf gets its approvals
rubber-stamped, which defeats the gate. The tests in this repo are
the start of that: they pin the classification rules and the gate
behaviour so a change cannot quietly weaken either.

## 5. "Why not just let the agent fix it automatically?"

Because the cost of a wrong automatic action in production — deleting
a backup, rolling back the wrong deployment, scaling the wrong service
— is far higher than the cost of waiting for a human to approve a
plan. The agent's job is to do the slow, boring part fast: read the
alert, find the runbook, draft the plan, and present it. The human's
job is the judgement call. That is also why the audit log matters:
if something does go wrong, you can reconstruct exactly who approved
what, and when.

## Trade-offs

1. **Rule-based severity vs model judgement.** Rules are
   reproducible and auditable, but they are brittle — a new alert
   type with no rule falls back to "escalate". A model would be more
   flexible, but its decisions would not be reproducible. I chose
   rules for severity and kept the model to drafting, because a wrong
   severity is the most expensive mistake this agent can make.

2. **Approval gate vs speed.** Requiring a human on every action
   slows response, especially out of hours. The mitigation is to make
   the plan so clear that approval is a 30-second decision, and to
   pre-approve a narrow set of read-only checks. I would not
   pre-approve writes without a long track record of correct plans.

3. **Demo adapter vs a real model.** The demo is free, offline, and
   deterministic — good for interviews and tests. A real model would
   write better summaries, but it adds cost, latency, a network
   dependency, and a new failure mode. The interface is one function
   (`draft_summary`), so swapping adapters is cheap and the rest of
   the system does not change.
