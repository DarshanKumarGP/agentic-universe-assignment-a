"""Pipeline vs. independent oracle on the repo's real synthetic generator (several seeds)."""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

from ledger.config import DEFAULT_SCHEMAS_DIR, REPO_ROOT
from ledger.db import connect
from ledger.ingest import discover_drops, ingest_path
from ledger.reports import build_report, write_report
from ledger.schema_def import load_catalogue, load_schemas
from tests.oracle import run_oracle

GEN = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
pytestmark = pytest.mark.skipif(not GEN.exists(), reason="assignment-a-data/tools/generate_data.py not found")


def generate(seed: int, out: Path, scale: str = "small"):
    spec = importlib.util.spec_from_file_location("generate_data", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import sys
    old = sys.argv
    sys.argv = ["generate_data.py", "--seed", str(seed), "--scale", scale, "--out", str(out)]
    try:
        mod.main()
    finally:
        sys.argv = old


def read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))[1:]


@pytest.mark.parametrize("seed", [1, 7, 2026])
def test_pipeline_matches_oracle(seed, tmp_path, vault, capsys):
    data = tmp_path / "data"
    generate(seed, data)
    schemas, catalogue = load_schemas(DEFAULT_SCHEMAS_DIR), load_catalogue(DEFAULT_SCHEMAS_DIR)

    conn = connect(str(tmp_path / "l.db"), vault)
    for drop in sorted(data.iterdir()):           # drop_1 then drop_2, like the real runs
        ingest_path(conn, vault, schemas, drop)
    out = tmp_path / "rep"
    write_report(build_report(conn, schemas, catalogue), out)

    want = run_oracle(discover_drops(data), schemas, catalogue)
    for name in ("lead_report", "batch_summary", "daily_overview", "rejections"):
        assert read_csv(out / f"{name}.csv") == want[name], name

    got = json.loads((out / "data_quality.json").read_text())
    dq = want["dq"]
    for key in ("lead_rows_total", "lead_rows_rejected", "duplicate_lead_rows_merged", "field_warnings",
                "events_total", "malformed_events", "duplicate_events_dropped", "orphan_events",
                "unknown_disposition_events"):
        assert got[key] == dq[key], key
    for reason, n in got["rejections_by_reason"].items():
        assert n == dq["why:" + reason], reason
    assert sum(got["rejections_by_reason"].values()) == got["lead_rows_rejected"]
    assert got["orphan_events"] > 0 and got["unknown_disposition_events"] > 0   # data really is messy


def test_drops_incrementally_equal_everything_at_once(tmp_path, vault):
    data = tmp_path / "data"
    generate(5, data)
    schemas, catalogue = load_schemas(DEFAULT_SCHEMAS_DIR), load_catalogue(DEFAULT_SCHEMAS_DIR)
    a = connect(str(tmp_path / "a.db"), vault)
    for drop in sorted(data.iterdir()):
        ingest_path(a, vault, schemas, drop)
    b = connect(str(tmp_path / "b.db"), vault)
    ingest_path(b, vault, schemas, data)
    assert build_report(a, schemas, catalogue) == build_report(b, schemas, catalogue)
