from __future__ import annotations

from pathlib import Path

from codemigrator.core import StableErrorCode
from codemigrator.workspace import ToolError, ToolGateway


def test_gateway_rejects_unsafe_path_before_bound_root_open(
    roots, execute_context, write_scope
) -> None:
    gateway = ToolGateway(context=execute_context, roots=roots, write_scope=write_scope)

    result = gateway.dispatch({"tool": "ReadFile", "path": "../secret"})

    assert isinstance(result, ToolError)
    assert result.code is StableErrorCode.PATH_DENIED
    assert roots.open_count == 0


def test_gateway_rejects_symlinked_intermediate_directory(
    roots, execute_context, write_scope
) -> None:
    outside = Path(roots.snapshot.path).parent / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret", encoding="utf-8")
    (roots.snapshot.path / "linked").symlink_to(outside, target_is_directory=True)
    gateway = ToolGateway(context=execute_context, roots=roots, write_scope=write_scope)

    result = gateway.dispatch({"tool": "ReadFile", "path": "linked/secret.py"})

    assert isinstance(result, ToolError)
    assert result.code is StableErrorCode.PATH_DENIED
