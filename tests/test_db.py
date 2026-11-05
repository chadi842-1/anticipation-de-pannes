import shutil
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from pannes.config import get_paths
from pannes.db import (
    equipements,
    get_engine,
    init_db,
    load_incremental,
    pannes,
    pieces,
    run_quality_checks,
    sqlserver_ddl,
)
from pannes.synthetic import generate

ROOT = Path(__file__).resolve().parents[1]


def test_loaded_breakdowns_are_exactly_the_true_ones_with_a_valid_row(loaded):
    paths, run, report, eng = loaded
    defective = run.rows[run.rows["defect"].notna() & (run.rows["defect"] != "duplicate")]
    expected = len(run.pannes) - defective["panne_id"].nunique()
    with eng.connect() as c:
        n = c.execute(text("select count(*) from pannes")).scalar_one()
        rej = c.execute(text("select count(*) from etl_rejets")).scalar_one()
    assert n == expected == report["rows_loaded"]
    assert n + rej == len(run.rows)                       # every Excel row accounted for


def test_second_run_skips_unchanged_files(loaded):
    paths, *_ = loaded
    again = load_incremental(paths)
    assert again["files_loaded"] == 0 and again["files_skipped"] == 18 and again["rows_loaded"] == 0


def test_incremental_arrival_and_changed_file(tmp_path):
    paths = get_paths(tmp_path).ensure()
    generate(paths, seed=2)
    hold = tmp_path / "hold"
    hold.mkdir()
    for f in paths.inbox.glob("pannes_*2025*.xlsx"):
        shutil.move(f, hold / f.name)
    first = load_incremental(paths)
    assert first["files_loaded"] == 12
    for f in hold.iterdir():
        shutil.move(f, paths.inbox / f.name)
    second = load_incremental(paths)
    assert second["files_loaded"] == 6 and second["files_skipped"] == 12

    # a site sends a corrected version of one file: only that file is reloaded
    f = paths.inbox / "pannes_casablanca_2024T1.xlsx"
    wb = openpyxl.load_workbook(f)
    ws = wb.active
    last = ws.max_row
    ws.delete_rows(last)
    wb.save(f)
    eng = get_engine(paths.database_url())
    with eng.connect() as c:
        before = c.execute(text("select count(*) from pannes where source_file = :f"),
                           {"f": f.name}).scalar_one()
    third = load_incremental(paths)
    assert third["files_loaded"] == 1
    with eng.connect() as c:
        after = c.execute(text("select count(*) from pannes where source_file = :f"),
                          {"f": f.name}).scalar_one()
        n_files = c.execute(text("select count(*) from etl_fichiers")).scalar_one()
    assert after <= before and n_files == 18


@pytest.fixture
def eng(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path / 't.db'}")
    init_db(e)
    with e.begin() as c:
        c.execute(text("insert into sites (nom) values ('S')"))
        c.execute(equipements.insert().values(code="PRS-001", designation="Presse", site_id=1))
    return e


def _panne(**kw):
    base = {"equipement_id": 1, "piece_id": 1, "date_panne": datetime(2025, 1, 1, 8),
            "source": "app", "charge_le": datetime(2025, 1, 1, 9)}
    return {**base, **kw}


@pytest.mark.parametrize("bad", [
    {"gravite": "catastrophique"},
    {"date_declaration": datetime(2024, 12, 31)},
    {"date_fin": datetime(2024, 12, 31)},
    {"equipement_id": 999},
    {"source": "email"},
])
def test_constraints_reject_bad_rows(eng, bad):
    with pytest.raises(IntegrityError), eng.begin() as c:
        c.execute(pannes.insert().values(**_panne(**bad)))


def test_unique_breakdown_key(eng):
    with eng.begin() as c:
        c.execute(pannes.insert().values(**_panne()))
    with pytest.raises(IntegrityError), eng.begin() as c:
        c.execute(pannes.insert().values(**_panne(gravite="mineure")))


def test_parts_catalogue_is_seeded(eng):
    with eng.connect() as c:
        assert len(c.execute(select(pieces)).all()) == 15


def test_quality_checks_detect_an_overlap(eng):
    with eng.begin() as c:
        c.execute(pannes.insert().values(**_panne(date_fin=datetime(2025, 1, 1, 12))))
        c.execute(pannes.insert().values(**_panne(piece_id=2, date_panne=datetime(2025, 1, 1, 10),
                                                  date_declaration=datetime(2025, 1, 3, 10))))
    q = run_quality_checks(eng)
    assert q["chevauchements"] == 1 and q["declarations_tardives"] == 1
    assert q["reparations_sans_fin"] == 1


def test_committed_sqlserver_ddl_is_up_to_date():
    committed = (ROOT / "sql" / "sqlserver" / "schema.sql").read_text()
    assert committed == sqlserver_ddl()
    for needle in ("IDENTITY", "NVARCHAR", "CONSTRAINT uq_panne UNIQUE",
                   "CONSTRAINT ck_declaration_apres_panne CHECK", "FOREIGN KEY"):
        assert needle in committed, needle
