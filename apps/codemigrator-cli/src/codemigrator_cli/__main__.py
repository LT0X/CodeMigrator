from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from .cancel import CancelAction, CancelController
from .client import (
    EventSource,
    HttpRunControl,
    MockEventSource,
    MockRunControl,
    RunControl,
    StaleVersionError,
)
from .exit_codes import ExitCode
from .http import HttpEventSource
from .models import RunEvent
from .projector import project_events, safe_data
from .renderer import render_human, render_json, render_jsonl, render_jsonl_event


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codemigrator", description="CodeMigrator 迁移观察客户端")
    subparsers = parser.add_subparsers(dest="command", required=True)
    migrate = subparsers.add_parser("migrate", help="迁移操作")
    migrate_sub = migrate.add_subparsers(dest="migrate_command", required=True)
    start = migrate_sub.add_parser("start", help="运行本地确定性迁移演示")
    start.add_argument("spec")
    _add_output_flags(start)
    project = migrate_sub.add_parser("project", help="迁移本地项目并按阶段验证")
    project.add_argument("source", type=Path)
    project.add_argument("--target", type=Path, required=True)
    project.add_argument("--api-key-file", type=Path, required=True)
    project.add_argument("--state-dir", type=Path)
    project.add_argument("--resume", action="store_true")
    project.add_argument(
        "--workflow",
        choices=("full", "legacy"),
        default="full",
        help="选择完整 V6 起草-规划-执行流程，或显式使用兼容 runner",
    )
    project.add_argument(
        "--from-phase",
        choices=("PREFLIGHT", "ANALYSIS", "PLAN", "EXECUTE", "VERIFY", "REPORT"),
    )
    project.add_argument("--parallelism", type=_positive_int, default=4)
    project.add_argument("--output", choices=("human", "json"), default="human")
    run = subparsers.add_parser("run", help="Run 操作")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    watch = run_sub.add_parser("watch", help="观察 Run")
    watch.add_argument("run_id")
    _add_output_flags(watch)
    show = run_sub.add_parser("show", help="查看 Run 摘要")
    show.add_argument("run_id")
    show.add_argument("--output", choices=("human", "json", "jsonl"), default="human")
    cancel = run_sub.add_parser("cancel", help="请求取消 Run")
    cancel.add_argument("run_id")
    cancel.add_argument("--if-match", type=_non_negative_int, required=True)
    cancel.add_argument("--output", choices=("human", "json", "jsonl"), default="human")
    return parser


def _add_output_flags(parser: argparse.ArgumentParser) -> None:
    follow = parser.add_mutually_exclusive_group()
    follow.add_argument("--follow", action="store_true")
    follow.add_argument("--no-follow", action="store_true")
    parser.add_argument("--output", choices=("human", "json", "jsonl"), default="human")


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _render_start(output: str) -> str:
    if output == "json":
        return (
            json.dumps(
                {
                    "run_id": "mock-run-001",
                    "status": "CREATED",
                    "web_url": "/runs/mock-run-001",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    if output == "jsonl":
        return '{"run_id":"mock-run-001","status":"CREATED"}\n'
    return "已创建 Run mock-run-001\nWeb: /runs/mock-run-001\n"


def _render_project(report: dict[str, object], output: str) -> str:
    if output == "json":
        return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    status = report.get("status", "UNKNOWN")
    phase = report.get("phase", report.get("stage", "UNKNOWN"))
    failed = report.get("failed_files", [])
    failed_count = len(failed) if isinstance(failed, list) else 0
    return (
        f"项目迁移 {status} · phase={phase} · "
        f"translated={report.get('translated_files', 0)} · "
        f"copied={report.get('copied_files', 0)} · failed={failed_count}\n"
    )


def _run_project_command(args: argparse.Namespace) -> tuple[int, str]:
    from codemigrator.runtime import (
        OpenAIProjectTranslator,
        ProjectMigrationPipeline,
        ProjectMigrationPipelineRequest,
        ProjectMigrationRequest,
        ProjectMigrationRunner,
    )

    if args.workflow == "full" and args.from_phase is not None:
        return (
            int(ExitCode.UNKNOWN),
            _render_project(
                {
                    "status": "UNKNOWN",
                    "stage": "PREFLIGHT",
                    "errors": [
                        "--from-phase is only supported with --workflow legacy; "
                        "full workflow resumes from its checkpoint"
                    ],
                },
                args.output,
            ),
        )

    translator = None
    try:
        translator = OpenAIProjectTranslator.from_key_file(args.api_key_file)
        if args.workflow == "legacy":
            legacy_report = ProjectMigrationRunner().run(
                ProjectMigrationRequest(
                    source=args.source,
                    target=args.target,
                    state_dir=args.state_dir,
                    resume=args.resume,
                    from_phase=args.from_phase,
                    translator=translator,
                    max_parallelism=args.parallelism,
                )
            )
            payload = legacy_report.as_dict()
        else:
            full_report = ProjectMigrationPipeline().run(
                ProjectMigrationPipelineRequest(
                    source=args.source,
                    target=args.target,
                    state_dir=args.state_dir,
                    resume=args.resume,
                    translator=translator,
                    max_parallelism=args.parallelism,
                )
            )
            payload = full_report.as_dict()
    except (OSError, ValueError, RuntimeError):
        return int(ExitCode.UNKNOWN), _render_project({"status": "UNKNOWN"}, args.output)
    finally:
        if translator is not None:
            translator.close()
    return (
        int(ExitCode.COMPLETED if payload.get("status") == "COMPLETED" else ExitCode.FAILED),
        _render_project(payload, args.output),
    )


def _render_payload(payload: dict[str, object], output: str) -> str:
    safe_payload: dict[str, object] = {}
    run_id = payload.get("run_id")
    status = payload.get("status")
    version = payload.get("version")
    web_url = payload.get("web_url")
    if isinstance(run_id, str):
        safe_payload["run_id"] = run_id
    if isinstance(status, str):
        safe_payload["status"] = status
    if type(version) is int and version >= 0:
        safe_payload["version"] = version
    if isinstance(web_url, str) and web_url.startswith("/") and not web_url.startswith("//"):
        safe_payload["web_url"] = web_url
    payload = safe_payload
    if output == "json":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if output == "jsonl":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return (
        f"Run {payload.get('run_id', 'unknown')} · "
        f"{payload.get('status', 'UNKNOWN')} · version {payload.get('version', '?')}\n"
    )


def _status_exit(status: object) -> int:
    return {
        "COMPLETED": int(ExitCode.COMPLETED),
        "PARTIALLY_COMPLETED": int(ExitCode.PARTIALLY_COMPLETED),
        "FAILED": int(ExitCode.FAILED),
        "CANCELLED": int(ExitCode.CANCELLED),
    }.get(str(status), int(ExitCode.UNKNOWN))


def run_command(
    argv: Sequence[str],
    *,
    source: EventSource | None = None,
    control: RunControl | None = None,
    on_event: Callable[[RunEvent], None] | None = None,
) -> tuple[int, str]:
    args = _parser().parse_args(list(argv))
    if args.command == "migrate" and args.migrate_command == "project":
        return _run_project_command(args)
    run_control = control or _configured_run_control()
    if args.command == "run" and args.run_command == "show":
        try:
            show_payload = run_control.show(args.run_id)
        except Exception:
            return int(ExitCode.UNKNOWN), _render_payload(
                {"run_id": args.run_id, "status": "UNKNOWN"}, args.output
            )
        return _status_exit(show_payload.get("status")), _render_payload(show_payload, args.output)
    if args.command == "run" and args.run_command == "cancel":
        try:
            cancel_payload = run_control.cancel(args.run_id, args.if_match)
        except StaleVersionError:
            cancel_payload = {"run_id": args.run_id, "status": "STALE_VERSION"}
        except Exception:
            cancel_payload = {"run_id": args.run_id, "status": "UNKNOWN"}
        return _status_exit(cancel_payload.get("status")), _render_payload(
            cancel_payload, args.output
        )
    if args.no_follow and args.command == "migrate":
        return int(ExitCode.COMPLETED), _render_start(args.output)
    try:
        events: Iterable[RunEvent] = _observe_events(
            source or _configured_event_source(args),
            on_event=on_event,
        )
        if args.output == "jsonl":
            event_list = list(events)
            projection = project_events(event_list)
            output = "\n".join(render_jsonl(event_list)) + "\n"
            return _status_exit(projection.run_status), "" if on_event is not None else output
        projection = project_events(events)
        if args.output == "json":
            return _status_exit(projection.run_status), render_json(projection) + "\n"
        return _status_exit(projection.run_status), render_human(projection)
    except _CancelConfirmed:
        raise
    except Exception:
        error_payload: dict[str, object] = {"status": "UNKNOWN"}
        if args.command == "run":
            error_payload["run_id"] = args.run_id
        return int(ExitCode.UNKNOWN), _render_payload(error_payload, args.output)


def _observe_events(
    source: EventSource, *, on_event: Callable[[RunEvent], None] | None
) -> Iterable[RunEvent]:
    for event in source.events():
        if on_event is not None:
            on_event(event)
        yield event


def _configured_run_control() -> RunControl:
    base_url = os.environ.get("CODEMIGRATOR_API_URL")
    token = os.environ.get("CODEMIGRATOR_API_TOKEN")
    if base_url and token:
        return HttpRunControl(base_url, token=token)
    return MockRunControl()


def _configured_event_source(args: argparse.Namespace) -> EventSource:
    base_url = os.environ.get("CODEMIGRATOR_API_URL")
    token = os.environ.get("CODEMIGRATOR_API_TOKEN")
    if args.command == "run" and args.run_command == "watch" and base_url and token:
        return HttpEventSource(base_url, args.run_id, token=token)
    return MockEventSource()


class _CancelConfirmed(Exception):
    """Stop an interactive watch after the API persisted cancellation."""


def _interrupt_run_id(arguments: Sequence[str]) -> str | None:
    parsed = _parser().parse_args(list(arguments))
    if parsed.command == "run" and parsed.run_command == "watch":
        run_id = parsed.run_id
        return run_id if isinstance(run_id, str) else None
    if parsed.command == "migrate" and parsed.migrate_command == "start":
        return "mock-run-001"
    return None


def _request_interrupt_cancel(
    control: RunControl, run_id: str, controller: CancelController, output: str
) -> None:
    try:
        current = control.show(run_id)
        version = current.get("version")
        if type(version) is not int or version < 0:
            raise ValueError("Run version is invalid")
        payload = control.cancel(run_id, version)
    except StaleVersionError:
        try:
            latest = control.show(run_id)
            latest_version = latest.get("version")
            if type(latest_version) is not int or latest_version < 0:
                raise ValueError("Run version is invalid")
            payload = control.cancel(run_id, latest_version)
        except StaleVersionError:
            sys.stderr.write("取消请求的 Run 版本再次过期，取消未确认。\n")
            return
        except Exception:
            sys.stderr.write("无法刷新 Run 版本，取消未确认。\n")
            return
    except Exception:
        sys.stderr.write("无法提交取消请求；Run 状态保持未知。\n")
        return
    status = payload.get("status")
    if isinstance(status, str):
        controller.observe(status)
    if controller.confirmed:
        sys.stdout.write(_render_payload(payload, output))
        sys.stdout.flush()
        raise _CancelConfirmed
    sys.stderr.write("取消请求已提交，正在等待 Run actor 的持久化确认。\n")


def _render_process_event(event: RunEvent) -> None:
    data = safe_data(event.data)
    target = data.get("slice_id", data.get("sliceId"))
    suffix = f" · {target}" if isinstance(target, str) else ""
    sys.stdout.write(f"#{event.sequence} {event.type}{suffix}\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    is_observer = len(arguments) >= 2 and tuple(arguments[:2]) in {
        ("migrate", "start"),
        ("run", "watch"),
    }
    if (
        is_observer
        and not sys.stdout.isatty()
        and "--follow" not in arguments
        and "--no-follow" not in arguments
    ):
        arguments.append("--no-follow")
    if is_observer and not sys.stdout.isatty() and "--output" not in arguments:
        arguments.extend(["--output", "jsonl" if "--follow" in arguments else "json"])
    control = _configured_run_control()
    controller = CancelController()
    run_id = _interrupt_run_id(arguments) if is_observer else None
    parsed = _parser().parse_args(arguments)
    event_history: list[RunEvent] = []
    cancel_requested = False

    def observe_event(event: RunEvent) -> None:
        event_history.append(event)
        if parsed.output == "jsonl":
            sys.stdout.write(render_jsonl_event(event_history) + "\n")
            sys.stdout.flush()
        elif parsed.output == "human":
            _render_process_event(event)
        status = event.data.get("run_status", event.data.get("status"))
        cancellation_confirmed = (
            cancel_requested
            and status == "CANCELLED"
            and controller.observe("CANCELLED") is CancelAction.WAIT
        )
        if cancellation_confirmed:
            if controller.confirmed:
                raise _CancelConfirmed

    def handle_interrupt(signum: int, frame: object) -> None:
        nonlocal cancel_requested
        del signum, frame
        if controller.interrupt() is CancelAction.EXIT:
            raise KeyboardInterrupt
        cancel_requested = True
        if run_id is None:
            sys.stderr.write("已收到取消请求，但当前命令没有可取消的 Run。\n")
            return
        _request_interrupt_cancel(control, run_id, controller, "human")

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        code, output = run_command(
            arguments,
            control=control,
            on_event=observe_event if "--follow" in arguments else None,
        )
    except _CancelConfirmed:
        return int(ExitCode.LOCAL_CANCEL_CONFIRMED)
    except KeyboardInterrupt:
        return int(ExitCode.LOCAL_CANCEL_CONFIRMED if controller.confirmed else ExitCode.UNKNOWN)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
