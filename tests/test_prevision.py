from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from pannes.config import EQUIPMENT_TYPES
from pannes.prevision import (
    HORIZON_DAYS,
    MODELS,
    backtest,
    calibration,
    current_risk,
    load_history,
    machine_parts,
    month_starts,
    precision_at_k,
    rate_proba,
    snapshots,
    write_risk,
)

START = datetime(2024, 1, 1)


@pytest.fixture(scope="module")
def hist(loaded):
    *_, eng = loaded
    return load_history(eng)


def test_machine_parts_follow_the_bill_of_materials(hist):
    _, machines = hist
    pairs = machine_parts(machines)
    expected = sum(len(EQUIPMENT_TYPES[t][1]) for t in machines["type"])
    assert len(pairs) == expected and not pairs.duplicated(["equipement", "piece"]).any()


def test_features_never_look_past_the_cutoff(hist):
    pannes, machines = hist
    c = datetime(2024, 9, 1)
    full = snapshots(pannes, machines, [c], START)
    past_only = snapshots(pannes[pannes["date_panne"] < c], machines, [c], START,
                          labelled=False)
    cols = ["jours_depuis_derniere", "jamais_vue", "n_piece_365", "n_machine_90",
            "n_machine_365", "taux_piece_flotte", "n_piece_total"]
    pd.testing.assert_frame_equal(full[cols], past_only[cols])


def test_label_is_a_failure_of_that_part_within_30_days(hist):
    pannes, machines = hist
    c = datetime(2024, 10, 1)
    s = snapshots(pannes, machines, [c], START)
    win = pannes[(pannes["date_panne"] >= c)
                 & (pannes["date_panne"] < c + timedelta(days=HORIZON_DAYS))]
    want = set(zip(win["equipement"], win["piece"], strict=True))
    got = set(map(tuple, s.loc[s["panne_30j"] == 1, ["equipement", "piece"]].to_numpy()))
    assert got == want


def test_days_since_last_failure(hist):
    pannes, machines = hist
    c = datetime(2024, 12, 1)
    s = snapshots(pannes, machines, [c], START).set_index(["equipement", "piece"])
    seen = s[s["jamais_vue"] == 0]
    e, part = seen.index[0]
    last = pannes[(pannes.equipement == e) & (pannes.piece == part)
                  & (pannes.date_panne < c)]["date_panne"].max()
    assert seen.loc[(e, part), "jours_depuis_derniere"] == (c - last).days
    assert (s.loc[s["jamais_vue"] == 1, "jours_depuis_derniere"] == (c - START).days).all()


def test_helpers():
    assert month_starts(datetime(2024, 11, 15), datetime(2025, 2, 1)) == [
        datetime(2024, 12, 1), datetime(2025, 1, 1), datetime(2025, 2, 1)]
    assert rate_proba(np.array([0.0]))[0] == 0
    assert 0.07 < rate_proba(np.array([1.0]))[0] < 0.08          # 1/yr -> ~7.9 % in 30 days
    d = pd.DataFrame({"cutoff": [1, 1, 1, 2, 2], "panne_30j": [1, 0, 0, 0, 1]})
    r = precision_at_k(d, np.array([0.9, 0.1, 0.2, 0.3, 0.8]), k=1)
    assert r["precision"] == 1.0 and r["recall"] == 1.0
    cal = calibration(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9]), bins=2)
    assert cal[0]["observed"] == 0 and cal[-1]["observed"] == 1


@pytest.fixture(scope="module")
def bt(loaded):
    *_, eng = loaded
    return backtest(eng, n_boot=50)


def test_backtest_split_has_no_overlap(bt):
    s = bt.metrics["setup"]
    last_train = datetime.fromisoformat(s["train_cutoffs"][-1])
    first_test = datetime.fromisoformat(s["test_cutoffs"][0])
    assert last_train + timedelta(days=HORIZON_DAYS) <= first_test


def test_backtest_metrics_are_complete(bt):
    for m in MODELS:
        r = bt.metrics["models"][m]
        assert 0.5 <= r["auc"] <= 1 and 0 <= r["brier"] <= 0.25
        lo, hi = r["auc_ci95"]
        assert lo <= hi
        assert r["top20_per_month"]["inspections"] == 20 * len(bt.metrics["setup"]
                                                                ["test_cutoffs"])


def test_learned_models_rank_better_than_the_fleet_rate(bt):
    # holds on this data-generating process (wear-out + frail machines)
    base = bt.metrics["models"]["taux_flotte"]["auc"]
    assert bt.metrics["models"]["gradient_boosting"]["auc"] > base
    assert bt.metrics["models"]["regression_logistique"]["auc"] > base


def test_current_risk_scores_every_machine_part(loaded):
    *_, eng = loaded
    risk = current_risk(eng)
    assert risk["proba_30j"].between(0, 1).all()
    assert risk["proba_30j"].is_monotonic_decreasing
    assert write_risk(eng, risk) == len(risk)
