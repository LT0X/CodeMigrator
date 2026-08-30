from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from codemigrator.workspace import (
    EditFileCall,
    LineRange,
    QuerySourceAstCall,
    ReadFileCall,
    ToolCall,
    WriteFileCall,
)


def test_tool_calls_are_closed_and_discriminated() -> None:
    adapter = TypeAdapter(ToolCall)

    call = adapter.validate_python({"tool": "ReadFile", "path": "src/a.py"})
    assert isinstance(call, ReadFileCall)

    with pytest.raises(ValidationError):
        adapter.validate_python(
            {"tool": "WriteFile", "path": "src/a.py", "content": "x", "extra": 1}
        )

    with pytest.raises(ValidationError):
        adapter.validate_python({"tool": "ReadFile", "path": "src/a.py", "cas": "cas://abc"})


def test_file_call_limits_and_edit_range_are_explicit() -> None:
    assert LineRange(start_line=2, end_line=3).end_line == 3
    with pytest.raises(ValidationError):
        LineRange(start_line=3, end_line=2)
    with pytest.raises(ValidationError):
        WriteFileCall(tool="WriteFile", path="src/a.py", content="x" * (64 * 1024**2 + 1))
    with pytest.raises(ValidationError):
        EditFileCall(tool="EditFile", path="src/a.py", old_text="", new_text="x")


def test_query_source_ast_reuses_analysis_closed_schema() -> None:
    call = QuerySourceAstCall(
        tool="QuerySourceAst",
        request={"kind": "FIND_SYMBOL", "symbol": "render"},
    )
    assert call.request.kind == "FIND_SYMBOL"
    with pytest.raises(ValidationError):
        QuerySourceAstCall(tool="QuerySourceAst", request={"kind": "FIND_SYMBOL"})


def test_text_limits_are_utf8_byte_limits() -> None:
    with pytest.raises(ValidationError):
        WriteFileCall(tool="WriteFile", path="src/a.py", content="中" * (64 * 1024**2))
