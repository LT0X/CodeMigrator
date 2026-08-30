"""Analysis-layer failures with stable, package-local error codes."""

from codemigrator.core import StableErrorCode


class AnalysisFailure(RuntimeError):
    def __init__(self, code: StableErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


class GrammarFailure(AnalysisFailure):
    pass


__all__ = ["AnalysisFailure", "GrammarFailure"]
