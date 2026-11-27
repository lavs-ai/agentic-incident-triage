"""Agentic incident triage — demo agent with a human approval gate.

The agent never acts on its own. It reads an alert, classifies severity,
matches a runbook, drafts a safe action plan, and then stops. An action is
only executed when a human approves it, the action is on the runbook's
allow-list, and the approval is written to the audit log.

Demo mode (default) uses a rule-based model adapter: no live model call,
no API key, no network. A real model is pluggable through
``AGENT_MODEL_PROVIDER`` (``bedrock`` or ``openai``) and a single interface
function, :func:`get_model_adapter`. The model is only ever used to draft
plan text — it never decides severity, never picks an action, and never
executes anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNBOOKS_PATH = HERE / "runbooks.json"
SAMPLE_ALERTS_PATH = HERE / "sample_alerts.json"

# Hard cost cap for any real model call, in USD per invocation. The demo
# adapter spends nothing; a real adapter must refuse to call the model if
# the estimated cost would exceed this.
MODEL_COST_CAP_USD = 0.05

SEVERITY_ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}

# P1 keywords are checked first: these are never downgraded by the
# rule-based classifier, whatever the metrics say.
P1_KEYWORDS = (
    "backup",
    "restore fail",
    "data loss",
    "database down",
    "down",
    "outage",
    "unavailable",
    "security breach",
    "breach",
)


@dataclass
class Alert:
    """A single cloud alert, as received from a monitoring source."""

    id: str
    title: str
    message: str
    service: str
    source: str = "unknown"
    environment: str = "production"
    region: str = ""
    alert_type: str = ""
    metrics: dict = field(default_factory=dict)
    timestamp: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Alert":
        return cls(
            id=str(data.get("id", "unknown")),
            title=str(data.get("title", "")),
            message=str(data.get("message", "")),
            service=str(data.get("service", "")),
            source=str(data.get("source", "unknown")),
            environment=str(data.get("environment", "production")),
            region=str(data.get("region", "")),
            alert_type=str(data.get("alert_type", "")),
            metrics=dict(data.get("metrics") or {}),
            timestamp=str(data.get("timestamp", "")),
        )


@dataclass
class Runbook:
    id: str
    name: str
    description: str
    checks: list
    safe_actions: list
    unsafe_actions: list
    severity_default: str
    escalate_if: str
    rollback: str
    match_alert_types: list = field(default_factory=list)
    match_keywords: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Runbook":
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            checks=list(data.get("checks", [])),
            safe_actions=list(data.get("safe_actions", [])),
            unsafe_actions=list(data.get("unsafe_actions", [])),
            severity_default=data.get("severity_default", "P3"),
            escalate_if=data.get("escalate_if", ""),
            rollback=data.get("rollback", ""),
            match_alert_types=list(data.get("match_alert_types", [])),
            match_keywords=list(data.get("match_keywords", [])),
        )


@dataclass
class Triage:
    """The agent's output for one alert: classification, runbook, plan."""

    alert_id: str
    severity: str
    runbook_id: str | None
    summary: str
    plan: list
    requires_approval: bool = True
    model_used: str = "demo"


def load_runbooks(path: Path = RUNBOOKS_PATH) -> list[Runbook]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [Runbook.from_dict(item) for item in data["runbooks"]]


def load_sample_alerts(path: Path = SAMPLE_ALERTS_PATH) -> list[Alert]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [Alert.from_dict(item) for item in data["alerts"]]


def classify_severity(alert: Alert, runbook: Runbook | None = None) -> str:
    """Rule-based severity. Deterministic, and deliberately conservative.

    P1: backup/restore failure, outage, data loss, or a P1 runbook.
    P2: crashloop / failed deployment / sustained high CPU in production.
    P3: anything else in production, or a P2 issue outside production.
    P4: non-production and low impact.
    The model is never allowed to change severity — that is a rule, not
    a judgement call, so it is reproducible and auditable.
    """
    text = f"{alert.title} {alert.message}".lower()
    if any(word in text for word in P1_KEYWORDS):
        return "P1"
    if runbook is not None and runbook.severity_default == "P1":
        return "P1"
    if alert.environment != "production":
        return "P4"
    cpu = alert.metrics.get("cpu_percent")
    if isinstance(cpu, (int, float)) and cpu >= 90:
        return "P2"
    if alert.alert_type in ("pod_crashloop", "failed_deployment", "high_cpu"):
        return "P2"
    if runbook is not None:
        return runbook.severity_default
    return "P3"


def match_runbook(alert: Alert, runbooks: list[Runbook]) -> Runbook | None:
    """Match by alert type first, then by keyword in the alert text."""
    for runbook in runbooks:
        if alert.alert_type and alert.alert_type in runbook.match_alert_types:
            return runbook
    text = f"{alert.title} {alert.message}".lower()
    for runbook in runbooks:
        if any(keyword in text for keyword in runbook.match_keywords):
            return runbook
    return None


# ---------------------------------------------------------------------------
# Model adapters — the single interface a real model plugs into.
# A model adapter only drafts plan text. It receives the alert and the
# matched runbook, and returns a short summary string.
# ---------------------------------------------------------------------------

class ModelAdapter:
    """Interface for a plan-drafting model. Implement ``draft_summary``."""

    name = "base"

    def draft_summary(self, alert: Alert, runbook: Runbook | None) -> str:
        raise NotImplementedError


class DemoModelAdapter(ModelAdapter):
    """Rule-based adapter used for the demo. No network, no cost."""

    name = "demo"

    def draft_summary(self, alert: Alert, runbook: Runbook | None) -> str:
        if runbook is None:
            return (
                f"{alert.service}: no matching runbook found. "
                "Do not act — escalate to a human and open an incident."
            )
        return (
            f"{alert.service}: {runbook.name}. "
            f"{runbook.description} "
            f"Escalate if: {runbook.escalate_if}"
        )


class BedrockModelAdapter(ModelAdapter):
    """AWS Bedrock adapter. Requires boto3 and AWS credentials in the
    environment; the model id comes from AGENT_MODEL_ID. Refuses to call
    the model if the estimated cost would exceed MODEL_COST_CAP_USD —
    callers must supply an estimate, and this adapter has no estimate,
    so it raises rather than guessing. Wire in your own cost estimate
    before using it in production."""

    name = "bedrock"

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or os.environ.get(
            "AGENT_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"
        )

    def draft_summary(self, alert: Alert, runbook: Runbook | None) -> str:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - needs boto3
            raise RuntimeError(
                "boto3 is not installed; install it to use the Bedrock adapter"
            ) from exc
        # Cost guard: without a real cost estimate, do not call the model.
        raise RuntimeError(
            "Bedrock adapter is wired for the interface but has no cost "
            "estimate for this call, so it refuses to call the model "
            f"(cap: ${MODEL_COST_CAP_USD:.2f} per call). Supply an estimate "
            "in draft_summary before enabling it."
        )


class OpenAIModelAdapter(ModelAdapter):
    """OpenAI adapter. Requires the openai package and OPENAI_API_KEY.
    Same cost guard as the Bedrock adapter: no estimate, no call."""

    name = "openai"

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or os.environ.get(
            "AGENT_MODEL_ID", "gpt-4o-mini"
        )

    def draft_summary(self, alert: Alert, runbook: Runbook | None) -> str:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set; the OpenAI adapter will not run "
                "without it. Never put a key in code."
            )
        raise RuntimeError(
            "OpenAI adapter is wired for the interface but has no cost "
            "estimate for this call, so it refuses to call the model "
            f"(cap: ${MODEL_COST_CAP_USD:.2f} per call). Supply an estimate "
            "in draft_summary before enabling it."
        )


def get_model_adapter() -> ModelAdapter:
    """Return the adapter selected by AGENT_MODEL_PROVIDER.

    Unset (or ``demo``) -> DemoModelAdapter. ``bedrock`` / ``openai`` ->
    the matching adapter. Anything else raises, so a typo can never
    silently fall back to a live model.
    """
    provider = os.environ.get("AGENT_MODEL_PROVIDER", "demo").lower()
    if provider in ("", "demo"):
        return DemoModelAdapter()
    if provider == "bedrock":
        return BedrockModelAdapter()
    if provider == "openai":
        return OpenAIModelAdapter()
    raise ValueError(f"Unknown AGENT_MODEL_PROVIDER: {provider!r}")


# ---------------------------------------------------------------------------
# Triage, approval gate, and audit log.
# ---------------------------------------------------------------------------

def triage_alert(
    alert: Alert,
    runbooks: list[Runbook],
    model: ModelAdapter | None = None,
) -> Triage:
    """Classify, match a runbook, and draft a plan. No action is taken."""
    model = model or get_model_adapter()
    runbook = match_runbook(alert, runbooks)
    severity = classify_severity(alert, runbook)
    summary = model.draft_summary(alert, runbook)
    plan = []
    if runbook is not None:
        plan = [f"Check: {check}" for check in runbook.checks]
        plan.append(f"Safe actions (approval required): {', '.join(runbook.safe_actions)}")
        plan.append(f"Rollback: {runbook.rollback}")
        plan.append(f"Do not do: {', '.join(runbook.unsafe_actions)}")
    else:
        plan = ["No matching runbook — escalate to a human. Do not act."]
    return Triage(
        alert_id=alert.id,
        severity=severity,
        runbook_id=runbook.id if runbook else None,
        summary=summary,
        plan=plan,
        requires_approval=True,
        model_used=model.name,
    )


def write_audit_log(
    event: dict, log_path: Path | str = "audit.log"
) -> dict:
    """Append one JSON line to the audit log and return the event.

    Every triage, approval, denial, and action goes through here, so the
    full history of an incident can be reconstructed from the file.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def execute_action(
    action: str,
    triage: Triage,
    runbooks: list[Runbook],
    approved: bool = False,
    approver: str = "",
    log_path: Path | str = "audit.log",
) -> dict:
    """Execute one action, only if the approval gate passes.

    The gate is three checks, in order:
    1. A human approved this specific action (approved=True + approver).
    2. The action is on the matched runbook's allow-list.
    3. The action is not on the runbook's unsafe list.

    Anything that fails is denied and logged — it is never executed.
    This function does not call any cloud API; it returns a record of
    what *would* be executed, so the demo is safe to run anywhere.
    """
    runbook = next((r for r in runbooks if r.id == triage.runbook_id), None)
    event = {
        "event": "action",
        "alert_id": triage.alert_id,
        "action": action,
        "approved": approved,
        "approver": approver,
        "severity": triage.severity,
    }
    if not approved or not approver:
        event.update({"result": "denied", "reason": "no human approval"})
        write_audit_log(event, log_path)
        return event
    if runbook is None:
        event.update({"result": "denied", "reason": "no matching runbook"})
        write_audit_log(event, log_path)
        return event
    if action in runbook.unsafe_actions:
        event.update({"result": "denied", "reason": "action is on the unsafe list"})
        write_audit_log(event, log_path)
        return event
    if action not in runbook.safe_actions:
        event.update(
            {"result": "denied", "reason": "action is not on the runbook allow-list"}
        )
        write_audit_log(event, log_path)
        return event
    event.update({"result": "executed (demo — no cloud call made)"})
    write_audit_log(event, log_path)
    return event


def format_triage(triage: Triage) -> str:
    lines = [
        f"[{triage.severity}] {triage.alert_id} — runbook: {triage.runbook_id or 'none'} "
        f"(model: {triage.model_used})",
        f"  Summary: {triage.summary}",
        "  Plan:",
    ]
    lines.extend(f"    - {step}" for step in triage.plan)
    lines.append("  Approval: required — no action has been taken.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agentic incident triage (demo)")
    parser.add_argument(
        "--alerts",
        type=Path,
        default=SAMPLE_ALERTS_PATH,
        help="JSON file of alerts to triage",
    )
    parser.add_argument(
        "--runbooks", type=Path, default=RUNBOOKS_PATH, help="Runbooks JSON file"
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("audit.log"),
        help="Audit log path (JSONL)",
    )
    parser.add_argument(
        "--approve-one",
        help="Alert id to approve one safe action for, e.g. AL-1001",
    )
    parser.add_argument(
        "--action",
        default="scale_replicas",
        help="Action to approve for --approve-one (must be on the allow-list)",
    )
    parser.add_argument("--approver", default="", help="Name of the approver")
    args = parser.parse_args(argv)

    runbooks = load_runbooks(args.runbooks)
    alerts = load_sample_alerts(args.alerts)
    model = get_model_adapter()

    for alert in alerts:
        triage = triage_alert(alert, runbooks, model)
        print(format_triage(triage))
        print()
        write_audit_log(
            {
                "event": "triage",
                "alert_id": alert.id,
                "severity": triage.severity,
                "runbook_id": triage.runbook_id,
                "model": triage.model_used,
            },
            args.log,
        )
        # Demonstrate the gate: an unapproved action is always denied.
        if triage.runbook_id:
            runbook = next(r for r in runbooks if r.id == triage.runbook_id)
            denied = execute_action(
                runbook.safe_actions[0], triage, runbooks,
                approved=False, log_path=args.log,
            )
            print(f"  Gate check (no approval): {denied['result']} — {denied['reason']}")
        print()
        if args.approve_one == alert.id:
            if not args.approver:
                print("  --approver is required to approve an action.")
                return 2
            result = execute_action(
                args.action, triage, runbooks,
                approved=True, approver=args.approver, log_path=args.log,
            )
            print(f"  Approved by {args.approver}: {result['result']}")
            if result["result"].startswith("denied"):
                print(f"    Reason: {result['reason']}")
    print(f"Audit log written to {args.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
