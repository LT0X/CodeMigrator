"""Inline bubblewrap execution primitives for CodeMigrator."""

from .command import (
    BwrapPolicy,
    FrozenCommand,
    NetworkMode,
    ShellCommand,
    build_bwrap_argv,
    build_shell_bwrap_argv,
    freeze_check_command,
)
from .executor import ExecutionReceipt, NetworkAttachment, SandboxExecutor, TerminationReceipt
from .lifecycle import (
    CgroupProcessDomain,
    TemporaryValidationDirectory,
    pdeathsig_preexec,
    terminate_process_group,
)
from .limits import (
    DEFAULT_RESOURCE_LIMITS,
    ResourceLimits,
    calculate_pool_capacity,
    validation_directory_exceeds_quota,
)
from .pool import SandboxExecutionPool
from .preflight import PreflightFacts, PreflightRequirements, PreflightResult, check_preflight
from .proxy import AsyncForwardProxy, DomainAllowlist, ProxyAuditEvent, proxy_environment
from .termination import TerminationCause, TerminationDecision, reduce_termination

__all__ = [
    "AsyncForwardProxy",
    "BwrapPolicy",
    "CgroupProcessDomain",
    "DEFAULT_RESOURCE_LIMITS",
    "DomainAllowlist",
    "ExecutionReceipt",
    "FrozenCommand",
    "NetworkMode",
    "NetworkAttachment",
    "PreflightFacts",
    "PreflightRequirements",
    "PreflightResult",
    "ProxyAuditEvent",
    "ResourceLimits",
    "SandboxExecutionPool",
    "SandboxExecutor",
    "ShellCommand",
    "TemporaryValidationDirectory",
    "TerminationCause",
    "TerminationDecision",
    "TerminationReceipt",
    "build_bwrap_argv",
    "build_shell_bwrap_argv",
    "calculate_pool_capacity",
    "check_preflight",
    "freeze_check_command",
    "pdeathsig_preexec",
    "proxy_environment",
    "reduce_termination",
    "terminate_process_group",
    "validation_directory_exceeds_quota",
]
