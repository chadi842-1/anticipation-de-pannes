from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from pannes.config import LATE_DECLARATION_HOURS
from pannes.kpi import create_views, kpi_summary, plot_kpis, sqlserver_views, view_sql

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def kp(loaded):
    paths, run, _, eng = loaded
    create_views(eng)
    with eng.connect() as c:
        raw = pd.read_sql(text("""select p.*, pc.nom as piece, e.code as equipement
                                  from pannes p join pieces pc using (piece_id)
                                  join equipements e using (equipement_id)"""), c,
                          parse_dates=["date_panne", "date_declaration", "date_fin"])
    return eng, raw


def test_detail_view_delays_match_pandas(kp):
    eng, raw = kp
    with eng.connect() as c:
        d = pd.read_sql(text("select panne_id, delai_declaration_h, duree_reparation_h, "
                             "declaration_tardive from v_pannes_detail"), c)
    m = d.merge(raw, on="panne_id")
    delay = (m["date_declaration"] - m["date_panne"]).dt.total_seconds() / 3600
    rep = (m["date_fin"] - m["date_panne"]).dt.total_seconds() / 3600
    assert (m["delai_declaration_h"] - delay).abs().max() < 1e-6
    assert (m["duree_reparation_h"] - rep).abs().max() < 1e-6
    assert (m["declaration_tardive"] == (delay > LATE_DECLARATION_HOURS).astype(int)).all()


def test_part_kpis_match_pandas(kp):
    eng, raw = kp
    with eng.connect() as c:
        v = pd.read_sql(text("select * from v_kpi_piece"), c).set_index("piece")
    counts = raw.groupby("piece").size()
    assert v["nb_pannes"].to_dict() == counts.to_dict()
    span_months = (raw["date_panne"].max() - raw["date_panne"].min()).total_seconds() / 3600 / \
        24 / 30.4375
    assert (v["pannes_par_mois"] - counts / span_months).abs().max() < 1e-9
    mttr = raw.assign(h=(raw["date_fin"] - raw["date_panne"]).dt.total_seconds() / 3600) \
        .groupby("piece")["h"].mean()
    assert (v["mttr_h"] - mttr).abs().max() < 1e-6


def test_frequent_parts_are_the_ones_with_high_true_rates(kp):
    eng, _ = kp
    top3 = [p["piece"] for p in kpi_summary(eng)["par_piece"][:3]]
    # Roulement fails often AND is fitted on three machine types: it must lead
    assert top3[0] == "Roulement"


def test_equipment_availability_is_a_share(kp):
    eng, _ = kp
    with eng.connect() as c:
        v = pd.read_sql(text("select * from v_kpi_equipement"), c)
    assert v["disponibilite"].between(0, 1).all() and (v["mtbf_h"] > 0).all()


def test_summary_and_figure(kp, tmp_path):
    eng, raw = kp
    s = kpi_summary(eng)
    assert s["nb_pannes"] == len(raw)
    assert 0 < s["part_declarations_tardives"] < 0.5
    plot_kpis(eng, tmp_path / "k.png")
    assert (tmp_path / "k.png").stat().st_size > 10_000


def test_views_differ_only_by_date_functions():
    lite, ms = view_sql("sqlite"), view_sql("mssql")
    assert "julianday" in lite["v_pannes_detail"] and "DATEDIFF" in ms["v_pannes_detail"]
    assert set(lite) == set(ms)


def test_committed_sqlserver_views_are_up_to_date():
    assert (ROOT / "sql" / "sqlserver" / "views.sql").read_text() == sqlserver_views()
