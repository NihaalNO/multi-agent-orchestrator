"""Keep docs, examples and the CLI honest: they are checked against the code."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from orchestrator.__main__ import check_config, main
from orchestrator.api import create_app
from orchestrator.api.auth import hash_api_key
from orchestrator.config import Settings

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


# --- Docs drift ------------------------------------------------------------------------------------


def test_every_setting_is_documented():
    text = (DOCS / "DEPLOYMENT.md").read_text(encoding="utf-8")
    missing = [f"ORCH_{n.upper()}" for n in Settings.model_fields if f"`ORCH_{n.upper()}`" not in text]
    assert not missing, f"undocumented settings: {missing}"


def test_no_documented_setting_is_stale():
    text = (DOCS / "DEPLOYMENT.md").read_text(encoding="utf-8") + (ROOT / ".env.example").read_text(encoding="utf-8")
    known = {f"ORCH_{n.upper()}" for n in Settings.model_fields}
    stale = sorted(set(re.findall(r"ORCH_[A-Z_]+", text)) - known)
    assert not stale, f"docs mention settings that do not exist: {stale}"


def test_every_route_is_documented():
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    app = create_app(settings=Settings(llm_provider="rule_based"))
    paths = {r.path for r in app.routes if hasattr(r, "methods") and not r.path.startswith(("/docs", "/openapi", "/redoc"))}
    missing = sorted(p for p in paths if p not in text)
    assert not missing, f"undocumented routes: {missing}"


def test_docs_links_resolve():
    for md in [ROOT / "README.md", *DOCS.glob("*.md")]:
        for target in re.findall(r"\]\(((?!https?://|#)[^)\s]+)\)", md.read_text(encoding="utf-8")):
            path = (md.parent / target.split("#")[0].split(":")[0]).resolve()
            assert path.exists(), f"{md.name}: broken link {target}"


# --- Examples keep working -------------------------------------------------------------------------


def _load(name):
    spec = importlib.util.spec_from_file_location(f"example_{name}", ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name,expected",
    [
        ("mvp_demo", "Average revenue: {\"column\": \"Revenue\", \"mean\": 129325.0, \"count\": 6}"),
        ("custom_tool", "After: completed"),
        ("approval_workflow", "3. resumed: completed"),
    ],
)
def test_examples_run(name, expected, capsys):
    assert _load(name).main([]) == 0
    assert expected in capsys.readouterr().out


# --- CLI ---------------------------------------------------------------------------------------------


def test_hash_key_from_stdin(monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret\n"))
    assert main(["hash-key"]) == 0
    assert capsys.readouterr().out.strip() == hash_api_key("s3cret")


def test_hash_key_generate(capsys):
    assert main(["hash-key", "--generate"]) == 0
    out = capsys.readouterr().out
    key = re.search(r"shown once\): (\S+)", out)[1]
    assert hash_api_key(key) in out and len(key) >= 32


def test_check_config_flags_common_mistakes(tmp_path):
    errors, warnings = check_config(Settings(), environ={})
    text = " ".join(warnings)
    assert not errors
    assert "ORCH_PRINCIPALS_PATH is not set" in text and "ORCH_POLICY_PATH is not set" in text
    assert "no API key is set" in text


def test_check_config_with_example_files(tmp_path):
    from orchestrator.receipts import generate_key

    generate_key(tmp_path / "receipt-key.pem")
    settings = Settings(policy_path=ROOT / "config/policy.example.json",
                        principals_path=ROOT / "config/principals.example.json", llm_provider="rule_based",
                        state_path=tmp_path / "state.db", receipt_key_path=tmp_path / "receipt-key.pem")
    errors, warnings = check_config(settings, environ={})
    assert not errors
    assert warnings[0] == "principals file still contains the example placeholder hashes."
    assert warnings[1].startswith("ORCH_SEARCH_BACKEND is 'none'") and len(warnings) == 2


def test_check_config_search_backend_without_key():
    _, warnings = check_config(Settings(llm_provider="rule_based", search_backend="anthropic"), environ={})
    assert any("search backend is 'anthropic'" in w for w in warnings)
    _, warnings = check_config(Settings(llm_provider="rule_based", search_backend="anthropic"),
                               environ={"ANTHROPIC_API_KEY": "x"})
    assert not any("search" in w.lower() for w in warnings)


def test_check_config_reports_broken_files(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text("{not json", encoding="utf-8")
    errors, _ = check_config(Settings(policy_path=bad, llm_provider="rule_based"), environ={})
    assert errors and errors[0].startswith("policy:")


def test_serve_refuses_to_start_with_broken_config(monkeypatch, tmp_path, capsys):
    import uvicorn

    def must_not_run(*a, **k):
        raise AssertionError("uvicorn must not start with a broken config")

    monkeypatch.setattr(uvicorn, "run", must_not_run)
    monkeypatch.setenv("ORCH_LLM_PROVIDER", "rule_based")
    monkeypatch.setenv("ORCH_POLICY_PATH", str(tmp_path / "missing-policy.json"))
    monkeypatch.setenv("ORCH_PRINCIPALS_PATH", str(tmp_path / "missing-principals.json"))
    assert main(["serve"]) == 1
    err = capsys.readouterr().err
    assert "file not found" in err and "not starting" in err


def test_check_config_receipt_key(tmp_path):
    _, warnings = check_config(Settings(llm_provider="rule_based"), environ={})
    assert any("receipts are hash-chained but not signed" in w for w in warnings)
    (tmp_path / "bad.pem").write_text("not a key", encoding="utf-8")
    errors, _ = check_config(Settings(llm_provider="rule_based", receipt_key_path=tmp_path / "bad.pem"), environ={})
    assert any(e.startswith("receipts: cannot load the receipt signing key") for e in errors)
    _, warnings = check_config(Settings(llm_provider="rule_based", receipts="off"), environ={})
    assert not any("receipt" in w for w in warnings)
