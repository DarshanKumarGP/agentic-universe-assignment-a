"""Command line: `python -m ledger ingest <path>...` and `python -m ledger report --out <dir>`."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .config import DEFAULT_DB_PATH, DEFAULT_SCHEMAS_DIR
from .crypto import load_vault
from .db import FileChangedError, WrongKeyError, connect
from .ingest import ingest_path
from .reports import build_report, write_report
from .schema_def import load_catalogue, load_schemas


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--db", default=os.environ.get("LEDGER_DB", DEFAULT_DB_PATH),
                   help="SQLite database file (default: ./ledger.db or $LEDGER_DB)")
    p.add_argument("--schemas", default=os.environ.get("LEDGER_SCHEMAS", str(DEFAULT_SCHEMAS_DIR)),
                   help="folder with workflow schemas (*.json) and dispositions.csv")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the final summary")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", parents=[common], help="ingest a drop (or a folder of drops)")
    ing.add_argument("paths", nargs="+", type=Path, help="drop folder(s), e.g. data/drop_1, or data/")
    rep = sub.add_parser("report", parents=[common], help="write the five report files")
    rep.add_argument("--out", required=True, type=Path, help="output directory")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(message)s", stream=sys.stderr)
    started = time.perf_counter()
    try:
        schemas = load_schemas(Path(args.schemas))
        catalogue = load_catalogue(Path(args.schemas))
        vault = load_vault(args.db)
        conn = connect(args.db, vault)
        if args.command == "ingest":
            for path in args.paths:
                s = ingest_path(conn, vault, schemas, path)
                print(f"{path}: {s.files_ingested} file(s) ingested, {s.files_skipped} already ingested | "
                      f"leads +{s.new_leads} (merged {s.merged}, rejected {s.rejected}) | "
                      f"events +{s.events_stored} (duplicate {s.events_duplicate}, malformed {s.events_malformed})")
                for note in s.notes:
                    print(f"  note: {note}", file=sys.stderr)
        else:
            written = write_report(build_report(conn, schemas, catalogue), args.out)
            print("wrote " + ", ".join(p.name for p in written) + f" to {args.out}")
        print(f"done in {time.perf_counter() - started:.1f}s")
        return 0
    except (FileNotFoundError, FileChangedError, WrongKeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
