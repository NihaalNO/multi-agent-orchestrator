"""Command line: ``python -m orchestrator <command>``.

  serve          run the HTTP API (uvicorn)
  hash-key       print the SHA-256 hash of an API key for principals.json
  check-config   load settings, policy and principals and report problems before deploying
  receipt-key    create the Ed25519 key that signs receipts
  verify-receipt check a receipt's signature, compare it with its request record, or check a chain
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys

from orchestrator.api.auth import hash_api_key
from orchestrator.config import Settings
from orchestrator.errors import ConfigurationError


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from orchestrator.observability.logging import configure_logging

    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"ERROR settings: {exc.message}: {exc.details.get('errors')}", file=sys.stderr)
        return 1
    # Fail fast with a readable message (not a stack trace) when the config cannot be loaded.
    errors, warnings = check_config(settings)
    for w in warnings:
        print(f"WARNING {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"ERROR   {e}", file=sys.stderr)
        print("not starting: fix the errors above (see `python -m orchestrator check-config`)", file=sys.stderr)
        return 1
    configure_logging(settings.log_level)
    uvicorn.run(
        "orchestrator.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,  # keep our JSON logging
        proxy_headers=args.proxy_headers,
    )
    return 0


def cmd_hash_key(args: argparse.Namespace) -> int:
    if args.generate:
        key = secrets.token_urlsafe(32)
        print(f"api key (give to the user, shown once): {key}")
        print(f"api_key_sha256 (put in principals.json): {hash_api_key(key)}")
        return 0
    key = sys.stdin.readline().rstrip("\r\n") if not sys.stdin.isatty() else getpass.getpass("API key: ")
    if not key:
        print("no key given", file=sys.stderr)
        return 1
    print(hash_api_key(key))
    return 0


def check_config(settings: Settings, environ: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a configuration. Pure, so it is testable."""
    from orchestrator.permissions import load_grants, load_principals

    env = dict(os.environ) if environ is None else environ
    errors: list[str] = []
    warnings: list[str] = []

    if settings.principals_path is None:
        warnings.append("ORCH_PRINCIPALS_PATH is not set: no API key will authenticate (every call gets 401).")
    else:
        try:
            principals = load_principals(settings.principals_path)
            if not principals:
                warnings.append(f"{settings.principals_path} defines no principals.")
            if not any(settings.admin_role in p.roles for p in principals):
                warnings.append(f"no principal has the {settings.admin_role!r} role: tools cannot be managed over the API.")
            if not any(settings.approver_role in p.roles for p in principals):
                warnings.append(f"no principal has the {settings.approver_role!r} role: sensitive operations can never be approved.")
            placeholder = {"0" * 64, "1" * 64}
            if any(p.api_key_sha256 in placeholder for p in principals):
                warnings.append("principals file still contains the example placeholder hashes.")
        except ConfigurationError as exc:
            errors.append(f"principals: {exc.message}")

    from orchestrator.tools.wasm_python import wasm_unavailable_reason

    if settings.python_sandbox != "subprocess":
        reason = wasm_unavailable_reason(settings.python_wasm_dir)
        if reason and settings.python_sandbox == "wasm":
            errors.append(f"ORCH_PYTHON_SANDBOX=wasm but the sandbox is unavailable: {reason}")
        elif reason:
            warnings.append(f"python_exec will run in a plain subprocess (not a security sandbox): {reason}")
    else:
        warnings.append("ORCH_PYTHON_SANDBOX=subprocess: python_exec is not sandboxed; grant it only to trusted users.")

    if settings.receipts != "off":
        if settings.receipt_key_path is None:
            warnings.append("ORCH_RECEIPT_KEY_PATH is not set: receipts are hash-chained but not signed; create a key "
                            "with `python -m orchestrator receipt-key --out <path>`.")
        else:
            from orchestrator.receipts import Signer

            try:
                Signer(settings.receipt_key_path)
            except ConfigurationError as exc:
                errors.append(f"receipts: {exc.message}")

    if settings.state_path is None:
        warnings.append("ORCH_STATE_PATH is not set: requests, approvals and duplicate protection are kept in "
                        "memory and lost on restart.")
    elif settings.state_path.is_dir():
        errors.append(f"ORCH_STATE_PATH {settings.state_path} is a directory; it must be a file path.")
    elif settings.state_path.exists():
        import sqlite3

        from orchestrator.state import SCHEMA_VERSION

        try:
            conn = sqlite3.connect(f"file:{settings.state_path.as_posix()}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            if version > SCHEMA_VERSION:
                errors.append(f"{settings.state_path} was written by a newer orchestrator "
                              f"(schema {version} > {SCHEMA_VERSION}).")
        except sqlite3.Error as exc:
            errors.append(f"state database {settings.state_path} cannot be read: {exc}")

    if settings.policy_path is None:
        warnings.append("ORCH_POLICY_PATH is not set: no grants, so every tool call will be denied.")
    else:
        try:
            if not load_grants(settings.policy_path):
                warnings.append(f"{settings.policy_path} defines no grants: everything is denied.")
        except ConfigurationError as exc:
            errors.append(f"policy: {exc.message}")

    def has_key(provider: str) -> bool:
        if settings.api_key_for(provider):
            return True
        if provider == "anthropic":
            return bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))
        if provider == "gemini":
            return bool(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"))
        return True

    from urllib.parse import urlsplit

    planner = settings.llm_provider.lower()
    from orchestrator.routing.openai_compat_planner import PROVIDERS as COMPAT

    if planner not in ("anthropic", "gemini", "rule_based", *COMPAT):
        errors.append(f"unknown ORCH_LLM_PROVIDER {settings.llm_provider!r} "
                      f"(anthropic, gemini, {', '.join(COMPAT)} or rule_based)")
    if planner in COMPAT:
        profile = COMPAT[planner]
        try:
            import openai  # noqa: F401
        except ImportError:
            errors.append(f"planner is {planner!r} but the openai package is not installed: "
                          "pip install 'multi-tool-orchestrator[openai-compat]'")
        url = profile.resolve_base_url(settings.llm_base_url, env)
        if profile.key_needed(url) and not profile.api_key(settings.api_key_for(planner), env):
            errors.append(f"planner is {planner!r} but no key is set: set {' or '.join(profile.key_env)}")
        if not (settings.planner_model or profile.default_model):
            errors.append(f"planner is {planner!r} but no model is set: set ORCH_LLM_MODEL "
                          "(the model name your endpoint serves)")
        if planner == "openai" and urlsplit(url).path.strip("/") == "":
            warnings.append(f"planner is 'openai' at {url}: the base URL has no path; most OpenAI-compatible APIs "
                            f"live under /v1 (try {url.rstrip('/')}/v1).")
        if planner == "openai" and not profile.is_native(url) and settings.llm_json_mode == "auto":
            warnings.append(f"planner is 'openai' at {url}: plans use JSON mode with the schema in the instructions; "
                            "set ORCH_LLM_JSON_MODE=json_schema if this server enforces JSON Schemas.")
        if planner == "ollama":
            url = settings.llm_base_url or profile.base_url
            warnings.append(f"planner is 'ollama': make sure an Ollama server is reachable at {url} and the model "
                            f"is pulled (`ollama pull {settings.planner_model}`); this check does not contact it.")
    if planner == "anthropic" and not has_key("anthropic"):
        warnings.append("planner is 'anthropic' but no API key is set in the environment; the SDK may still find an "
                        "`ant auth login` profile, otherwise planning will fail (requests are rejected, not run).")
    if settings.search_backend == "anthropic" and not has_key("anthropic"):
        warnings.append("search backend is 'anthropic' but no API key is set in the environment; searches will fail "
                        "unless the SDK finds an `ant auth login` profile.")
    # Gemini has no sign-in fallback: without a key its client cannot even be created.
    gemini_uses = [use for use, on in (("planner", planner == "gemini"), ("search backend", settings.search_backend == "gemini")) if on]
    if gemini_uses:
        try:
            import google.genai  # noqa: F401
        except ImportError:
            errors.append(f"{' and '.join(gemini_uses)} use Gemini but google-genai is not installed: "
                          "pip install 'multi-tool-orchestrator[gemini]'")
        if not has_key("gemini"):
            errors.append(f"{' and '.join(gemini_uses)} use Gemini but no key is set: "
                          "set GEMINI_API_KEY (or GOOGLE_API_KEY)")
    if settings.search_backend == "tavily":
        from orchestrator.tools.tavily_search import KEY_ENV, tavily_key

        if not tavily_key(env):
            errors.append(f"search backend is 'tavily' but no key is set: set {KEY_ENV} (from https://app.tavily.com/)")
    if settings.search_backend == "none":
        warnings.append("ORCH_SEARCH_BACKEND is 'none': no web search tool is registered, so requests that need "
                        "web search are only partly answered (or rejected).")
    return errors, warnings


def cmd_check_config(args: argparse.Namespace) -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"ERROR settings: {exc.message}: {exc.details.get('errors')}")
        return 1
    errors, warnings = check_config(settings)
    print(f"environment={settings.environment} planner={settings.llm_provider} model={settings.planner_model} "
          f"search={settings.search_backend}{'/' + settings.effective_search_model if settings.effective_search_model else ''} "
          f"uploads={settings.file_tool_root}")
    for e in errors:
        print(f"ERROR   {e}")
    for w in warnings:
        print(f"WARNING {w}")
    if not errors and not warnings:
        print("OK")
    return 1 if errors else 0


def cmd_receipt_key(args: argparse.Namespace) -> int:
    from orchestrator.receipts import generate_key, key_id

    try:
        public = generate_key(args.out)
    except ConfigurationError as exc:
        print(f"ERROR {exc.message}", file=sys.stderr)
        return 1
    print(f"private key written to {args.out} (keep it secret; set ORCH_RECEIPT_KEY_PATH to this path)")
    print(f"key id: {key_id(public)}")
    print(public, end="")
    return 0


def cmd_verify_receipt(args: argparse.Namespace) -> int:
    import json

    from orchestrator.receipts import Receipt, verify_against_record, verify_chain, verify_signature

    def load(path):
        return json.loads(open(path, encoding="utf-8").read())

    findings = []
    public = open(args.public_key, encoding="utf-8").read() if args.public_key else None
    if args.chain:
        data = load(args.chain)
        receipts = [Receipt.model_validate(r) for r in (data["receipts"] if isinstance(data, dict) else data)]
        findings += verify_chain(receipts)
        if public:
            findings += [verify_signature(r, public) for r in receipts]
    if args.receipt:
        receipt = Receipt.model_validate(load(args.receipt))
        findings.append(verify_signature(receipt, public) if public else
                        verify_signature(receipt, "") if not receipt.signature else None)
        if args.record:
            findings += verify_against_record(receipt, load(args.record))
    findings = [f for f in findings if f is not None]
    if not findings:
        print("nothing to check: give a receipt (with --public-key and/or --record) or --chain")
        return 2
    for f in findings:
        print(f"{f.level.upper():7} {f.message}")
    return 1 if any(f.level == "error" for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--proxy-headers", action="store_true",
                       help="trust X-Forwarded-* headers (only behind a reverse proxy you control)")
    serve.set_defaults(func=cmd_serve)

    hk = sub.add_parser("hash-key", help="hash an API key (prompted, or read from stdin)")
    hk.add_argument("--generate", action="store_true", help="generate a new random key and print it with its hash")
    hk.set_defaults(func=cmd_hash_key)

    cc = sub.add_parser("check-config", help="validate configuration from ORCH_* environment variables")
    cc.set_defaults(func=cmd_check_config)

    rk = sub.add_parser("receipt-key", help="create the Ed25519 key that signs receipts")
    rk.add_argument("--out", required=True, help="where to write the private key (PEM); never overwritten")
    rk.set_defaults(func=cmd_receipt_key)

    vr = sub.add_parser("verify-receipt", help="check receipts offline")
    vr.add_argument("receipt", nargs="?", help="a receipt JSON file (GET /requests/{id}/receipt)")
    vr.add_argument("--public-key", help="PEM file from GET /receipts/public-key")
    vr.add_argument("--record", help="the request record JSON (GET /requests/{id}) to compare hashes with")
    vr.add_argument("--chain", help="JSON from GET /admin/receipts: check sequence and links")
    vr.set_defaults(func=cmd_verify_receipt)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
