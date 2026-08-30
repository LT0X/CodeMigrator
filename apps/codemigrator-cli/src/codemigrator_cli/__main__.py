from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Sequence

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
from .projector import project_events
from .renderer import render_human, render_json, render_jsonl


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codemigrator", description="CodeMigrator 迁移观察客户端")
    subparsers = parser.add_subparsers(dest="command", required=True)
    migrate = subparsers.add_parser("migrate", help="迁移操作")
    migrate_sub = migrate.add_subparsers(dest="migrate_command", required=True)
    start = migrate_sub.add_parser("start", help="创建并观察一个 mock Run")
    start.add_argument("spec")
    _add_output_flags(start)
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


def _render_start(output: str) -> str:
    if output == "json":
        return json.dumps(
            {
                "run_id": "mock-run-001",
                "status": "CREATED",
                "web_url": "/runs/mock-run-001",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    if output == "jsonl":
        return '{"run_id":"mock-run-001","status":"CREATED"}\n'
    return "已创建 Run mock-run-001\nWeb: /runs/mock-run-001\n"


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
        return (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
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
    argv: Sequence[str], *, source: EventSource | None = None, control: RunControl | None = None
) -> tuple[int, str]:
    args = _parser().parse_args(list(argv))
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
            try:
                latest = run_control.show(args.run_id)
                latest_version = latest.get("version")
                if type(latest_version) is not int:
                    raise ValueError("latest Run version is invalid")
                cancel_payload = run_control.cancel(args.run_id, latest_version)
            except Exception:
                cancel_payload = {"run_id": args.run_id, "status": "UNKNOWN"}
        except Exception:
            cancel_payload = {"run_id": args.run_id, "status": "UNKNOWN"}
        return _status_exit(cancel_payload.get("status")), _render_payload(
            cancel_payload, args.output
        )
    if args.no_follow and args.command == "migrate":
        return int(ExitCode.COMPLETED), _render_start(args.output)
    try:
        events: Iterable[RunEvent] = (source or _configured_event_source(args)).events()
        if args.output == "jsonl":
            event_list = list(events)
            projection = project_events(event_list)
            return _status_exit(projection.run_status), "\n".join(render_jsonl(event_list)) + "\n"
        projection = project_events(events)
        if args.output == "json":
            return _status_exit(projection.run_status), render_json(projection) + "\n"
        return _status_exit(projection.run_status), render_human(projection)
    except Exception:
        error_payload: dict[str, object] = {"status": "UNKNOWN"}
        if args.command == "run":
            error_payload["run_id"] = args.run_id
        return int(ExitCode.UNKNOWN), _render_payload(error_payload, args.output)


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
    code, output = run_command(arguments)
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
