import pytest
from fastapi.testclient import TestClient

from orchestrator.api import create_app
from orchestrator.api.auth import ApiKeyAuthenticator, hash_api_key
from orchestrator.config import Settings
from orchestrator.contracts import Principal
from orchestrator.permissions import ApprovalManager, PrincipalRecord
from orchestrator.registry import ToolRegistry

from fakes import CalcTool

KEYS = {"alice": "alice-key", "bob": "bob-key", "root": "root-key"}
ROLES = {"alice": ["analyst"], "bob": ["approver"], "root": ["admin"]}


def auth(user):
    return {"Authorization": f"Bearer {KEYS[user]}"}


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def approvals():
    return ApprovalManager()


@pytest.fixture
def client(registry, approvals):
    authn = ApiKeyAuthenticator(
        PrincipalRecord(user_id=u, roles=ROLES[u], api_key_sha256=hash_api_key(k)) for u, k in KEYS.items()
    )
    app = create_app(settings=Settings(), registry=registry, authenticator=authn, approvals=approvals)
    with TestClient(app) as c:
        yield c


def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic alice-key"}])
def test_protected_endpoints_require_valid_key(client, headers):
    assert client.get("/tools", headers=headers).status_code == 401


def test_me_reports_principal_from_key(client):
    assert client.get("/me", headers=auth("alice")).json() == {"user_id": "alice", "roles": ["analyst"]}


def test_tool_registered_at_runtime_is_visible_over_api(client, registry):
    assert client.get("/tools", headers=auth("alice")).json() == {"tools": []}
    client.portal.call(registry.register, CalcTool())
    tools = client.get("/tools", headers=auth("alice")).json()["tools"]
    assert [t["spec"]["tool_id"] for t in tools] == ["calc"]
    matches = client.get("/tools", params={"capability": "data.compute"}, headers=auth("alice")).json()["matches"]
    assert matches[0]["tool_id"] == "calc"


def test_unknown_tool_returns_structured_404(client):
    r = client.get("/tools/ghost", headers=auth("alice"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "tool_not_found"


def test_admin_endpoints_require_admin_role(client, registry):
    client.portal.call(registry.register, CalcTool())
    assert client.post("/admin/tools/calc/disable", headers=auth("alice")).status_code == 403
    assert client.post("/admin/tools/calc/disable", headers=auth("bob")).status_code == 403
    r = client.post("/admin/tools/calc/disable", headers=auth("root"))
    assert r.status_code == 200 and r.json()["available"] is False
    assert client.get("/capabilities", headers=auth("alice")).json() == {}
    assert client.post("/admin/tools/calc/enable", headers=auth("root")).json()["available"] is True
    assert client.post("/admin/tools/health-check", headers=auth("root")).json() == {"calc": "healthy"}


def test_approval_endpoints(client, approvals):
    alice = Principal(user_id="alice", roles=frozenset({"analyst"}))
    item = client.portal.call(lambda: approvals.request(
        principal=alice, request_id="r", task_id="t", tool_id="records", operation="delete",
        resource="db:records/1", args={"id": 1},
    ))
    aid = item.approval_id
    assert client.get("/approvals", headers=auth("alice")).status_code == 403
    listed = client.get("/approvals", headers=auth("bob")).json()["approvals"]
    assert [a["approval_id"] for a in listed] == [aid]

    # Requester can see their own approval; another non-approver cannot even confirm it exists.
    assert client.get(f"/approvals/{aid}", headers=auth("alice")).json()["status"] == "pending"
    assert client.get(f"/approvals/{aid}", headers=auth("root")).status_code == 404

    assert client.post(f"/approvals/{aid}/approve", headers=auth("alice")).status_code == 403
    r = client.post(f"/approvals/{aid}/approve", headers=auth("bob"), json={"note": "looks fine"})
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["note"] == "looks fine"
    r = client.post(f"/approvals/{aid}/deny", headers=auth("bob"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_required"


def test_no_principals_configured_means_nobody_gets_in(registry):
    with TestClient(create_app(settings=Settings(), registry=registry)) as c:
        assert c.get("/tools", headers=auth("root")).status_code == 401


# --- Chat UI -----------------------------------------------------------------------------------


def test_root_redirects_to_chat_ui(client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 307 and res.headers["location"] == "/ui"


@pytest.mark.parametrize("path,ctype", [("/ui", "text/html"), ("/ui/chat.js", "text/javascript"), ("/ui/chat.css", "text/css")])
def test_chat_ui_is_served_without_a_key_and_locked_down(client, path, ctype):
    res = client.get(path)
    assert res.status_code == 200 and res.headers["content-type"].startswith(ctype)
    csp = res.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert res.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", ["../app.py", "app.py", "chat.html", "missing.js"])
def test_only_the_chat_files_are_served(client, name):
    assert client.get(f"/ui/{name}").status_code == 404


def test_chat_ui_renders_server_text_safely():
    from pathlib import Path

    import orchestrator.api.app as app_module

    js = (Path(app_module.__file__).parent / "static" / "chat.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js
    assert "localStorage" not in js  # the key lives only for the tab (sessionStorage)


def test_chat_ui_is_packaged():
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["setuptools"]["package-data"]["orchestrator.api"] == ["static/*", "static/fonts/*"]


def test_chat_ui_explains_which_key_and_never_sends_provider_keys():
    from pathlib import Path

    import orchestrator.api.app as app_module

    static = Path(app_module.__file__).parent / "static"
    html = (static / "chat.html").read_text(encoding="utf-8")
    js = (static / "chat.js").read_text(encoding="utf-8")
    assert "Orchestrator API key" in html and "config/dev/api-key.txt" in html
    for prefix in ("gsk_", "sk-ant-", "AIza"):
        assert prefix in js
    # The provider check runs before signIn (which is what sends the key to /me).
    handler = js[js.index('addEventListener("submit", async'):]
    assert handler.index("providerOf(key)") < handler.index("signIn(key)")


def test_chat_ui_clears_the_conversation_between_users_and_messages():
    from pathlib import Path

    import orchestrator.api.app as app_module

    js = (Path(app_module.__file__).parent / "static" / "chat.js").read_text(encoding="utf-8")
    sign_out = js[js.index("function signOut"):js.index("// ----", js.index("function signOut"))]
    assert "resetConversation()" in sign_out           # nothing left on screen for the next user
    send = js[js.index("async function send"):]
    assert send.index("state.attached = []") < send.index("streamRequest(")  # files go with one message


def test_chat_ui_links_only_web_addresses():
    from pathlib import Path

    import orchestrator.api.app as app_module

    js = (Path(app_module.__file__).parent / "static" / "chat.js").read_text(encoding="utf-8")
    safe = js[js.index("function safeUrl"):js.index("function sources")]
    assert '"https:"' in safe and '"http:"' in safe
    assert 'rel: "noopener noreferrer"' in js and "const href = safeUrl(s.url)" in js



@pytest.mark.parametrize("name", ["public-sans-400.woff2", "public-sans-600.woff2", "public-sans-700.woff2",
                                  "barlow-condensed-600.woff2"])
def test_chat_fonts_are_self_hosted(client, name):
    r = client.get(f"/ui/fonts/{name}")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2" and r.content[:4] == b"wOF2"
    assert "font-src 'self'" in client.get("/ui").headers["content-security-policy"]


@pytest.mark.parametrize("name", ["LICENSE-PublicSans.txt", "chat.js", "other.woff2"])
def test_only_the_chat_fonts_are_served(client, name):
    assert client.get(f"/ui/fonts/{name}").status_code == 404


def test_font_licenses_ship_with_the_fonts():
    from pathlib import Path

    import orchestrator.api.app as app_module

    fonts = Path(app_module.__file__).parent / "static" / "fonts"
    for lic in ("LICENSE-PublicSans.txt", "LICENSE-BarlowCondensed.txt"):
        assert "SIL Open Font License" in (fonts / lic).read_text(encoding="utf-8")


def test_receipts_are_copied_not_downloaded():
    from pathlib import Path

    import orchestrator.api.app as app_module

    js = (Path(app_module.__file__).parent / "static" / "chat.js").read_text(encoding="utf-8")
    assert "Copy receipt" in js and "navigator.clipboard.writeText" in js and 'execCommand("copy")' in js
    assert "Download receipt" not in js and "createObjectURL" not in js
