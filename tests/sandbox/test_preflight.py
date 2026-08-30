from __future__ import annotations

from codemigrator.sandbox import PreflightFacts, PreflightRequirements, check_preflight


def facts(**overrides: object) -> PreflightFacts:
    values: dict[str, object] = {
        "kernel_release": "6.1.0",
        "cgroup_v2": True,
        "disk_quota": True,
        "bubblewrap_version": "0.8.0",
        "user_namespace": True,
        "architecture": "x86_64",
        "disk_free_bytes": 20 * 1024**3,
        "toolchain_image_digest": "sha256:" + "a" * 64,
        "seccomp_sha256": "b" * 64,
    }
    values.update(overrides)
    return PreflightFacts(**values)


def test_preflight_accepts_matching_hardened_runtime_facts() -> None:
    result = check_preflight(
        facts(),
        PreflightRequirements(
            toolchain_image_digest="sha256:" + "a" * 64,
            seccomp_sha256="b" * 64,
            min_disk_free_bytes=10 * 1024**3,
        ),
    )

    assert result.ready is True
    assert result.reasons == ()


def test_preflight_rejects_any_missing_capability_without_degrading() -> None:
    result = check_preflight(
        facts(cgroup_v2=False, bubblewrap_version="0.7.0"),
        PreflightRequirements(
            toolchain_image_digest="sha256:" + "a" * 64,
            seccomp_sha256="b" * 64,
        ),
    )

    assert result.ready is False
    assert {"cgroup_v2", "bubblewrap_version"}.issubset(result.reasons)
