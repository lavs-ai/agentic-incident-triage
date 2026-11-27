"""Tests for the agentic incident-triage agent.

Run with:  python -m pytest app/test_agent.py -v
or plain:  python app/test_agent.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import (  # noqa: E402
    Alert,
    classify_severity,
    execute_action,
    get_model_adapter,
    load_runbooks,
    load_sample_alerts,
    match_runbook,
    triage_alert,
    write_audit_log,
)

RUNBOOKS = load_runbooks()
ALERTS = {a.id: a for a in load_sample_alerts()}


def test_backup_failure_is_p1():
    triage = triage_alert(ALERTS["AL-1003"], RUNBOOKS)
    assert triage.severity == "P1"
    assert triage.runbook_id == "backup-restore-failure"


def test_crashloop_is_p2_with_runbook():
    triage = triage_alert(ALERTS["AL-1002"], RUNBOOKS)
    assert triage.severity == "P2"
    assert triage.runbook_id == "pod-crashloop"


def test_high_cpu_is_p2_and_plan_lists_checks():
    triage = triage_alert(ALERTS["AL-1001"], RUNBOOKS)
    assert triage.severity == "P2"
    assert triage.runbook_id == "high-cpu"
    assert any("Check:" in step for step in triage.plan)
    assert triage.requires_approval is True


def test_non_production_alert_is_not_p1_or_p2():
    alert = Alert(
        id="AL-TEST",
        title="High CPU in dev",
        message="CPU above 92% for 20 minutes in dev",
        service="checkout-api",
        environment="dev",
        alert_type="high_cpu",
        metrics={"cpu_percent": 92},
    )
    assert classify_severity(alert) == "P4"


def test_no_runbook_means_escalate_and_no_action():
    alert = Alert(
        id="AL-UNKNOWN",
        title="Something odd happened",
        message="An alert with no known pattern",
        service="mystery-service",
        alert_type="something_new",
    )
    triage = triage_alert(alert, RUNBOOKS)
    assert triage.runbook_id is None
    assert "escalate" in triage.plan[0].lower()
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "audit.log"
        result = execute_action(
            "restart_pod", triage, RUNBOOKS, approved=True,
            approver="lavanya", log_path=log,
        )
        assert result["result"] == "denied"


def test_approval_gate_blocks_unapproved_actions():
    triage = triage_alert(ALERTS["AL-1001"], RUNBOOKS)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "audit.log"
        result = execute_action(
            "scale_replicas", triage, RUNBOOKS, approved=False, log_path=log
        )
        assert result["result"] == "denied"
        assert result["reason"] == "no human approval"


def test_approval_gate_blocks_unsafe_actions_even_when_approved():
    triage = triage_alert(ALERTS["AL-1003"], RUNBOOKS)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "audit.log"
        result = execute_action(
            "delete_backup", triage, RUNBOOKS,
            approved=True, approver="lavanya", log_path=log,
        )
        assert result["result"] == "denied"
        assert "unsafe" in result["reason"]


def test_approved_allow_listed_action_is_executed_and_logged():
    triage = triage_alert(ALERTS["AL-1001"], RUNBOOKS)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "audit.log"
        result = execute_action(
            "scale_replicas", triage, RUNBOOKS,
            approved=True, approver="lavanya", log_path=log,
        )
        assert result["result"].startswith("executed")
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["alert_id"] == "AL-1001"
        assert record["approver"] == "lavanya"
        assert record["action"] == "scale_replicas"


def test_audit_log_written():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "audit.log"
        write_audit_log({"event": "triage", "alert_id": "AL-1001"}, log)
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "triage"
        assert "timestamp" in record


def test_demo_model_is_default():
    os.environ.pop("AGENT_MODEL_PROVIDER", None)
    assert get_model_adapter().name == "demo"


def test_unknown_model_provider_raises():
    os.environ["AGENT_MODEL_PROVIDER"] = "not-a-provider"
    try:
        try:
            get_model_adapter()
        except ValueError:
            return
        raise AssertionError("expected ValueError for unknown provider")
    finally:
        os.environ.pop("AGENT_MODEL_PROVIDER", None)


def test_runbook_matching_by_keyword():
    alert = Alert(
        id="AL-KW",
        title="Restore failed",
        message="The restore test failed last night",
        service="billing-db",
        alert_type="",
    )
    runbook = match_runbook(alert, RUNBOOKS)
    assert runbook is not None
    assert runbook.id == "backup-restore-failure"


if __name__ == "__main__":
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001 - plain runner reports all
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    raise SystemExit(1 if failed else 0)
