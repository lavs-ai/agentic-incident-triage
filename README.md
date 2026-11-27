# Agentic Incident Triage

An incident-triage agent that reads cloud alerts, classifies severity,
matches a runbook, and drafts a safe action plan — then **stops and waits
for a human to approve it**. It never acts on its own.

Built by Lavanya Bangari as an interview portfolio project. It is a demo:
it runs on sample alerts with a rule-based model adapter, no live model
call, no API key, and no cloud account. A real model (AWS Bedrock or
OpenAI) is pluggable through one interface function and an environment
variable.

## The agentic loop

```
Alert (CloudWatch / Azure Monitor)
  -> Triage agent
    -> Classify severity (rules, not the model)
    -> Runbook lookup
    -> Draft plan (model drafts text only)
  -> Human approval  <-- the gate: nothing runs without this
  -> Action (allow-list only, logged)
  -> Post-incident note (audit log)
```

The model is deliberately kept in a small box: it drafts plan text.
Severity, runbook choice, and every action are decided by rules and by
a human. That is the whole point of the design, and it is what the
interview notes in `docs/INTERVIEW.md` are about.

## Guardrails

- **Approval gate.** `execute_action()` denies any action without
  `approved=True` *and* a named approver. The denial is written to the
  audit log, so a blocked action is visible, not silent.
- **Allow-list of actions.** Each runbook lists `safe_actions` and
  `unsafe_actions`. An action that is not on the allow-list is denied
  even when a human approves it — approval cannot authorise a
  destructive action. `delete_backup`, `delete_database` and
  `run_migrations` are never executable through this agent.
- **Cost cap.** Any real model call is capped at $0.05 per invocation
  (`MODEL_COST_CAP_USD` in `app/agent.py`). The Bedrock and OpenAI
  adapters refuse to call a model without a cost estimate, rather than
  guessing.
- **Full audit log.** Every triage, approval, denial, and action is
  appended as one JSON line to `audit.log`, with a timestamp, alert id,
  action, approver, and result. An incident can be reconstructed from
  that file alone.
- **No secrets in code.** No API keys, no credentials. A real model
  needs credentials in the environment (`OPENAI_API_KEY`, or AWS
  credentials for Bedrock), never in a file in this repo.

## What is in this repo

| File | What it is |
| --- | --- |
| `app/agent.py` | The agent: alert parsing, severity classification, runbook matching, plan drafting, approval gate, audit logging, demo + real-model adapters, CLI |
| `app/runbooks.json` | 4 runbooks: high CPU, pod CrashLoopBackOff, failed deployment, backup restore failure |
| `app/sample_alerts.json` | 3 sample alerts (CloudWatch + Azure Monitor) to demo with |
| `app/test_agent.py` | Tests for classification, the approval gate, and the audit log |
| `docs/INTERVIEW.md` | 5 likely interview questions on agentic systems, with answers, plus 3 trade-offs |
| `docs/demo-script.md` | A 2-minute demo script to run in an interview |

## How to run the demo

Requires Python 3.10+ and nothing else. No packages to install.

```bash
# 1. Triage the 3 sample alerts (demo model, no network, no cost)
python3 -m app.agent

# 2. Triage one alert and approve one safe action for it
python3 -m app.agent --approve-one AL-1001 --action scale_replicas --approver lavanya

# 3. Run the tests
python3 -m pytest app/test_agent.py -v
# or, with no pytest installed:
python3 app/test_agent.py
```

Step 1 prints a triage and plan for each alert, and for each one shows
the gate check: an unapproved action is denied. Step 2 shows the same
action being executed once a named human approves it. The audit log is
written to `audit.log` in the directory you run it from — open it to
see every decision the agent made.

To use a real model instead of the demo adapter:

```bash
export AGENT_MODEL_PROVIDER=bedrock   # or: openai
export AGENT_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0
python3 -m app.agent
```

The adapters are in `app/agent.py` (`BedrockModelAdapter`,
`OpenAIModelAdapter`). They implement the same single method,
`draft_summary(alert, runbook)`, and they refuse to call a model
without a cost estimate — wire one in before using them in production.
Severity and actions are still decided by rules, never by the model.

## What it proves for an agentic-AI / Senior DevOps interview

- You can design an agent as a **system with guardrails**, not just
  a prompt: deterministic classification, a runbook as ground truth,
  an approval gate, an allow-list, and an audit trail.
- You know where a model belongs (drafting text) and where it does
  not (deciding severity, choosing actions, executing changes).
- You can reason about the failure modes that matter in production:
  hallucination, runaway cost, destructive actions, and silent
  failures — and you can show the code that prevents each one.
- You have worked across clouds: the sample alerts come from
  CloudWatch (AWS, ap-southeast-2) and Azure Monitor (australiaeast),
  and the runbooks are cloud-neutral.
