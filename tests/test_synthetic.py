from datetime import datetime

import openpyxl
import pandas as pd
import pytest

from pannes.config import EQUIPMENT_TYPES, get_paths
from pannes.synthetic import END, START, generate


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    paths = get_paths(tmp_path_factory.mktemp("synth")).ensure()
    rates = {"duplicate": 0.06, "date_incoherente": 0.04, "date_future": 0.02,
             "date_illisible": 0.03, "equipement_inconnu": 0.03, "champ_manquant": 0.03}
    return paths, generate(paths, seed=3, defect_rates=rates)


def test_register_and_quarterly_files(run):
    paths, r = run
    assert len(r.files) == 18 and len(r.equipements) == 60
    assert (paths.inbox / "referentiel_equipements.xlsx").exists()
    assert set(r.equipements["code"].str[:3]) == {p for p, _ in EQUIPMENT_TYPES.values()}


def test_true_breakdowns_are_consistent(run):
    _, r = run
    p = r.pannes
    assert p["date_panne"].between(START, END).all()
    assert (p["date_fin"] > p["date_panne"]).all()
    assert (p["date_declaration"] >= p["date_panne"]).all()
    for _, g in p.sort_values("date_panne").groupby("equipement"):
        assert (g["date_panne"].iloc[1:].to_numpy() >= g["date_fin"].iloc[:-1].to_numpy()).all()
    for e in p.itertuples():
        typ = r.equipements.set_index("code").loc[e.equipement, "type"]
        assert e.piece in EQUIPMENT_TYPES[typ][1]


def test_every_written_row_is_labelled_and_points_to_its_file(run):
    paths, r = run
    assert set(r.rows["defect"].dropna()) == {"duplicate", "date_incoherente", "date_future",
                                             "date_illisible", "equipement_inconnu",
                                             "champ_manquant"}
    for f, g in r.rows.groupby("file"):
        ws = openpyxl.load_workbook(paths.inbox / f).active
        assert ws.max_row == g["row"].max()


def test_each_true_breakdown_has_at_most_one_non_duplicate_row(run):
    _, r = run
    firsts = r.rows[r.rows["defect"] != "duplicate"]
    assert not firsts["panne_id"].duplicated().any()
    dups = r.rows[r.rows["defect"] == "duplicate"]
    assert dups["panne_id"].isin(firsts["panne_id"]).all()


def test_three_templates_and_date_encodings(run):
    paths, r = run
    casa = openpyxl.load_workbook(paths.inbox / r.files[0]).active
    assert casa.cell(1, 1).value == "Code équipement"
    assert isinstance(casa.cell(2, 3).value, datetime)
    jorf = openpyxl.load_workbook(paths.inbox / "pannes_jorf_lasfar_2024T1.xlsx").active
    assert jorf.cell(1, 1).value.startswith("Suivi des pannes")
    assert jorf.cell(3, 1).value == "equip"
    moha = openpyxl.load_workbook(paths.inbox / "pannes_mohammedia_2024T1.xlsx").active
    assert moha.cell(1, 1).value == "EQUIPEMENT"
    assert isinstance(moha.cell(2, 3).value, str) and moha.cell(2, 3).value[2] == "/"


def test_reproducible(tmp_path):
    a = generate(get_paths(tmp_path / "a"), seed=9)
    b = generate(get_paths(tmp_path / "b"), seed=9)
    pd.testing.assert_frame_equal(a.rows, b.rows)
