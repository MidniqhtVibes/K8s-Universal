import argparse
from pathlib import Path

import yaml

from .generator import render_cluster
from .schemas import ClusterConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster Builder developer utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render", help="Render IaC files from a public cluster.yaml")
    render.add_argument("--config", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--source", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.command == "render":
        config = ClusterConfig.model_validate(yaml.safe_load(args.config.read_text(encoding="utf-8")))
        render_cluster(config, args.output, args.source)
        print(f"Konfiguration nach {args.output} gerendert")


if __name__ == "__main__":
    main()

