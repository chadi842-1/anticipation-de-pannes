"""Command line.

    pannes generate [--per-type-site N]   synthetic Excel files into inbox/
    pannes load                           incremental load of inbox/ (new or changed files only)
    pannes kpi                            create the KPI views, write reports/kpi.json + figure
    pannes predict                        backtest the 30-day risk models, score current risk
    pannes ddl                            write sql/sqlserver/{schema,views}.sql
    pannes serve [--port P]               Flask declaration app
    pannes run [--per-type-site N]        demo: 2024 files, then 2025 files arrive, then reports
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .config import SEED, Paths, get_paths

ROOT = Path(__file__).resolve().parents[2]


def cmd_generate(paths: Paths, per_type_site: int, seed: int) -> dict:
    from .synthetic import generate

    r = generate(paths, seed=seed, per_type_site=per_type_site)
    return {"equipements": len(r.equipements), "true_breakdowns": len(r.pannes),
            "excel_rows": len(r.rows), "files": len(r.files)}


def cmd_load(paths: Paths) -> dict:
    from .db import load_incremental

    return load_incremental(paths)


def cmd_kpi(paths: Paths) -> dict:
    from .db import get_engine
    from .kpi import create_views, kpi_summary, plot_kpis

    eng = get_engine(paths.database_url())
    create_views(eng)
    summary = kpi_summary(eng)
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "kpi.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    plot_kpis(eng, paths.reports / "kpi.png")
    return summary


def cmd_predict(paths: Paths, n_boot: int = 500) -> dict:
    from .db import get_engine
    from .prevision import backtest, current_risk, plot_backtest, write_risk

    eng = get_engine(paths.database_url())
    bt = backtest(eng, n_boot=n_boot)
    plot_backtest(bt, paths.reports / "prevision.png")
    risk = current_risk(eng)
    write_risk(eng, risk)
    out = {**bt.metrics, "current_risk_top10": risk.head(10).assign(
        calcule_le=risk["calcule_le"].astype(str)).to_dict("records")}
    (paths.reports / "prevision.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return out


def cmd_ddl(out_dir: Path = ROOT / "sql" / "sqlserver") -> list[str]:
    from .db import sqlserver_ddl
    from .kpi import sqlserver_views

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "schema.sql").write_text(sqlserver_ddl())
    (out_dir / "views.sql").write_text(sqlserver_views())
    return [str(out_dir / "schema.sql"), str(out_dir / "views.sql")]


def cmd_run(paths: Paths, per_type_site: int, seed: int) -> None:
    """Demo of the incremental behaviour: 2024 files first, then the 2025 files arrive."""
    import pandas as pd

    from .clean import clean_files, read_extraction_date, read_register, score_against_truth

    for d in (paths.inbox, paths.data):
        shutil.rmtree(d, ignore_errors=True)
    paths.ensure()
    print("[1/6] generate", flush=True)
    gen = cmd_generate(paths, per_type_site, seed)
    hold = paths.data / "_arrivals"
    hold.mkdir(parents=True, exist_ok=True)
    later = sorted(paths.inbox.glob("pannes_*2025*.xlsx"))
    for f in later:
        shutil.move(f, hold / f.name)
    print("[2/6] load 2024 files", flush=True)
    first = cmd_load(paths)
    print("[3/6] reload with nothing new, then 2025 files arrive", flush=True)
    noop = cmd_load(paths)
    for f in later:
        shutil.move(hold / f.name, paths.inbox / f.name)
    second = cmd_load(paths)
    print("[4/6] KPIs", flush=True)
    kpi = cmd_kpi(paths)
    print("[5/6] predictive maintenance backtest", flush=True)
    prev = cmd_predict(paths)
    print("[6/6] report", flush=True)
    # score the cleaning rules against the generator's labels (synthetic data only)
    files = sorted(paths.inbox.glob("pannes_*.xlsx"))
    res = clean_files(files, read_register(paths.inbox / "referentiel_equipements.xlsx"),
                      read_extraction_date(paths.inbox))
    score = score_against_truth(res, pd.read_parquet(paths.truth / "rows.parquet"))
    report = {"settings": {"per_type_site": per_type_site, "seed": seed}, "generated": gen,
              "load_2024": first, "reload_unchanged": {k: noop[k] for k in (
                  "files_loaded", "files_skipped", "rows_loaded")},
              "load_2025": second, "cleaning_vs_truth": score, "cleaning_stats": res.stats}
    (paths.reports / "pipeline.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str))
    from .report import render_results

    (paths.reports / "results.md").write_text(render_results(report, kpi, prev),
                                              encoding="utf-8")
    print(f"done: {paths.reports / 'results.md'}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="pannes", description=__doc__.splitlines()[0])
    ap.add_argument("--root", help="working directory (default $PANNES_HOME or cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("generate", "run"):
        p = sub.add_parser(name)
        p.add_argument("--per-type-site", type=int, default=8,
                       help="machines per equipment type and site (5 types x 3 sites)")
        p.add_argument("--seed", type=int, default=SEED)
    for name in ("load", "kpi", "ddl", "predict"):
        sub.add_parser(name)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=5000)
    a = ap.parse_args(argv)
    paths = get_paths(a.root).ensure()
    if a.cmd == "run":
        cmd_run(paths, a.per_type_site, a.seed)
        return
    if a.cmd == "serve":
        from .app import create_app

        create_app(paths.database_url()).run(port=a.port)
        return
    out = {"generate": lambda: cmd_generate(paths, a.per_type_site, a.seed),
           "load": lambda: cmd_load(paths), "kpi": lambda: cmd_kpi(paths),
           "predict": lambda: {k: v for k, v in cmd_predict(paths).items()
                               if k in ("setup", "current_risk_top10")},
           "ddl": lambda: cmd_ddl()}[a.cmd]()
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
