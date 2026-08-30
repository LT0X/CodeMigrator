from __future__ import annotations

import uuid

import pytest

from codemigrator.core import (
    ArtifactRef,
    CheckAction,
    CheckCommandTemplate,
    CheckId,
    CheckResult,
    ReceiptId,
    RequiredCheck,
    Sha256,
    SliceId,
    WriteScope,
    WriteScopeOut,
)


def uid() -> uuid.UUID:
    return uuid.uuid4()


def check(action: CheckAction, digest: str | None = None) -> RequiredCheck:
    digest = digest or (str(action.value).lower().replace("_", "") + "a" * 60)[:64]
    return RequiredCheck(id=CheckId(uid()), action=action, template_sha256=Sha256(digest))


def template(action: CheckAction, program: str) -> CheckCommandTemplate:
    return CheckCommandTemplate(action=action, program=program, argv=["-q"], timeout_secs=30)


def artifact(fill: str = "a") -> ArtifactRef:
    return ArtifactRef(sha256=Sha256(fill * 64), size=1, media_type="text/plain")


def result(
    required: RequiredCheck,
    *,
    status: str = "PASSED",
    invocation_hash: str = "b" * 64,
    diagnostics: list[dict[str, object]] | None = None,
    receipt_id: uuid.UUID | None = None,
    artifact_fill: str = "a",
) -> CheckResult:
    return CheckResult(
        check_id=required.id,
        invocation_hash=Sha256(invocation_hash),
        status=status,
        receipt_id=ReceiptId(receipt_id or uid()),
        stdout=artifact(artifact_fill),
        stderr=artifact("c"),
        diagnostics=diagnostics or [],
    )


@pytest.fixture
def slice_id() -> SliceId:
    return SliceId(uid())


@pytest.fixture
def write_scope(slice_id: SliceId) -> dict[SliceId, WriteScope]:
    return {
        slice_id: WriteScope(out=WriteScopeOut(write_paths=["src/app.py"], create_roots=["src"]))
    }


__all__ = ["artifact", "check", "result", "slice_id", "template", "uid"]
