import pytest
from fastapi.testclient import TestClient

from orchestrator.api import create_app
from orchestrator.config import Settings
from orchestrator.registry import ToolRegistry

from fakes import CalcTool

ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def client(registry):
    app = create_app(settings=Settings(admin_token="test-admin"), registry=registry)
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_tool_registered_at_runtime_is_visible_over_api(client, registry):
    assert client.get("/tools").json() == {"tools": []}
    client.portal.call(registry.register, CalcTool())
    tools = client.get("/tools").json()["tools"]
    assert [t["spec"]["tool_id"] for t in tools] == ["calc"]
    assert tools[0]["available"] is True
    matches = client.get("/tools", params={"capability": "data.compute"}).json()["matches"]
    assert matches[0]["tool_id"] == "calc"
    assert client.get("/capabilities").json() == {
        "data.compute": ["calc.mean"],
        "data.stats.mean": ["calc.mean"],
    }


def test_unknown_tool_returns_structured_404(client):
    r = client.get("/tools/ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "tool_not_found"


def test_admin_requires_token(client, registry):
    client.portal.call(registry.register, CalcTool())
    assert client.post("/admin/tools/calc/disable").status_code == 401
    assert client.post("/admin/tools/calc/disable", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.post("/admin/tools/calc/disable", headers=ADMIN)
    assert r.status_code == 200 and r.json()["available"] is False
    assert client.get("/capabilities").json() == {}
    assert client.post("/admin/tools/calc/enable", headers=ADMIN).json()["available"] is True
    assert client.post("/admin/tools/health-check", headers=ADMIN).json() == {"calc": "healthy"}


def test_admin_disabled_without_configured_token(registry):
    app = create_app(settings=Settings(), registry=registry)
    with TestClient(app) as c:
        assert c.post("/admin/tools/health-check", headers=ADMIN).status_code == 403
