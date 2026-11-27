"""Signed, hash-chained receipts: proof of what ran, under which rules, producing what.

Every finished request gets a ``Receipt``. It holds **hashes, ids and statuses, never data**:

* the question, the permission policy in force and the plan (each task's tool, operation and a
  hash of its planned inputs);
* every step: status, the hash of its exact resolved arguments (the one approvals bind to), the hash
  of its output, timings, and the approval it used and who decided it;
* the written answer: hashes of its text and evidence, and each claim's verification status;
* the receipts of earlier requests a follow-up built on.

Receipts form a **chain**: each carries a sequence number and the hash of the previous receipt, so
removing, reordering or editing one breaks every later link. With a key configured each receipt is
**signed** (Ed25519); anyone holding the public key can check it offline (``python -m orchestrator
verify-receipt``) — and compare it against the full request record to show the record was not changed
after the fact. Without a key, receipts are still chained but unsigned, and the server warns.

Because a receipt contains no data, it can be handed to an auditor or a customer without revealing
file contents, search results or the answer itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.errors import ConfigurationError

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator.core import RequestRecord

RECEIPT_VERSION = 1
GENESIS = "sha256:" + "0" * 64


def canonical(value: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, UTF-8. Hashes and signatures use this."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def digest(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical(value)
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------- model


class StepReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    tool_id: str | None
    operation: str | None
    status: str
    args_sha256: str | None = Field(default=None, description="Hash of the resolved arguments that ran.")
    output_sha256: str | None = None
    error: str | None = None
    approval_id: str | None = None
    approved_by: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class AnswerReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    text_sha256: str
    evidence_sha256: str
    model: str | None = None
    claims: tuple[str, ...] = Field(default=(), description="Verification status of each claim, in order.")


class Receipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = RECEIPT_VERSION
    sequence: int
    previous: str = Field(description="receipt_sha256 of the previous receipt in the chain (GENESIS for the first).")
    request_id: str
    user_id: str
    issued_at: str
    state: str
    question_sha256: str
    files: tuple[str, ...] = ()
    context: dict[str, str | None] = Field(default_factory=dict, description="earlier request id -> its receipt hash")
    planner: str | None = None
    policy_sha256: str
    plan_sha256: str | None = None
    steps: tuple[StepReceipt, ...] = ()
    answer: AnswerReceipt | None = None
    error: str | None = None
    code_sha256: str | None = Field(default=None, description="Hash of a code answer's source, when there is one.")
    key_id: str | None = None
    signature: str | None = Field(default=None, description="Ed25519 over the canonical receipt without this field.")

    def _dump(self, *, signature: bool) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude=None if signature else {"signature"})
        # Fields added after version 1 enter the signed and hashed content only when set, so receipts
        # issued before they existed keep verifying and the chain stays intact.
        if data.get("code_sha256") is None:
            data.pop("code_sha256", None)
        return data

    def body(self) -> dict[str, Any]:
        """What is signed: everything except the signature."""
        return self._dump(signature=False)

    @property
    def receipt_sha256(self) -> str:
        """The chain link: hash of the full receipt, signature included."""
        return digest(self._dump(signature=True))


# --------------------------------------------------------------------------------- building


def policy_digest(grants: Iterable[Any]) -> str:
    return digest([g.model_dump(mode="json") for g in grants])


def plan_digest(record: "RequestRecord") -> str | None:
    if record.plan is None:
        return None
    return digest([{"task_id": t.task_id, "tool_id": t.tool_id, "operation": t.operation,
                    "inputs": t.inputs, "depends_on": list(t.depends_on)} for t in record.plan.tasks])


def answer_digests(record: "RequestRecord") -> AnswerReceipt | None:
    a = record.answer
    if a is None:
        return None
    return AnswerReceipt(text_sha256=digest(a.text), evidence_sha256=digest([e.model_dump(mode="json") for e in a.evidence]),
                         model=a.model, claims=tuple(c.status for c in a.claims))


def step_receipts(record: "RequestRecord", approver: Callable[[str], str | None]) -> tuple[StepReceipt, ...]:
    if record.report is None:
        return ()
    steps = []
    for r in record.report.task_results:
        steps.append(StepReceipt(
            task_id=r.task_id, tool_id=r.tool_id, operation=r.operation, status=r.status.value,
            args_sha256=f"sha256:{r.args_digest}" if r.args_digest else None,
            output_sha256=digest(r.output) if r.output is not None else None,
            error=r.error.code if r.error else None,
            approval_id=r.approval_id, approved_by=approver(r.approval_id) if r.approval_id else None,
            started_at=r.started_at.isoformat() if r.started_at else None,
            finished_at=r.finished_at.isoformat() if r.finished_at else None,
        ))
    return tuple(steps)


# --------------------------------------------------------------------------------- signing


class Signer:
    """Ed25519 signing with a private key in a PEM file (``python -m orchestrator receipt-key``)."""

    def __init__(self, key_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        try:
            key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigurationError(f"cannot load the receipt signing key {key_path}: {type(exc).__name__}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ConfigurationError(f"{key_path} is not an Ed25519 private key")
        self._key = key
        self.public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.key_id = key_id(self.public_pem)

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self._key.sign(message)).decode()


def key_id(public_pem: str) -> str:
    """Short, stable fingerprint of a public key: the first 16 hex digits of its SHA-256."""
    from cryptography.hazmat.primitives import serialization

    pub = serialization.load_pem_public_key(public_pem.encode())
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_key(path: Path) -> str:
    """Write a new Ed25519 private key (PEM, owner-only where the OS allows) and return the public PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = Path(path)
    if path.exists():
        raise ConfigurationError(f"{path} already exists; refusing to overwrite a signing key")
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    try:
        path.chmod(0o600)
    except OSError:  # Windows: inherits the folder's ACL
        pass
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


# --------------------------------------------------------------------------------- chain storage


class ReceiptChain:
    """Where receipts are kept and the chain head lives. In memory by default; ``state.SqliteReceipts``
    keeps them durable alongside requests."""

    def __init__(self) -> None:
        self._receipts: list[Receipt] = []

    def head(self) -> tuple[int, str]:
        return (self._receipts[-1].sequence, self._receipts[-1].receipt_sha256) if self._receipts else (0, GENESIS)

    def append(self, receipt: Receipt) -> None:
        self._receipts.append(receipt)

    def get(self, request_id: str) -> Receipt | None:
        return next((r for r in reversed(self._receipts) if r.request_id == request_id), None)

    def since(self, sequence: int = 0, limit: int = 500) -> list[Receipt]:
        return [r for r in self._receipts if r.sequence > sequence][:limit]


class ReceiptIssuer:
    def __init__(self, chain: ReceiptChain | None = None, signer: Signer | None = None,
                 policy_grants: Callable[[], Iterable[Any]] = tuple, approver: Callable[[str], str | None] = lambda _: None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.chain = chain or ReceiptChain()
        self.signer = signer
        self._grants, self._approver, self._now = policy_grants, approver, now
        self._lock = threading.Lock()

    @property
    def public_key(self) -> tuple[str, str] | None:
        return (self.signer.public_pem, self.signer.key_id) if self.signer else None

    def issue(self, record: "RequestRecord") -> Receipt:
        context = {}
        for rid in record.context:
            earlier = self.chain.get(rid)
            context[rid] = earlier.receipt_sha256 if earlier else None
        with self._lock:  # one link at a time: sequence and previous must not race
            sequence, previous = self.chain.head()
            receipt = Receipt(
                sequence=sequence + 1, previous=previous, request_id=record.request_id, user_id=record.user_id,
                issued_at=self._now().isoformat(), state=record.state.value, question_sha256=digest(record.text),
                files=tuple(record.files), context=context, planner=record.planner,
                policy_sha256=policy_digest(self._grants()), plan_sha256=plan_digest(record),
                steps=step_receipts(record, self._approver), answer=answer_digests(record),
                error=record.error.code if record.error else None,
                code_sha256=digest(record.code.code) if getattr(record, "code", None) is not None else None,
                key_id=self.signer.key_id if self.signer else None,
            )
            if self.signer:
                receipt = receipt.model_copy(update={"signature": self.signer.sign(canonical(receipt.body()))})
            self.chain.append(receipt)
        return receipt


# --------------------------------------------------------------------------------- verification


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: Literal["ok", "warning", "error"]
    message: str


def verify_signature(receipt: Receipt, public_pem: str) -> Finding:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    if not receipt.signature:
        return Finding(level="warning", message="receipt is not signed (no signing key was configured)")
    if receipt.key_id and receipt.key_id != key_id(public_pem):
        return Finding(level="error", message=f"receipt was signed by key {receipt.key_id}, not this public key")
    try:
        serialization.load_pem_public_key(public_pem.encode()).verify(
            base64.b64decode(receipt.signature), canonical(receipt.body()))
    except (InvalidSignature, ValueError):
        return Finding(level="error", message="signature does not match: the receipt was changed or signed by another key")
    return Finding(level="ok", message=f"signature valid (key {receipt.key_id})")


def verify_against_record(receipt: Receipt, record: dict[str, Any]) -> list[Finding]:
    """Recompute every hash from a full request record (as returned by GET /requests/{id})."""
    from orchestrator.core import RequestRecord

    rec = RequestRecord.model_validate(record)
    findings = []

    def check(name: str, expected: Any, actual: Any) -> None:
        ok = expected == actual
        findings.append(Finding(level="ok" if ok else "error",
                                message=f"{name}: {'matches' if ok else 'does NOT match'} the record"))

    check("request id", receipt.request_id, rec.request_id)
    check("question", receipt.question_sha256, digest(rec.text))
    check("state", receipt.state, rec.state.value)
    check("plan", receipt.plan_sha256, plan_digest(rec))
    recorded = {s.task_id: s for s in step_receipts(rec, lambda _: None)}
    for step in receipt.steps:
        mine = recorded.get(step.task_id)
        if mine is None:
            findings.append(Finding(level="error", message=f"step {step.task_id} is missing from the record"))
            continue
        check(f"step {step.task_id} output", step.output_sha256, mine.output_sha256)
        check(f"step {step.task_id} arguments", step.args_sha256, mine.args_sha256)
        check(f"step {step.task_id} status", step.status, mine.status)
    extra = set(recorded) - {s.task_id for s in receipt.steps}
    if extra:
        findings.append(Finding(level="error", message=f"record has steps the receipt does not: {sorted(extra)}"))
    if receipt.code_sha256 is not None or rec.code is not None:
        check("code", receipt.code_sha256, digest(rec.code.code) if rec.code is not None else None)
    mine_answer = answer_digests(rec)
    check("answer", receipt.answer.model_dump() if receipt.answer else None,
          mine_answer.model_dump() if mine_answer else None)
    return findings


def verify_chain(receipts: list[Receipt]) -> list[Finding]:
    """Consecutive sequence numbers, each linking to the hash of the one before."""
    findings = []
    ordered = sorted(receipts, key=lambda r: r.sequence)
    for before, after in zip(ordered, ordered[1:]):
        if after.sequence != before.sequence + 1:
            findings.append(Finding(level="error", message=f"gap in the chain between #{before.sequence} and #{after.sequence}"))
        elif after.previous != before.receipt_sha256:
            findings.append(Finding(level="error", message=f"receipt #{after.sequence} does not link to #{before.sequence}"))
    if ordered and ordered[0].sequence == 1 and ordered[0].previous != GENESIS:
        findings.append(Finding(level="error", message="receipt #1 does not start the chain"))
    if not findings:
        findings.append(Finding(level="ok", message=f"chain intact over {len(ordered)} receipt(s)"))
    return findings
