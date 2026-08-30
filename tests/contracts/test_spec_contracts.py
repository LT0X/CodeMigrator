import hashlib

from codemigrator.core import StableErrorCode
from codemigrator.core.spec import SpecProblem, limit_problems


def test_problem_order_is_json_pointer_ordered_and_limited_to_100() -> None:
    problems = [
        SpecProblem(
            pointer=f"/field/{index:03d}", code=StableErrorCode.SPEC_SCHEMA_INVALID, message="bad"
        )
        for index in range(120, -1, -1)
    ]

    limited = limit_problems(problems)

    assert len(limited.problems) == 100
    assert limited.problems[0].pointer == "/field/000"
    assert limited.problems[-1].pointer == "/field/099"
    assert limited.truncated is True


def test_problem_payload_is_not_a_second_error_code_definition() -> None:
    problem = SpecProblem(
        pointer="/version", code=StableErrorCode.SPEC_SCHEMA_UNSUPPORTED, message="unsupported"
    )

    assert problem.code is StableErrorCode.SPEC_SCHEMA_UNSUPPORTED
    assert hashlib.sha256(problem.code.value.encode()).hexdigest()
