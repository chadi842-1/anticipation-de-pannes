from datetime import datetime

import pytest
from sqlalchemy import select, text

from pannes.app import create_app
from pannes.db import equipements, get_engine, init_db, pannes
from pannes.declarations import close, declare

NOW = datetime(2025, 7, 10, 14, 0)


@pytest.fixture
def eng(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path / 'app.db'}")
    init_db(e)
    with e.begin() as c:
        c.execute(text("insert into sites (nom) values ('Casablanca')"))
        c.execute(equipements.insert(), [
            {"code": "PRS-001", "designation": "Presse hydraulique", "site_id": 1},
            {"code": "CMP-001", "designation": "Compresseur", "site_id": 1}])
    return e


def form(**kw):
    base = {"equipement": "PRS-001", "piece": "Joint", "date_panne": "2025-07-10T09:00",
            "gravite": "majeure", "technicien": "S. Tazi"}
    return {**base, **kw}


def clock():
    return NOW


# ---- business rules
def test_declaration_uses_the_server_clock(eng):
    r = declare(eng, form(), clock)
    assert r.ok and not r.late
    with eng.connect() as c:
        row = c.execute(select(pannes).where(pannes.c.panne_id == r.panne_id)).one()
    assert row.date_declaration == NOW and row.source == "app"


def test_late_declaration_needs_a_reason(eng):
    r = declare(eng, form(date_panne="2025-07-08T09:00"), clock)
    assert not r.ok and "motif" in r.errors[0]
    r = declare(eng, form(date_panne="2025-07-08T09:00", motif_retard="Week-end, site fermé"),
                clock)
    assert r.ok and r.late


@pytest.mark.parametrize("bad,needle", [
    ({"equipement": "XXX-999"}, "Équipement inconnu"),
    ({"piece": "Turbo"}, "Pièce inconnue"),
    ({"date_panne": "demain"}, "illisible"),
    ({"date_panne": "2025-07-11T09:00"}, "futur"),
    ({"gravite": "apocalyptique"}, "Gravité"),
    ({"date_fin": "2025-07-10T08:00"}, "antérieure"),
])
def test_invalid_input_is_refused_with_a_message(eng, bad, needle):
    r = declare(eng, form(**bad), clock)
    assert not r.ok and any(needle in e for e in r.errors)


def test_open_breakdown_blocks_an_overlapping_one(eng):
    assert declare(eng, form(date_panne="2025-07-10T09:00"), clock).ok        # still open
    r = declare(eng, form(piece="Vérin hydraulique", date_panne="2025-07-10T11:00"), clock)
    assert not r.ok and "Chevauchement" in r.errors[0]
    other = declare(eng, form(equipement="CMP-001", piece="Filtre",
                              date_panne="2025-07-10T11:00"), clock)
    assert other.ok                                                           # other machine


def test_closed_interval_blocks_breakdowns_inside_it_only(eng):
    assert declare(eng, form(date_panne="2025-07-10T08:00", date_fin="2025-07-10T10:00"),
                   clock).ok
    assert not declare(eng, form(piece="Pompe hydraulique", date_panne="2025-07-10T09:00"),
                       clock).ok
    assert declare(eng, form(piece="Pompe hydraulique", date_panne="2025-07-10T10:30"),
                   clock).ok


def test_duplicate_is_refused(eng):
    assert declare(eng, form(date_fin="2025-07-10T10:00"), clock).ok
    r = declare(eng, form(date_fin="2025-07-10T10:00"), clock)
    assert not r.ok


def test_closing_a_breakdown(eng):
    pid = declare(eng, form(date_panne="2025-07-10T08:00"), clock).panne_id
    assert not close(eng, pid, "2025-07-10T07:00", clock).ok                  # before start
    assert not close(eng, pid, "2025-07-11T07:00", clock).ok                  # future
    assert close(eng, pid, "2025-07-10T12:00", clock).ok
    assert not close(eng, 9999, "2025-07-10T12:00", clock).ok


def test_closing_cannot_create_an_overlap(eng):
    first = declare(eng, form(date_panne="2025-07-10T08:00"), clock).panne_id
    assert close(eng, first, "2025-07-10T09:00", clock).ok
    second = declare(eng, form(piece="Vérin hydraulique", date_panne="2025-07-10T10:00",
                               date_fin="2025-07-10T11:00"), clock)
    assert second.ok
    # re-closing the first one later would swallow the second one
    assert not close(eng, first, "2025-07-10T10:30", clock).ok


# ---- Flask layer
@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{tmp_path / 'web.db'}"
    app = create_app(url, now=clock)
    e = get_engine(url)
    with e.begin() as c:
        c.execute(text("insert into sites (nom) values ('Casablanca')"))
        c.execute(equipements.insert().values(code="PRS-001", designation="Presse hydraulique",
                                              site_id=1))
    app.config["TESTING"] = True
    return app.test_client()


def test_form_page_lists_equipment_and_parts(client):
    html = client.get("/").get_data(as_text=True)
    assert "PRS-001" in html and "Roulement" in html and "Motif du retard" in html


def test_form_post_success_and_error(client):
    ok = client.post("/declarer", data=form(), follow_redirects=True)
    assert "enregistrée" in ok.get_data(as_text=True)
    bad = client.post("/declarer", data=form(date_panne="2025-07-01T09:00"))
    assert bad.status_code == 422 and "motif" in bad.get_data(as_text=True)


def test_json_api(client):
    r = client.post("/api/pannes", json=form())
    assert r.status_code == 201 and r.get_json()["panne_id"]
    assert client.post("/api/pannes", json=form(equipement="NOPE")).status_code == 422
    rows = client.get("/api/pannes").get_json()
    assert rows[0]["code"] == "PRS-001" and rows[0]["source"] == "app"
    assert client.get("/health").get_json() == {"status": "ok"}


def test_close_through_the_web(client):
    pid = client.post("/api/pannes", json=form()).get_json()["panne_id"]
    page = client.post(f"/pannes/{pid}/cloturer", data={"date_fin": "2025-07-10T12:00"},
                       follow_redirects=True).get_data(as_text=True)
    assert "clôturée" in page


def test_risk_page_empty_then_filled(tmp_path):
    import pandas as pd

    url = f"sqlite:///{tmp_path / 'risk.db'}"
    app = create_app(url, now=clock)
    c = app.test_client()
    assert "pannes predict" in c.get("/risques").get_data(as_text=True)
    risk = pd.DataFrame({"equipement": ["PRS-001", "CMP-002"], "type": ["Presse", "Compresseur"],
                         "site": ["Casablanca", "Mohammedia"], "piece": ["Joint", "Filtre"],
                         "jours_depuis_derniere": [200, 10], "jamais_vue": [1, 0],
                         "n_piece_365": [0, 2], "proba_30j": [0.42, 0.11],
                         "proba_taux_flotte": [0.2, 0.25], "calcule_le": [NOW, NOW]})
    risk.to_sql("previsions_risque", get_engine(url), index=False)
    html = c.get("/risques").get_data(as_text=True)
    assert html.index("PRS-001") < html.index("CMP-002") and "42 %" in html
    only = c.get("/risques?site=Mohammedia").get_data(as_text=True)
    assert "CMP-002" in only and "PRS-001" not in only
    assert "PRS-001" in c.get("/risques?site=hack' or 1=1").get_data(as_text=True)  # ignored
