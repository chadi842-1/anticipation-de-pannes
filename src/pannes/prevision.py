"""Predict which parts of which machines will fail in the next 30 days.

Everything is computed from the CLEANED database (what the pipeline actually loaded), never from
the generator's truth: rejected rows are missing history, exactly as they would be in real life.

Snapshot design: on the 1st of each month (the cutoff), for every machine and every part that
machine type contains, features use only breakdowns BEFORE the cutoff, and the label is whether
that part fails on that machine in the next 30 days.

Features:
    jours_depuis_derniere   days since this part last failed on this machine (or since the start
                            of the history if never seen, with a flag): the wear-out signal
    n_piece_365, n_machine_90, n_machine_365   recent breakdown counts (machine-part / machine)
    taux_piece_flotte       failures of this part per machine-year over the whole fleet so far
    age_machine, piece, type, site

Protocol: train on 2024 cutoffs whose 30-day label window ends before 2025 (no leakage), test on
the 2025 cutoffs, once. Candidates:
    taux_flotte     P = 1 - exp(-fleet rate of the part x 30 days)     (what a planner would do)
    taux_machine    P from this machine-part's own past rate (shrunk to the fleet rate)
    regression_logistique, gradient_boosting   on the features above
Metrics: AUC, PR-AUC, Brier score, calibration, and the operational one: if the team inspects the
K riskiest machine-parts each month, how many of the next month's failures are among them.
Uncertainty: bootstrap over machines (failures of one machine are not independent).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sqlalchemy import Engine, text

from .config import EQUIPMENT_TYPES, SEED

HORIZON_DAYS = 30
NUM = ["jours_depuis_derniere", "jamais_vue", "n_piece_365", "n_machine_90", "n_machine_365",
       "taux_piece_flotte", "age_machine", "jours_historique"]
CAT = ["piece", "type", "site"]
MODELS = ["taux_flotte", "taux_machine", "regression_logistique", "gradient_boosting"]


# ----------------------------------------------------------------------------- data
def load_history(engine: Engine) -> tuple[pd.DataFrame, pd.DataFrame]:
    with engine.connect() as c:
        pannes = pd.read_sql(text("""
            select e.code as equipement, pc.nom as piece, p.date_panne
            from pannes p join equipements e using (equipement_id)
                          join pieces pc using (piece_id)"""), c, parse_dates=["date_panne"])
        machines = pd.read_sql(text("""
            select e.code as equipement, e.designation as type, s.nom as site, e.mise_en_service
            from equipements e join sites s using (site_id)"""), c,
                               parse_dates=["mise_en_service"])
    return pannes, machines


def machine_parts(machines: pd.DataFrame) -> pd.DataFrame:
    """Every (machine, part) pair the machine type contains (its bill of materials)."""
    rows = [{"equipement": m.equipement, "type": m.type, "site": m.site,
             "mise_en_service": m.mise_en_service, "piece": part}
            for m in machines.itertuples() for part in EQUIPMENT_TYPES[m.type][1]]
    return pd.DataFrame(rows)


def snapshots(pannes: pd.DataFrame, machines: pd.DataFrame, cutoffs: list[datetime],
              history_start: datetime, labelled: bool = True) -> pd.DataFrame:
    pairs = machine_parts(machines)
    out = []
    n_machines_by_part = pairs.groupby("piece")["equipement"].nunique()
    for c in cutoffs:
        past = pannes[pannes["date_panne"] < c]
        hist_days = max((c - history_start).days, 1)
        d = pairs.copy()
        d["cutoff"] = c
        last = past.groupby(["equipement", "piece"])["date_panne"].max()
        d = d.join(last.rename("derniere"), on=["equipement", "piece"])
        d["jamais_vue"] = d["derniere"].isna().astype(int)
        d["jours_depuis_derniere"] = np.where(
            d["derniere"].notna(), (c - d["derniere"]).dt.days, hist_days)
        yr = past[past["date_panne"] >= c - timedelta(days=365)]
        q = past[past["date_panne"] >= c - timedelta(days=90)]
        d = d.join(yr.groupby(["equipement", "piece"]).size().rename("n_piece_365"),
                   on=["equipement", "piece"])
        d = d.join(q.groupby("equipement").size().rename("n_machine_90"), on="equipement")
        d = d.join(yr.groupby("equipement").size().rename("n_machine_365"), on="equipement")
        fleet = past.groupby("piece").size() / n_machines_by_part / (hist_days / 365.25)
        d["taux_piece_flotte"] = d["piece"].map(fleet).fillna(0.0)
        own = past.groupby(["equipement", "piece"]).size()
        d = d.join(own.rename("n_piece_total"), on=["equipement", "piece"])
        d["age_machine"] = (c - d["mise_en_service"]).dt.days / 365.25
        d["jours_historique"] = hist_days
        if labelled:
            fut = pannes[(pannes["date_panne"] >= c)
                         & (pannes["date_panne"] < c + timedelta(days=HORIZON_DAYS))]
            hit = set(zip(fut["equipement"], fut["piece"], strict=True))
            d["panne_30j"] = [int((e, p) in hit) for e, p in zip(d["equipement"], d["piece"],
                                                                  strict=True)]
        out.append(d)
    df = pd.concat(out, ignore_index=True)
    for col in ("n_piece_365", "n_machine_90", "n_machine_365", "n_piece_total"):
        df[col] = df[col].fillna(0).astype(int)
    return df.drop(columns=["derniere"])


def month_starts(first: datetime, last: datetime) -> list[datetime]:
    """First days of months in [first, last] (a month start before `first` is not included)."""
    c = datetime(first.year, first.month, 1)
    if c < first:
        c = datetime(c.year + (c.month == 12), c.month % 12 + 1, 1)
    out = []
    while c <= last:
        out.append(c)
        c = datetime(c.year + (c.month == 12), c.month % 12 + 1, 1)
    return out


# --------------------------------------------------------------------------- models
def rate_proba(rate_per_year: np.ndarray) -> np.ndarray:
    return 1 - np.exp(-np.asarray(rate_per_year, float) * HORIZON_DAYS / 365.25)


def taux_machine(d: pd.DataFrame, prior_years: float = 1.0) -> np.ndarray:
    """Own rate of the machine-part, shrunk towards the fleet rate (gamma-Poisson style)."""
    years = d["jours_historique"] / 365.25
    rate = (d["n_piece_total"] + d["taux_piece_flotte"] * prior_years) / (years + prior_years)
    return rate_proba(rate)


def make_logistic() -> Pipeline:
    pre = ColumnTransformer([
        ("num", StandardScaler(), NUM),
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT),
    ])
    return Pipeline([("pre", pre), ("clf", LogisticRegression(C=0.5, max_iter=3000))])


def make_gbm(seed: int = SEED) -> Pipeline:
    pre = ColumnTransformer([
        ("num", "passthrough", NUM),
        ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CAT),
    ])
    return Pipeline([("pre", pre), ("clf", HistGradientBoostingClassifier(
        max_iter=250, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=40,
        l2_regularization=1.0, random_state=seed))])


def predict_all(train: pd.DataFrame, test: pd.DataFrame, seed: int = SEED) -> dict:
    lr = make_logistic().fit(train[NUM + CAT], train["panne_30j"])
    gb = make_gbm(seed).fit(train[NUM + CAT], train["panne_30j"])
    return {
        "taux_flotte": rate_proba(test["taux_piece_flotte"]),
        "taux_machine": taux_machine(test),
        "regression_logistique": lr.predict_proba(test[NUM + CAT])[:, 1],
        "gradient_boosting": gb.predict_proba(test[NUM + CAT])[:, 1],
        "_models": {"regression_logistique": lr, "gradient_boosting": gb},
    }


# ------------------------------------------------------------------------- metrics
def precision_at_k(test: pd.DataFrame, p: np.ndarray, k: int) -> dict:
    """Each month, inspect the k riskiest machine-parts: share of inspections that hit a failure
    in the next 30 days, and share of that month's failures caught."""
    d = test.assign(p=p)
    hits, fails, n = 0, 0, 0
    for _, g in d.groupby("cutoff"):
        top = g.nlargest(k, "p")
        hits += int(top["panne_30j"].sum())
        fails += int(g["panne_30j"].sum())
        n += len(top)
    return {"precision": round(hits / n, 4), "recall": round(hits / fails, 4) if fails else None,
            "failures_caught": hits, "failures_total": fails, "inspections": n}


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 8) -> list[dict]:
    q = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    idx = np.clip(np.searchsorted(q, p, side="right") - 1, 0, len(q) - 2)
    return [{"predicted": round(float(p[idx == b].mean()), 4),
             "observed": round(float(y[idx == b].mean()), 4), "n": int((idx == b).sum())}
            for b in range(len(q) - 1) if (idx == b).any()]


def bootstrap_by_machine(test: pd.DataFrame, preds: dict, n_boot: int, seed: int) -> dict:
    """95% CIs of AUC per model, and of each model's AUC gain over the fleet-rate baseline,
    resampling MACHINES (their failures are correlated)."""
    rng = np.random.default_rng(seed)
    machines = test["equipement"].unique()
    groups = {m: np.flatnonzero(test["equipement"].to_numpy() == m) for m in machines}
    y = test["panne_30j"].to_numpy()
    aucs = {m: [] for m in MODELS}
    for _ in range(n_boot):
        idx = np.concatenate([groups[m] for m in rng.choice(machines, len(machines))])
        if y[idx].min() == y[idx].max():
            continue
        for m in MODELS:
            aucs[m].append(roc_auc_score(y[idx], preds[m][idx]))
    out = {}
    base = np.array(aucs["taux_flotte"])
    for m in MODELS:
        a = np.array(aucs[m])
        out[m] = {"auc_ci95": [round(float(np.quantile(a, 0.025)), 4),
                               round(float(np.quantile(a, 0.975)), 4)]}
        if m != "taux_flotte":
            gain = a - base
            out[m]["gain_vs_taux_flotte_ci95"] = [round(float(np.quantile(gain, 0.025)), 4),
                                                  round(float(np.quantile(gain, 0.975)), 4)]
    return out


def spare_parts_forecast(test: pd.DataFrame, preds: dict) -> dict:
    """Expected failures per part and month = sum of probabilities; MAE against actual."""
    out = {}
    actual = test.groupby(["cutoff", "piece"])["panne_30j"].sum()
    for m in MODELS:
        pred = test.assign(p=preds[m]).groupby(["cutoff", "piece"])["p"].sum()
        out[m] = {"mae_per_part_month": round(float((pred - actual).abs().mean()), 4),
                  "total_predicted": round(float(pred.sum()), 1),
                  "total_actual": int(actual.sum())}
    return out


@dataclass
class BacktestResult:
    metrics: dict
    test: pd.DataFrame
    preds: dict


def backtest(engine: Engine, history_start: datetime = datetime(2024, 1, 1),
             train_until: datetime = datetime(2025, 1, 1), test_last: datetime | None = None,
             k: int = 20, n_boot: int = 500, seed: int = SEED) -> BacktestResult:
    pannes, machines = load_history(engine)
    data_end = pannes["date_panne"].max()
    test_last = test_last or (data_end - timedelta(days=HORIZON_DAYS))
    # train cutoffs: at least 2 months of history, label window ends before train_until
    train_cut = [c for c in month_starts(history_start + timedelta(days=60), train_until)
                 if c + timedelta(days=HORIZON_DAYS) <= train_until]
    test_cut = [c for c in month_starts(train_until, test_last)]
    train = snapshots(pannes, machines, train_cut, history_start)
    test = snapshots(pannes, machines, test_cut, history_start)
    preds = predict_all(train, test, seed)
    y = test["panne_30j"].to_numpy()
    res = {"setup": {"history_start": str(history_start.date()),
                     "train_cutoffs": [str(c.date()) for c in train_cut],
                     "test_cutoffs": [str(c.date()) for c in test_cut],
                     "train_rows": int(len(train)), "test_rows": int(len(test)),
                     "train_positive_rate": round(float(train["panne_30j"].mean()), 4),
                     "test_positive_rate": round(float(y.mean()), 4),
                     "horizon_days": HORIZON_DAYS, "k_inspections_per_month": k,
                     "machine_parts": int(test.groupby(["equipement", "piece"]).ngroups)},
           "models": {}}
    boot = bootstrap_by_machine(test, preds, n_boot, seed)
    spares = spare_parts_forecast(test, preds)
    for m in MODELS:
        p = preds[m]
        res["models"][m] = {
            "auc": round(float(roc_auc_score(y, p)), 4),
            "pr_auc": round(float(average_precision_score(y, p)), 4),
            "brier": round(float(brier_score_loss(y, p)), 5),
            **boot[m],
            f"top{k}_per_month": precision_at_k(test, p, k),
            "spare_parts": spares[m],
            "calibration": calibration(y, p),
        }
    gb = preds["_models"]["gradient_boosting"]
    res["feature_effects"] = _permutation_effects(gb, test, seed)
    return BacktestResult(res, test, preds)


def _permutation_effects(model, test: pd.DataFrame, seed: int) -> dict:
    from sklearn.inspection import permutation_importance

    r = permutation_importance(model, test[NUM + CAT], test["panne_30j"], scoring="roc_auc",
                               n_repeats=10, random_state=seed)
    imp = pd.Series(r.importances_mean, index=NUM + CAT).sort_values(ascending=False)
    return {k: round(float(v), 4) for k, v in imp.items()}


# -------------------------------------------------------------------- current risk scores
def current_risk(engine: Engine, history_start: datetime = datetime(2024, 1, 1),
                 seed: int = SEED) -> pd.DataFrame:
    """Train on every fully-labelled cutoff, score the machine-parts as of the end of the data."""
    pannes, machines = load_history(engine)
    data_end = pannes["date_panne"].max()
    asof = datetime(data_end.year, data_end.month, data_end.day) + timedelta(days=1)
    cut = [c for c in month_starts(history_start + timedelta(days=60), asof)
           if c + timedelta(days=HORIZON_DAYS) <= asof]
    train = snapshots(pannes, machines, cut, history_start)
    now = snapshots(pannes, machines, [asof], history_start, labelled=False)
    model = make_gbm(seed).fit(train[NUM + CAT], train["panne_30j"])
    now["proba_30j"] = model.predict_proba(now[NUM + CAT])[:, 1]
    now["proba_taux_flotte"] = rate_proba(now["taux_piece_flotte"])
    return now[["equipement", "type", "site", "piece", "jours_depuis_derniere", "jamais_vue",
                "n_piece_365", "proba_30j", "proba_taux_flotte"]].assign(
        calcule_le=asof).sort_values("proba_30j", ascending=False).reset_index(drop=True)


def write_risk(engine: Engine, risk: pd.DataFrame) -> int:
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS previsions_risque"))
    risk.to_sql("previsions_risque", engine, index=False)
    return len(risk)


def plot_backtest(res: BacktestResult, out_path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
    SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    colors = dict(zip(MODELS, [INK_2, AQUA, BLUE, ORANGE], strict=True))
    labels = {"taux_flotte": "Fleet rate per part", "taux_machine": "Machine-part own rate",
              "regression_logistique": "Logistic regression",
              "gradient_boosting": "Gradient boosting"}
    del YELLOW
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "xtick.color": INK_2,
        "ytick.color": INK_2, "text.color": INK, "axes.spines.top": False,
        "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "font.size": 10,
        "axes.axisbelow": True})
    m = res.metrics
    k = m["setup"]["k_inspections_per_month"]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))
    ax = axes[0]
    for i, name in enumerate(reversed(MODELS)):
        r = m["models"][name]
        lo, hi = r["auc_ci95"]
        ax.plot([lo, hi], [i, i], color=colors[name], lw=2.5, solid_capstyle="round")
        ax.plot(r["auc"], i, "o", color=colors[name], ms=9, mec=SURFACE, mew=2)
        ax.text(hi + 0.005, i, f"{r['auc']:.3f}", va="center", fontsize=9, color=INK)
    ax.set_yticks(range(len(MODELS)))
    ax.set_yticklabels([labels[n] for n in reversed(MODELS)])
    ax.set_xlabel("AUC on 2025 (95 % CI, bootstrap over machines)")
    ax.axvline(0.5, color=GRID, lw=1)
    ax.grid(axis="y", visible=False)
    tc = m["setup"]["test_cutoffs"]
    _title(ax, "Who will fail in the next 30 days?",
           f"trained on 2024; tested once on cutoffs {tc[0][:7]} to {tc[-1][:7]}", INK, INK_2)
    ax = axes[1]
    for name in MODELS:
        cal = m["models"][name]["calibration"]
        ax.plot([c["predicted"] for c in cal], [c["observed"] for c in cal], "-o",
                color=colors[name], lw=1.8, ms=4, label=labels[name])
    lim = max(max(c["observed"] for n in MODELS for c in m["models"][n]["calibration"]),
              max(c["predicted"] for n in MODELS for c in m["models"][n]["calibration"])) * 1.05
    ax.plot([0, lim], [0, lim], color=INK_2, lw=1, ls=(0, (4, 3)))
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.set_xlabel("Predicted probability")
    ax.set_ylabel("Observed failure rate")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    _title(ax, "Calibration", f"dashed = perfect; top-{k} list uses these scores", INK, INK_2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def _title(ax, main, sub, ink, ink2):
    ax.set_title(main, loc="left", fontsize=12, fontweight="bold", color=ink, pad=22)
    ax.text(0, 1.03, sub, transform=ax.transAxes, fontsize=9, color=ink2, va="bottom")


def public(res: BacktestResult) -> str:
    return json.dumps(res.metrics, indent=2, ensure_ascii=False)
