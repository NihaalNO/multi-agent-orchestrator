"""Code answers: detection, deterministic checks, running through the normal permission path."""

import json

import pytest

from orchestrator.coding import CodeAnswer, _RawCode, check, parse, user_message, wants_code
from orchestrator.config import Settings
from orchestrator.contracts import AccessLevel, PermissionGrant
from orchestrator.core import Orchestrator, RequestState
from orchestrator.errors import PlanError
from orchestrator.permissions import GrantPolicy, PermissionManager
from orchestrator.receipts import Receipt, ReceiptIssuer, verify_against_record
from orchestrator.routing import RuleBasedPlanner

from test_orchestrator import ALICE, FULL_GRANTS, MVP, client_for, h, world  # noqa: F401  (world is a fixture)


# --- detection --------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Write a Python function that checks for palindromes",
    "Give me a SQL query that counts orders per customer",
    "create a bash script to back up a folder",
    "Implement binary search in Rust",
    "How do I write a regex for email addresses?",
    "Can you generate unit tests for this function",
    "refactor this code to use a dataclass",
])
def test_requests_for_code_are_detected(text):
    assert wants_code(text)


@pytest.mark.parametrize("text", [
    MVP,  # "calculate its average revenue using Python": an analysis, not a request for code
    "Calculate the average revenue of my uploaded sales file using Python",
    "Search the web for the latest SaaS revenue growth benchmarks",
    "Now double it using Python",
    "What is the median revenue in sales.csv?",
])
def test_analysis_requests_are_not_code_requests(text):
    assert not wants_code(text)


# --- deterministic checks ---------------------------------------------------------------------


def raw(**kw):
    base = {"language": "python", "filename": "pal.py", "code": "print('ok')", "explanation": "e", "runnable": True}
    return _RawCode(**{**base, **kw})


def test_markdown_fences_are_removed():
    assert check(raw(code="```python\nprint('ok')\n```"), "w", None).code == "print('ok')"


def test_other_languages_are_shown_not_run():
    c = check(raw(language="JavaScript", filename="a.js", code="console.log(1)"), "w", None)
    assert c.language == "javascript" and not c.runnable and c.syntax_ok is None and "only Python runs" in c.run_note


def test_python_syntax_errors_are_caught_without_running():
    c = check(raw(code="def f(:\n    pass"), "w", None)
    assert c.syntax_ok is False and not c.runnable and c.syntax_error.startswith("line 1")


def test_python_the_writer_marks_not_runnable_is_not_run():
    c = check(raw(code="name = input()\nprint(name)", runnable=False), "w", None)
    assert c.syntax_ok is True and not c.runnable and "needs input" in c.run_note


def test_empty_or_unreadable_output_is_a_plan_error():
    with pytest.raises(PlanError):
        check(raw(code="   "), "w", None)
    with pytest.raises(PlanError):
        parse("not json", "w", None)


def test_previous_code_is_passed_as_data():
    prev = CodeAnswer(language="python", filename="a.py", code="x = 1")
    msg = user_message("now add type hints", prev)
    assert msg.startswith("<request>\nnow add type hints\n</request>") and "<previous_code" in msg and "x = 1" in msg


# --- the flow ----------------------------------------------------------------------------------


class FakeWriter:
    name, model = "fakewriter", "fake-model"

    def __init__(self, **reply):
        self.reply, self.seen = reply, []

    async def write(self, text, previous=None):
        self.seen.append((text, previous))
        return check(raw(**self.reply), self.name, self.model)


def orch(world, writer, grants=FULL_GRANTS, receipts=False):
    perms = PermissionManager(GrantPolicy(grants), world.registry, world.audit) if grants is not FULL_GRANTS else world.permissions
    issuer = ReceiptIssuer(policy_grants=lambda: perms.policy.grants) if receipts else None
    return Orchestrator(world.registry, RuleBasedPlanner(), perms, world.approvals, world.audit,
                        settings=Settings(llm_provider="rule_based"), code_writer=writer, receipts=issuer)


PALINDROME = ("def is_palindrome(s: str) -> bool:\n    t = [c.lower() for c in s if c.isalnum()]\n    return t == t[::-1]\n\n"
              "if __name__ == '__main__':\n    for s in ['Racecar', 'A man, a plan, a canal: Panama', 'hello']:\n"
              "        print(s, is_palindrome(s))\n")


async def test_runnable_python_runs_in_the_sandbox_through_the_normal_path(world):
    o = orch(world, FakeWriter(code=PALINDROME), receipts=True)
    record = await o.submit("Write a Python function that checks for palindromes", ALICE)
    assert record.state == RequestState.COMPLETED and record.code.syntax_ok is True
    [run] = record.report.task_results
    assert run.task_id == "run_code" and run.tool_id == "python_exec" and run.status.value == "succeeded"
    assert "Racecar True" in run.output["stdout"] and "hello False" in run.output["stdout"]  # __main__ block ran
    assert [d.tool_id for d in record.routing] == ["python_exec"]          # routed like any plan
    assert world.sink.events("permission.decision")                          # and permission-checked
    assert record.answer is None                                             # the code is the answer
    assert record.receipt.code_sha256 and verify_against_record(record.receipt, record.public())


async def test_other_languages_complete_without_a_run(world):
    o = orch(world, FakeWriter(language="javascript", filename="pal.js", code="console.log('x')"))
    record = await o.submit("Write a JavaScript function that reverses a string", ALICE)
    assert record.state == RequestState.COMPLETED and record.plan is None and record.report is None
    assert "only Python runs" in record.code.run_note


async def test_a_failing_run_is_shown_not_hidden(world):
    o = orch(world, FakeWriter(code="print('start')\nraise ValueError('boom')"))
    record = await o.submit("Write a Python script that fails", ALICE)
    assert record.state == RequestState.COMPLETED            # the code was delivered
    [run] = record.report.task_results
    assert run.status.value == "failed" and "ValueError: boom" in run.error.details["traceback"]
    assert "start" in run.error.details["stdout"]


async def test_without_permission_to_run_code_the_code_still_comes_back(world):
    grants = [g for g in FULL_GRANTS if g.tool_id != "python_exec"]
    o = orch(world, FakeWriter(code="print(1)"), grants=grants)
    record = await o.submit("Write a Python script that prints 1", ALICE)
    assert record.state == RequestState.COMPLETED and record.report is None
    assert record.code.run_note == "Not run: you don't have permission to run code."


async def test_modes(world):
    writer = FakeWriter(code="print(1)")
    o = orch(world, writer)
    forced = await o.submit("palindromes please", ALICE, mode="code")           # Code switch: always code
    assert forced.code is not None
    task = await o.submit("Write a Python function for palindromes", ALICE, mode="task")
    assert task.code is None                                                       # task: never code
    auto = await o.submit(MVP, ALICE)                                              # auto: analysis stays analysis
    assert auto.code is None and auto.report.final_output["compute_mean"]["result"]["mean"] == 200.0
    no_writer = await orch(world, None).submit("anything", ALICE, mode="code")
    assert no_writer.state == RequestState.REJECTED and "Code answers need" in no_writer.error.message


async def test_follow_ups_change_the_earlier_code(world):
    writer = FakeWriter(code="def f():\n    return 1\nprint(f())")
    o = orch(world, writer)
    first = await o.submit("Write a Python function f", ALICE)
    await o.submit("Now add type hints to it", ALICE, mode="code", context=[first.request_id])
    assert writer.seen[1][1].code.startswith("def f()")


def test_receipts_issued_before_code_answers_still_verify(tmp_path):
    from orchestrator.receipts import Signer, generate_key, verify_signature

    generate_key(tmp_path / "k.pem")
    signer = Signer(tmp_path / "k.pem")
    r = Receipt(sequence=1, previous="sha256:" + "0" * 64, request_id="r", user_id="u", issued_at="t", state="completed",
                question_sha256="sha256:x", policy_sha256="sha256:y")
    signed = r.model_copy(update={"signature": signer.sign(__import__("orchestrator.receipts", fromlist=["canonical"]).canonical(r.body()))})
    old_form = json.loads(signed.model_dump_json())
    del old_form["code_sha256"]                                   # how a pre-existing receipt looks
    again = Receipt.model_validate(old_form)
    assert verify_signature(again, signer.public_pem).level == "ok"
    assert again.receipt_sha256 == signed.receipt_sha256
    assert "code_sha256" not in again.body()


# --- HTTP ---------------------------------------------------------------------------------------


async def test_api_mode_field(world):
    with client_for(world) as c:
        assert c.post("/requests", json={"text": MVP, "mode": "nonsense"}, headers=h("alice")).status_code == 422
        r = c.post("/requests", json={"text": MVP, "mode": "task"}, headers=h("alice"))
        assert r.status_code == 200 and r.json()["code"] is None


def test_progress_names_the_code_phases():
    from orchestrator.observability.audit import AuditEvent
    from orchestrator.observability.progress import to_progress

    assert to_progress(AuditEvent(kind="code.writing", request_id="r")) == {"type": "coding"}
    assert to_progress(AuditEvent(kind="code.written", request_id="r", data={"language": "python", "runnable": True,
                                                                             "lines": 9})) == {
        "type": "coded", "language": "python", "runnable": True}


def test_chat_has_a_code_switch_and_console_windows():
    from pathlib import Path

    import orchestrator.api.app as app_module

    static = Path(app_module.__file__).parent / "static"
    html = (static / "chat.html").read_text(encoding="utf-8")
    js = (static / "chat.js").read_text(encoding="utf-8")
    assert 'name="mode" value="code"' in html and 'name="mode" value="auto"' in html
    assert "function codeAnswer" in js and "Copy code" in js and "mode: currentMode()" in js
    assert "innerHTML" not in js
