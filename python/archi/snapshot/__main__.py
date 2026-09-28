"""Command line: ``python -m archi.snapshot {build,verify}``."""
from __future__ import annotations

import argparse
import sys

from archi.snapshot.builder import (
    BuildRefused,
    SnapshotError,
    build,
    load_config,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archi.snapshot")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser(
        "build",
        help="validate, redact and pack each configured group, plus a lock file",
    )
    b.add_argument("--config", required=True, help="YAML: snapshot name and groups")
    b.add_argument("--out", required=True, help="new or empty output directory")
    b.add_argument("--built-by", default=None, help="recorded in the lock file")
    v = sub.add_parser("verify", help="check archives against snapshot.lock.yaml")
    v.add_argument("--lock", required=True)
    v.add_argument("--archives", required=True, help="directory holding the archives")
    args = parser.parse_args(argv)

    try:
        if args.command == "build":
            lock = build(load_config(args.config), args.out, built_by=args.built_by)
            for name, row in lock["groups"].items():
                print(
                    f"{name}: {row['record_count']} records, {row['file_count']} "
                    f"files, collected {row['collected']}, sha256 {row['sha256']}"
                )
            print(f"wrote {args.out}")
            return 0
        result = verify(args.lock, args.archives)
        print("\n".join(result.lines))
        return 0 if result.ok else 1
    except BuildRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
