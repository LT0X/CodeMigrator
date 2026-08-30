from codemigrator.core import load_verification_policy
from codemigrator.verification.routing import load_policy_snapshot


def test_verification_policy_is_versioned_and_snapshotted() -> None:
    snapshot = load_policy_snapshot()
    assert snapshot.flaky_reruns == 2
    assert snapshot.majority_required == 2
    assert snapshot.majority_total == 3
    assert snapshot.feedback_repair_limit == 2
    assert snapshot.conservation_bandwidth == (0.5, 2.0)
    assert snapshot.global_repair_attempts == 3
    assert len(snapshot.sha256) == 64

    payload = load_verification_policy()
    payload["global_repair_attempts"] = 99
    assert load_policy_snapshot().global_repair_attempts == 3
