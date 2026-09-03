"""Command-line entrypoint for the autonomous semantic-montage runner."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .project import load_project
from .runner import PipelineRunner, StageRegistry


def build_production_registry() -> StageRegistry:
    """Build the production registry.

    Keeping this function explicit makes a missing stage a startup error instead
    of a silent skip and keeps imports lazy for ``status``/help startup.
    """
    from .production_stages import build_production_stage_registry

    return build_production_stage_registry()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semantic-montage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run or resume a portable project")
    run.add_argument("project")
    run.add_argument("--run-id")
    run.add_argument("--stage", action="append", dest="stages")
    run.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="runtime-only named asset mount; never written to project/state",
    )
    status = subparsers.add_parser("status", help="show durable run status")
    status.add_argument("project")
    status.add_argument("run_id")
    inspect = subparsers.add_parser("stages", help="list the production stage DAG")
    inspect.add_argument("project")
    return parser


def _parse_mounts(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        name, separator, path = str(raw).partition("=")
        name, path = name.strip(), path.strip()
        if not separator or not name or not path or name in result:
            raise SystemExit(f"invalid or duplicate --mount {raw!r}; expected NAME=PATH")
        result[name] = Path(path).expanduser().resolve()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = load_project(args.project)
    registry = build_production_registry()
    runner = PipelineRunner(
        project,
        registry,
        runtime_mounts=_parse_mounts(getattr(args, "mount", [])),
    )
    if args.command == "status":
        payload = {
            "run": runner.state.run(args.run_id),
            "stages": runner.state.stages(args.run_id),
            "artifacts": runner.state.artifacts(args.run_id),
        }
    elif args.command == "stages":
        payload = {
            "project_id": project.project_id,
            "requested": list(project.stages),
            "ordered": [
                {
                    "name": row.name,
                    "dependencies": list(row.dependencies),
                    "version": row.version,
                    "paid": row.paid,
                }
                for row in registry.ordered(project.stages)
            ],
        }
    else:
        payload = runner.run(run_id=args.run_id, requested=args.stages)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "build_production_registry", "main"]
