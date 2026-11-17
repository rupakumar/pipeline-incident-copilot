import pytest

from pipeline_incident_copilot.interface.approval_server import ApprovalService
from pipeline_incident_copilot.notifications.approval_links import ApprovalLinks, InvalidLink
from pipeline_incident_copilot.notifications.base import EventKind, IncidentEvent
from pipeline_incident_copilot.notifications.html_templates import render_html
from pipeline_incident_copilot.schemas.agent_outputs import ProposedAction, Resolution, RootCause
from pipeline_incident_copilot.schemas.incident import FailedRun, IncidentStatus
from pipeline_incident_copilot.storage.database import connect
from pipeline_incident_copilot.storage.incident_repository import IncidentRepository

LINKS = ApprovalLinks(b"test-secret", "http://localhost:8787", ttl=60)


def test_links_are_signed_expiring_and_tamper_proof():
    token = LINKS.token("INC-0020", "approve", now=1000)
    assert LINKS.verify(token, now=1010) == {
        "incident_id": "INC-0020",
        "decision": "approve",
        "nonce": token and LINKS.verify(token, now=1010)["nonce"],
        "via": "email link",
        "who": "",
    }
    with pytest.raises(InvalidLink, match="expired"):
        LINKS.verify(token, now=2000)
    body, sig = token.split(".")
    forged = LINKS.token("INC-0099", "approve", now=1000).split(".")[0] + "." + sig
    with pytest.raises(InvalidLink, match="signature"):
        LINKS.verify(forged, now=1010)
    with pytest.raises(InvalidLink, match="signature"):
        ApprovalLinks(b"other-secret").verify(token, now=1010)


def _incident(tmp_path):
    repo = IncidentRepository(connect(tmp_path / "t.db"))
    run = FailedRun(
        job_id=1,
        job_name="loghub_etl",
        run_id=7,
        result_state="FAILED",
        failed_task="gold_aggregate",
        parameters={"source": "hdfs", "run_date": "2008-11-10", "gold_table": "samples.x.y"},
    )
    inc = repo.open(run)
    repo.set_status(inc.incident_id, IncidentStatus.AWAITING_APPROVAL)
    return repo, repo.get(inc.incident_id)


def _event(inc):
    res = Resolution(
        summary="Reset gold_table",
        steps=["reset it", "rerun"],
        risk="low",
        verify=["rerun succeeds"],
        action=ProposedAction(name="rerun_job", parameters={"gold_table": ""}, reason="undo"),
    )
    rc = RootCause(
        cause="gold_table points to samples.x.y",
        failure_class="permissions",
        confidence=0.95,
        evidence_refs=["compare_with_last_success"],
        needs_human=False,
    )
    return IncidentEvent(kind=EventKind.DIAGNOSIS_READY, incident=inc, root_cause=rc, resolution=res)


def test_html_email_shows_details_and_both_buttons(tmp_path):
    _, inc = _incident(tmp_path)
    html = render_html(_event(inc), "http://localhost:8787/decide?token=A", "http://localhost:8787/decide?token=D")
    assert "Approve fix" in html and "Deny" in html and "token=A" in html and "token=D" in html
    assert "gold_table points to samples.x.y" in html and "(reset to default)" in html and "95%" in html
    assert "Approve fix" not in render_html(_event(inc))  # no links → no buttons


def test_confirm_page_changes_nothing_and_decide_works_once(tmp_path):
    repo, inc = _incident(tmp_path)
    started = []
    service = ApprovalService(repo, LINKS, lambda iid: _event(repo.get(iid)), lambda iid, d: started.append((iid, d)))
    token = LINKS.token(inc.incident_id, "approve")
    status, html = service.confirm_page(token)
    assert status == 200 and "Confirm: approve the fix" in html and started == []  # GET never acts
    status, _ = service.decide(token, "ok from email")
    assert status == 200 and started[0][0] == inc.incident_id and started[0][1].decision == "approve"
    assert started[0][1].note == "ok from email"
    status, html = service.decide(token, "")  # second click
    assert status == 400 and "already used" in html and len(started) == 1


def test_link_for_an_incident_already_decided_is_refused(tmp_path):
    repo, inc = _incident(tmp_path)
    service = ApprovalService(repo, LINKS, lambda iid: _event(repo.get(iid)), lambda *a: None)
    repo.set_status(inc.incident_id, IncidentStatus.REJECTED)
    status, html = service.confirm_page(LINKS.token(inc.incident_id, "approve"))
    assert status == 400 and "already rejected" in html


def test_console_lists_incidents_and_offers_buttons_only_when_waiting(tmp_path):
    repo, inc = _incident(tmp_path)
    service = ApprovalService(repo, LINKS, lambda iid: _event(repo.get(iid)), lambda *a: None)
    status, html = service.index()
    assert status == 200 and inc.incident_id in html and "awaiting approval: 1" in html
    status, html = service.incident_page(inc.incident_id)
    assert status == 200 and "Approve fix" in html and "History" in html and "opened" in html
    repo.set_status(inc.incident_id, IncidentStatus.RESOLVED)
    assert "Approve fix" not in service.incident_page(inc.incident_id)[1]
    assert service.incident_page("INC-9999")[0] == 404


def test_grafana_button_opens_the_confirm_page_and_records_where_it_came_from(tmp_path):
    repo, inc = _incident(tmp_path)
    started = []
    service = ApprovalService(repo, LINKS, lambda iid: _event(repo.get(iid)), lambda iid, d: started.append(d))
    status, html = service.review(inc.incident_id, "approve")
    assert status == 200 and "Confirm: approve the fix" in html and started == []  # opening never acts
    token = html.split('name="token" value="')[1].split('"')[0]
    service.decide(token, "")
    assert started[0].decision == "approve" and started[0].by.endswith("(Grafana)")
    repo.set_status(inc.incident_id, IncidentStatus.RESOLVED)
    assert service.review(inc.incident_id, "approve")[0] == 409  # stale dashboard row: nothing to decide
    assert service.review(inc.incident_id, "delete")[0] == 404


def test_an_escalated_ticket_can_be_closed_from_grafana_and_nothing_runs(tmp_path):
    repo, inc = _incident(tmp_path)
    started = []
    service = ApprovalService(repo, LINKS, lambda iid: _event(repo.get(iid)), lambda iid, d: started.append(d))
    assert service.review(inc.incident_id, "close")[0] == 409  # awaiting approval: approve or deny instead
    repo.set_status(inc.incident_id, IncidentStatus.ESCALATED, "needs a person")
    assert service.review(inc.incident_id, "approve")[0] == 409  # nothing to approve on an escalated ticket
    status, html = service.review(inc.incident_id, "close")
    assert status == 200 and "Confirm: close the ticket" in html and "Nothing runs" in html
    token = html.split('name="token" value="')[1].split('"')[0]
    status, _ = service.decide(token, "upstream resent the file")
    assert status == 200 and started == [] and repo.get(inc.incident_id).status is IncidentStatus.CLOSED
    assert any(e["kind"] == "closed" and "upstream resent" in e["detail"] for e in repo.events(inc.incident_id))
    assert service.decide(token, "")[0] == 400  # single use
