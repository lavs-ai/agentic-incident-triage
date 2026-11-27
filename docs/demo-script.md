# Demo script — 2 minutes

Run this in an interview. It needs Python 3.10+ and nothing else.
No network, no API key, no cloud account.

## Before you start

Say: "This is an incident-triage agent. It reads cloud alerts,
classifies them, drafts a plan from a runbook — and it never acts
without a human approving it. That's the whole design."

## 1. Triage the sample alerts (about 40 seconds)

```bash
python3 -m app.agent
```

Say, while it runs: "Three alerts — high CPU on CloudWatch, a pod in
CrashLoopBackOff on Azure Monitor, and a failed backup restore test.
The agent classifies the backup one as P1, because a backup you
cannot restore is the most serious thing here. Each one gets a
runbook and a plan. And notice the gate check after each one: an
unapproved action is denied."

Point at the output for the backup alert: "The plan says: preserve
all existing backups, do not delete anything. That's in the runbook,
not something the model invented."

## 2. Approve one safe action (about 30 seconds)

```bash
python3 -m app.agent --approve-one AL-1001 --action scale_replicas --approver lavanya
```

Say: "Same alert, same action — but this time a named human approved
it, so it goes through. The approval, the approver, and the result
are all in the audit log."

## 3. Show the audit log (about 20 seconds)

```bash
cat audit.log
```

Say: "Every triage, every denial, every approval is one JSON line
here. If something goes wrong at 3am, this file tells you exactly
who approved what."

## 4. Show the tests (about 30 seconds)

```bash
python3 -m pytest app/test_agent.py -v
# or: python3 app/test_agent.py
```

Say: "And the guardrails are tested — the gate blocks unapproved
actions, and it blocks unsafe actions even when they are approved.
That's the part I care most about: the agent is only as good as
the things it cannot do."

## If they ask "where is the model?"

Say: "The demo uses a rule-based adapter, so it runs offline. A real
model plugs in through one function — `draft_summary` in
`app/agent.py` — via `AGENT_MODEL_PROVIDER=bedrock` or `openai`.
The model only drafts text; severity and actions are still rules,
and the approval gate does not change."
