"""Deterministic Git verified-ref CAS and Run-owner receipt recovery."""

from __future__ import annotations

from dataclasses import dataclass

from codemigrator.core import IntegrationIntent, RunId, StableErrorCode
from codemigrator.runtime.contracts import ActorPhaseReceipt
from codemigrator.workspace import (
    CandidateRefConflict,
    GitRunRepository,
    RecoveryAction,
    classify_integration_recovery,
)

from .actor import RunActor


class IntegrationRecoveryError(RuntimeError):
    """An intent and its Git verified ref cannot be reconciled safely."""

    def __init__(self, code: StableErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class RunIntegrationDriver:
    """Apply an already-validated intent and adopt its verified-ref receipt."""

    actor: RunActor
    repository: GitRunRepository

    async def integrate(self, intent: IntegrationIntent) -> ActorPhaseReceipt:
        run_id = RunId(intent.run_id)
        if self.actor.run_id != run_id or RunId(self.repository.run_id) != run_id:
            raise ValueError("integration intent, actor, and Git repository must share a Run")

        await self.actor.persist_integration_intent(intent)
        receipt = await self.actor.store.load_integration_receipt(
            run_id, str(intent.idempotency_key)
        )
        if receipt is not None:
            if (
                receipt.intent != intent
                or receipt.verified_commit_oid != intent.prospective_commit_oid
            ):
                raise IntegrationRecoveryError(StableErrorCode.RECOVERY_LEDGER_INCONSISTENT)
            return await self.actor.complete_integration(intent, intent.prospective_commit_oid)

        if self.repository.parents(intent.prospective_commit_oid) != (
            intent.expected_verified_oid,
        ):
            raise IntegrationRecoveryError(StableErrorCode.RECOVERY_LEDGER_INCONSISTENT)

        verified_ref = self.repository.refs.verified
        for attempt in range(2):
            observed = self.repository.resolve(verified_ref)
            decision = classify_integration_recovery(
                intent.expected_verified_oid,
                intent.prospective_commit_oid,
                observed,
                receipt_exists=False,
            )
            if decision.action is RecoveryAction.COMPLETE_RECEIPT:
                return await self.actor.complete_integration(intent, intent.prospective_commit_oid)
            if decision.action is RecoveryAction.INCONSISTENT:
                raise IntegrationRecoveryError(
                    decision.code or StableErrorCode.RECOVERY_LEDGER_INCONSISTENT
                )

            try:
                self.repository.update_ref(
                    verified_ref,
                    intent.prospective_commit_oid,
                    intent.expected_verified_oid,
                )
            except CandidateRefConflict:
                # Re-read once: another writer may have applied this exact CAS already.
                pass

            observed = self.repository.resolve(verified_ref)
            decision = classify_integration_recovery(
                intent.expected_verified_oid,
                intent.prospective_commit_oid,
                observed,
                receipt_exists=False,
            )
            if decision.action is RecoveryAction.COMPLETE_RECEIPT:
                return await self.actor.complete_integration(intent, intent.prospective_commit_oid)
            if decision.action is RecoveryAction.INCONSISTENT:
                raise IntegrationRecoveryError(
                    decision.code or StableErrorCode.RECOVERY_LEDGER_INCONSISTENT
                )
            if attempt == 1:
                raise CandidateRefConflict("verified ref did not advance after bounded CAS retry")

        raise CandidateRefConflict("verified ref CAS retry was exhausted")


__all__ = ["IntegrationRecoveryError", "RunIntegrationDriver"]
