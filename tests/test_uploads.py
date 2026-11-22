"""File uploads: permissions, path safety, limits, atomic commit, and upload-then-request end to end."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from orchestrator.api import create_app
from orchestrator.api.auth import ApiKeyAuthenticator, hash_api_key
from orchestrator.config import Settings
from orchestrator.contracts import AccessLevel, PermissionGrant, Principal
from orchestrator.errors import PermissionDeniedError
from orchestrator.observability import AuditLog, InMemoryAuditSink
from orchestrator.permissions import GrantPolicy, PrincipalRecord, load_grants
from orchestrator.uploads import (
    STAGING_DIR,
    FileExistsConflictError,
    UnsupportedFileTypeError,
    UploadStore,
    UploadTooLargeError,
)

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
BOB = Principal(user_id="bob", roles=frozenset({"analyst"}))

PER_USER = [
    PermissionGrant(subject="role:analyst", tool_id="uploads", max_access=AccessLevel.WRITE,
                    resources=("file:users/{user_id}/*",)),
    PermissionGrant(subject="role:analyst", tool_id="file_processing", resources=("file:users/{user_id}/*",)),
    PermissionGrant(subject="role:analyst", tool_id="python_exec", operation="run", max_access=AccessLevel.EXECUTE),
]


async def body(*chunks):
    for c in chunks:
        yield c


@pytest.fixture
def sink():
    return InMemoryAuditSink()


@pytest.fixture
def store(tmp_path, sink):
    return UploadStore(tmp_path / "uploads", GrantPolicy(PER_USER), AuditLog(sink),
                       max_bytes=1024, allowed_extensions=frozenset({".csv", ".txt", ".bin"}))


# --- Store --------------------------------------------------------------------------------------------


async def test_upload_list_delete(store, tmp_path, sink):
    info, replaced = await store.save(ALICE, "users/alice/sales.csv", body(b"a,b\n", b"1,2\n"))
    assert (info.path, info.size_bytes, replaced) == ("users/alice/sales.csv", 8, False)
    assert len(info.sha256) == 64
    assert (tmp_path / "uploads/users/alice/sales.csv").read_bytes() == b"a,b\n1,2\n"
    assert [f.path for f in store.list(ALICE)] == ["users/alice/sales.csv"]
    store.delete(ALICE, "users/alice/sales.csv")
    assert store.list(ALICE) == []
    assert [e.kind for e in sink.events() if e.kind.startswith("file.")] == ["file.uploaded", "file.deleted"]


async def test_users_are_isolated_by_user_id_placeholder(store):
    await store.save(ALICE, "users/alice/a.csv", body(b"x\n"))
    with pytest.raises(PermissionDeniedError):
        await store.save(ALICE, "users/bob/a.csv", body(b"x\n"))
    with pytest.raises(PermissionDeniedError):
        await store.save(ALICE, "shared.csv", body(b"x\n"))
    assert store.list(BOB) == []  # bob cannot even see alice's file
    with pytest.raises(PermissionDeniedError):
        store.delete(BOB, "users/alice/a.csv")


@pytest.mark.parametrize("bad", ["../escape.csv", "users/alice/../../x.csv", "/etc/passwd.csv",
                                 "users/alice/.hidden.csv", f"{STAGING_DIR}/x.csv", ""])
async def test_unsafe_paths_rejected(store, bad):
    with pytest.raises(PermissionDeniedError):
        await store.save(ALICE, bad, body(b"x\n"))


async def test_size_cap_enforced_while_streaming_and_staging_cleaned(store, tmp_path):
    with pytest.raises(UploadTooLargeError):
        await store.save(ALICE, "users/alice/big.csv", body(b"x" * 600, b"x" * 600))
    assert not (tmp_path / "uploads/users/alice/big.csv").exists()
    assert list((tmp_path / "uploads" / STAGING_DIR).iterdir()) == []
    with pytest.raises(UploadTooLargeError):  # declared size rejected before reading anything
        await store.save(ALICE, "users/alice/big.csv", body(b""), declared_size=10_000)


async def test_types_and_encoding(store):
    with pytest.raises(UnsupportedFileTypeError, match="'.exe'"):
        await store.save(ALICE, "users/alice/tool.exe", body(b"MZ"))
    with pytest.raises(UnsupportedFileTypeError, match="UTF-8"):
        await store.save(ALICE, "users/alice/latin1.csv", body("café".encode("latin-1")))
    await store.save(ALICE, "users/alice/blob.bin", body(b"\xff\xfe"))  # non-text types are not decoded


async def test_no_silent_overwrite(store, tmp_path):
    await store.save(ALICE, "users/alice/a.csv", body(b"v1\n"))
    with pytest.raises(FileExistsConflictError):
        await store.save(ALICE, "users/alice/a.csv", body(b"v2\n"))
    _, replaced = await store.save(ALICE, "users/alice/a.csv", body(b"v2\n"), overwrite=True)
    assert replaced and (tmp_path / "uploads/users/alice/a.csv").read_bytes() == b"v2\n"


async def test_racing_uploads_to_same_name_one_wins(store, tmp_path):
    async def slow(data):
        await asyncio.sleep(0.01)
        yield data

    results = await asyncio.gather(
        store.save(ALICE, "users/alice/race.csv", slow(b"first\n")),
        store.save(ALICE, "users/alice/race.csv", slow(b"second\n")),
        return_exceptions=True,
    )
    assert sum(isinstance(r, FileExistsConflictError) for r in results) == 1
    assert (tmp_path / "uploads/users/alice/race.csv").read_bytes() in (b"first\n", b"second\n")


def test_hidden_files_are_invisible_to_the_file_tool(tmp_path):
    from orchestrator.tools import FileProcessingTool

    root = tmp_path / "uploads"
    (root / STAGING_DIR).mkdir(parents=True)
    (root / STAGING_DIR / "x.part").write_text("secret")
    (root / "ok.csv").write_text("a\n1\n")
    tool = FileProcessingTool(root)
    with pytest.raises(PermissionDeniedError):
        tool.resolve(f"{STAGING_DIR}/x.part")
    listed = asyncio.run(tool.op_list_files({}, None))["files"]
    assert listed == ["ok.csv"]


def test_example_policy_gives_analysts_private_folders():
    from pathlib import Path

    grants = load_grants(Path(__file__).resolve().parent.parent / "config/policy.example.json")
    policy = GrantPolicy(grants)
    from orchestrator.uploads import UPLOADS_SPEC

    op = UPLOADS_SPEC.operation("upload")
    assert policy.check(ALICE, UPLOADS_SPEC, op, "file:users/alice/q3.csv").allowed
    assert not policy.check(ALICE, UPLOADS_SPEC, op, "file:users/bob/q3.csv").allowed


# --- API, end to end -----------------------------------------------------------------------------------

KEYS = {"alice": "k-alice", "bob": "k-bob"}


def h(user):
    return {"Authorization": f"Bearer {KEYS[user]}"}


@pytest.fixture
def client(tmp_path):
    from orchestrator.permissions import PermissionManager  # noqa: F401  (create_app builds it)

    authn = ApiKeyAuthenticator(PrincipalRecord(user_id=u, roles=["analyst"], api_key_sha256=hash_api_key(k))
                                for u, k in KEYS.items())
    policy = tmp_path / "policy.json"
    policy.write_text('{"grants": ' + "[" + ",".join(g.model_dump_json() for g in PER_USER) + "]}")
    settings = Settings(llm_provider="rule_based", file_tool_root=tmp_path / "uploads", policy_path=policy,
                        upload_max_bytes=2048)
    with TestClient(create_app(settings=settings, authenticator=authn)) as c:
        yield c


def test_api_upload_then_ask_about_the_file(client):
    csv = b"month,Revenue\njan,100\nfeb,200\nmar,300\n"
    r = client.put("/files/users/alice/sales.csv", content=csv, headers=h("alice"))
    assert r.status_code == 201 and r.json()["size_bytes"] == len(csv)
    assert client.put("/files/users/alice/sales.csv", content=csv, headers=h("alice")).status_code == 409
    assert client.put("/files/users/alice/sales.csv?overwrite=true", content=csv, headers=h("alice")).status_code == 200

    r = client.post("/requests", headers=h("alice"),
                    json={"text": "calculate the average revenue in users/alice/sales.csv"})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["final_output"]["compute_mean"]["result"]["mean"] == 200.0

    # bob can neither see nor use alice's file
    assert client.get("/files", headers=h("bob")).json() == {"files": []}
    r = client.post("/requests", headers=h("bob"), json={"text": "calculate the average revenue in users/alice/sales.csv"})
    assert r.status_code == 403 and r.json()["state"] == "rejected"


def test_api_upload_errors(client):
    assert client.put("/files/users/alice/x.csv", content=b"a\n").status_code == 401
    assert client.put("/files/users/bob/x.csv", content=b"a\n", headers=h("alice")).status_code == 403
    r = client.put("/files/users/alice/x.exe", content=b"MZ", headers=h("alice"))
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_file_type"
    r = client.put("/files/users/alice/big.csv", content=b"x" * 5000, headers=h("alice"))
    assert r.status_code == 413 and r.json()["error"]["code"] == "upload_too_large"
    assert client.put("/files/users/alice/%2e%2e/escape.csv", content=b"a\n", headers=h("alice")).status_code in (403, 404)
    assert client.delete("/files/users/alice/missing.csv", headers=h("alice")).status_code == 404


def test_api_list_and_delete(client):
    client.put("/files/users/alice/a.csv", content=b"a\n", headers=h("alice"))
    client.put("/files/users/alice/notes.txt", content=b"hi", headers=h("alice"))
    assert [f["path"] for f in client.get("/files", headers=h("alice")).json()["files"]] == [
        "users/alice/a.csv", "users/alice/notes.txt"]
    assert [f["path"] for f in client.get("/files?pattern=*.csv", headers=h("alice")).json()["files"]] == [
        "users/alice/a.csv"]
    assert client.delete("/files/users/alice/a.csv", headers=h("bob")).status_code == 403
    assert client.delete("/files/users/alice/a.csv", headers=h("alice")).json() == {"deleted": "users/alice/a.csv"}
