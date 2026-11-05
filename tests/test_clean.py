from datetime import datetime

import numpy as np
import openpyxl
import pytest

from pannes.clean import (
    REASONS,
    clean_files,
    find_header,
    norm_code,
    norm_gravite,
    norm_part,
    parse_date,
    read_extraction_date,
    read_file,
    read_register,
    score_against_truth,
)
from pannes.config import get_paths
from pannes.synthetic import EXTRACTION_DATE, generate


@pytest.mark.parametrize("raw,expected", [
    ("PRS-012", "PRS-012"), ("prs 012", "PRS-012"), ("PRS012", "PRS-012"), (" cmp-003 ", "CMP-003"),
    ("XXX-999", "XXX-999"), ("", None), (None, None), (np.nan, None),
])
def test_norm_code(raw, expected):
    assert norm_code(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Roulement", "Roulement"), ("ROULEMENTS", "Roulement"), ("Verin hydraulique",
                                                              "Vérin hydraulique"),
    ("capteur de pressions", "Capteur de pression"), ("Câble ", "Câble"), (None, None),
])
def test_norm_part(raw, expected):
    assert norm_part(raw) == expected


def test_unknown_part_is_marked_not_dropped():
    assert norm_part("Turbo-encabulateur").startswith("?")


@pytest.mark.parametrize("raw,expected", [
    ("Maj", "majeure"), ("MAJEURE", "majeure"), ("mineure", "mineure"), ("Cri", "critique"),
    ("", None), (None, None),
])
def test_norm_gravite(raw, expected):
    assert norm_gravite(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (datetime(2024, 3, 9, 17, 42, 30), datetime(2024, 3, 9, 17, 42)),
    ("12/03/2024 17:48", datetime(2024, 3, 12, 17, 48)),
    ("2025-03-12 14:30", datetime(2025, 3, 12, 14, 30)),
    (45362.176389, datetime(2024, 3, 11, 4, 14)),
    ("12 mars 2025 14h30", datetime(2025, 3, 12, 14, 30)),
    ("3 février 2024 8h05", datetime(2024, 2, 3, 8, 5)),
    ("31/02/2025 08:00", None), ("N/A", None), ("à vérifier", None), (12.5, None), (None, None),
])
def test_parse_date_every_encoding(raw, expected):
    assert parse_date(raw) == expected


def test_find_header_skips_title_rows():
    rows = [("Suivi des pannes - X",), (None,),
            ("equip", "pièce changée", "survenue le", "déclarée le")]
    idx, mapping = find_header(rows)
    assert idx == 2 and set(mapping.values()) == {"equipement", "piece", "date_panne",
                                                  "date_declaration"}
    with pytest.raises(ValueError):
        find_header([("a", "b"), ("c", "d")])


def test_read_file_keeps_excel_row_numbers(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Titre"])
    ws.append([])
    ws.append(["EQUIPEMENT", "PIECE", "DATE_PANNE"])
    ws.append(["PRS-001", "Joint", "01/02/2024 10:00"])
    ws.append([None, None, None])                          # blank line skipped
    ws.append(["PRS-002", "Joint", "02/02/2024 10:00"])
    f = tmp_path / "x.xlsx"
    wb.save(f)
    df = read_file(f)
    assert df["source_row"].tolist() == [4, 6]
    assert df["date_declaration"].isna().all()             # missing column -> nulls


@pytest.fixture(scope="module")
def cleaned(tmp_path_factory):
    paths = get_paths(tmp_path_factory.mktemp("clean")).ensure()
    rates = {"duplicate": 0.06, "date_incoherente": 0.04, "date_future": 0.02,
             "date_illisible": 0.03, "equipement_inconnu": 0.03, "champ_manquant": 0.03}
    run = generate(paths, seed=11, defect_rates=rates)
    res = clean_files(sorted(paths.inbox.glob("pannes_*.xlsx")),
                      read_register(paths.inbox / "referentiel_equipements.xlsx"),
                      read_extraction_date(paths.inbox))
    return run, res


def test_every_row_is_accounted_for(cleaned):
    run, res = cleaned
    assert len(res.clean) + len(res.rejects) == len(run.rows)
    assert set(res.rejects["reason"]) <= set(REASONS)


def test_cleaning_matches_the_ground_truth(cleaned):
    run, res = cleaned
    s = score_against_truth(res, run.rows)
    assert s["clean_rows_wrongly_rejected"] == 0 and s["defective_rows_kept"] == 0
    for kind, v in s["per_defect"].items():
        assert v["caught_with_right_reason"] == v["injected"], kind


def test_clean_rows_are_normalised_and_unique(cleaned):
    _, res = cleaned
    c = res.clean
    assert c["equipement"].str.fullmatch(r"[A-Z]{3}-\d{3}").all()
    assert not c.duplicated(subset=["equipement", "piece", "date_panne"]).any()
    assert (c["date_panne"] <= EXTRACTION_DATE).all()
    assert c["gravite"].isin(["mineure", "majeure", "critique"]).all()


def test_extraction_date_comes_from_the_manifest(cleaned, tmp_path):
    assert read_extraction_date(tmp_path).date() == datetime.now().date()   # no manifest
