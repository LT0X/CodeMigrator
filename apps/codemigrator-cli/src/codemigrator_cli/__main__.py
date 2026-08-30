from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence

from .cancel import CancelController
from .client import EventSource, MockEventSource
from .exit_codes import ExitCode
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
    cancel.add_argument("--if-match", type=int, required=True)
    cancel.add_argument("--output", choices=("human", "json", "jsonl"), default="human")
    return parser


def _add_output_flags(parser: argparse.ArgumentParser) -> None:
    follow = parser.add_mutually_exclusive_group()
    follow.add_argument("--follow", action="store_true")
    follow.add_argument("--no-follow", action="store_true")
    parser.add_argument("--output", choices=("human", "json", "jsonl"), default="human")


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


def _render_status(run_id: str, status: str, output: str, *, version: int = 14) -> str:
    payload = {"run_id": run_id, "status": status, "version": version}
    if output == "json":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if output == "jsonl":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return f"Run {run_id} · {status} · version {version}\n"


def run_command(argv: Sequence[str], *, source: EventSource | None = None) -> tuple[int, str]:
    args = _parser().parse_args(list(argv))
    if args.command == "run" and args.run_command == "show":
        return int(ExitCode.COMPLETED), _render_status(args.run_id, "COMPLETED", args.output)
    if args.command == "run" and args.run_command == "cancel":
        controller = CancelController()
        action = controller.interrupt()
        del action
        controller.observe("CANCELLED")
        return int(ExitCode.CANCELLED), _render_status(
            args.run_id, "CANCELLED", args.output, version=args.if_match + 1
        )
    events: Iterable[RunEvent] = (source or MockEventSource()).events()
    if args.no_follow:
        return int(ExitCode.COMPLETED), _render_start(args.output)
    if args.output == "jsonl":
        return int(ExitCode.COMPLETED), "\n".join(render_jsonl(events)) + "\n"
    projection = project_events(events)
    if args.output == "json":
        return int(ExitCode.COMPLETED), render_json(projection) + "\n"
    return int(ExitCode.COMPLETED), render_human(projection)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if not sys.stdout.isatty() and "--follow" not in arguments and "--no-follow" not in arguments:
        arguments.append("--no-follow")
    code, output = run_command(arguments)
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
