"""Command line: ``python -m orchestrator <command>``.

  serve          run the HTTP API (uvicorn)
  hash-key       print the SHA-256 hash of an API key for principals.json
  check-config   load settings, policy and principals and report problems before deploying
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

    if settings.policy_path is None:
        warnings.append("ORCH_POLICY_PATH is not set: no grants, so every tool call will be denied.")
    else:
        try:
            if not load_grants(settings.policy_path):
                warnings.append(f"{settings.policy_path} defines no grants: everything is denied.")
        except ConfigurationError as exc:
            errors.append(f"policy: {exc.message}")

    has_key = bool(settings.llm_api_key or env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))
    if settings.llm_provider == "anthropic" and not has_key:
        warnings.append("planner is 'anthropic' but no API key is set in the environment; the SDK may still find an "
                        "`ant auth login` profile, otherwise planning will fail (requests are rejected, not run).")
    if settings.search_backend == "anthropic" and not has_key:
        warnings.append("search backend is 'anthropic' but no API key is set in the environment; searches will fail "
                        "unless the SDK finds an `ant auth login` profile.")
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
    print(f"environment={settings.environment} planner={settings.llm_provider} model={settings.llm_model} "
          f"uploads={settings.file_tool_root}")
    for e in errors:
        print(f"ERROR   {e}")
    for w in warnings:
        print(f"WARNING {w}")
    if not errors and not warnings:
        print("OK")
    return 1 if errors else 0


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
