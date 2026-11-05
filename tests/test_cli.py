import json
from pathlib import Path

from pannes.cli import cmd_ddl, main

ROOT = Path(__file__).resolve().parents[1]


def test_run_end_to_end(tmp_path):
    main(["--root", str(tmp_path), "run", "--per-type-site", "2", "--seed", "4"])
    rep = tmp_path / "reports"
    for f in ("pipeline.json", "kpi.json", "kpi.png", "results.md"):
        assert (rep / f).exists(), f
    r = json.loads((rep / "pipeline.json").read_text())
    assert r["load_2024"]["files_loaded"] == 12
    assert r["reload_unchanged"] == {"files_loaded": 0, "files_skipped": 12, "rows_loaded": 0}
    assert r["load_2025"]["files_loaded"] == 6 and r["load_2025"]["files_skipped"] == 12
    assert r["cleaning_vs_truth"]["clean_rows_wrongly_rejected"] == 0
    md = (rep / "results.md").read_text(encoding="utf-8")
    assert "## Incremental load" in md and "## Reliability KPIs" in md


def test_ddl_command_writes_what_is_committed(tmp_path):
    files = cmd_ddl(tmp_path)
    for f in files:
        name = Path(f).name
        assert Path(f).read_text() == (ROOT / "sql" / "sqlserver" / name).read_text()


def test_subcommands_have_help(capsys):
    import pytest

    for sub in ("generate", "load", "kpi", "ddl", "serve", "run"):
        with pytest.raises(SystemExit) as e:
            main([sub, "--help"])
        assert e.value.code == 0


def test_python_m_pannes_entrypoint():
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "pannes", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "usage: pannes" in r.stdout
