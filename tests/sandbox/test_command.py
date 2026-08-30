from __future__ import annotations

import pytest
from pydantic import ValidationError

from codemigrator.core import CheckAction, CheckCommandTemplate
from codemigrator.sandbox import (
    BwrapPolicy,
    FrozenCommand,
    NetworkMode,
    ShellCommand,
    build_bwrap_argv,
    build_shell_bwrap_argv,
    freeze_check_command,
)


def template() -> CheckCommandTemplate:
    return CheckCommandTemplate(
        action=CheckAction.Test,
        program="uv",
        argv=["run", "pytest", "-q"],
        timeout_secs=120,
    )


def policy() -> BwrapPolicy:
    return BwrapPolicy(
        executable="/usr/bin/bwrap",
        rootfs="/opt/toolchain",
        validation_dir="/tmp/validation-1",
        cache_dir="/opt/cache",
        seccomp_fd=7,
        environment={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
    )


def test_frozen_command_and_bwrap_argv_are_descriptor_owned() -> None:
    command = freeze_check_command(
        template(),
        template_sha256="a" * 64,
        toolchain_image_digest="sha256:" + "b" * 64,
    )

    argv = build_bwrap_argv(policy(), command)

    assert isinstance(command, FrozenCommand)
    assert argv[:10] == [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--new-session",
        "--die-with-parent",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/opt/toolchain",
        "/",
    ]
    assert "--seccomp" in argv
    assert argv[-5:] == ["--", "uv", "run", "pytest", "-q"]
    assert "--bind" in argv
    assert "/tmp/validation-1" in argv


def test_bwrap_policy_rejects_host_sensitive_mounts_and_environment() -> None:
    with pytest.raises(ValueError, match="forbidden mount"):
        BwrapPolicy(
            executable="/usr/bin/bwrap",
            rootfs="/opt/toolchain",
            validation_dir="/tmp/validation-1",
            extra_read_only_mounts=(("/var/run/docker.sock", "/docker.sock"),),
        )

    with pytest.raises(ValidationError, match="environment"):
        BwrapPolicy(
            executable="/usr/bin/bwrap",
            rootfs="/opt/toolchain",
            validation_dir="/tmp/validation-1",
            environment={"SSH_AUTH_SOCK": "/tmp/agent.sock"},
        )


def test_shell_network_mode_is_explicit_and_proxy_variables_are_allowlisted() -> None:
    shell_policy = BwrapPolicy(
        executable="/usr/bin/bwrap",
        rootfs="/opt/toolchain",
        validation_dir="/tmp/validation-1",
        network_mode=NetworkMode.Shell,
        proxy_url="http://127.0.0.1:3128",
        environment={"HTTP_PROXY": "http://127.0.0.1:3128"},
    )

    command = freeze_check_command(
        template(),
        template_sha256="a" * 64,
        toolchain_image_digest="sha256:" + "b" * 64,
    )
    assert "HTTP_PROXY" in build_bwrap_argv(shell_policy, command)
    shell_argv = build_shell_bwrap_argv(
        shell_policy, ShellCommand(program="sh", argv=("-c", "echo feedback"))
    )
    assert shell_argv[-4:] == ["--", "sh", "-c", "echo feedback"]
    with pytest.raises(ValueError, match="shell network profile"):
        build_shell_bwrap_argv(policy(), ShellCommand(program="sh"))
