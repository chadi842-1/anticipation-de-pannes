"""SYNTHETIC breakdown declarations, written as the kind of Excel files sites actually send.

The real data of the original internship is confidential and is not used. This generator
reproduces its PROBLEMS, so the cleaning and loading code has the same work to do:

- one file per site and quarter, three different templates (column names, header row position,
  date encodings: real Excel dates, 'dd/mm/yyyy HH:MM' strings, ISO strings, Excel serial
  numbers, French text such as '12 mars 2025 14h30');
- inconsistent spellings of equipment codes ('prs 012', 'PRS012'), parts ('ROULEMENTS',
  'Verin hydraulique') and severities ('Maj', 'MAJEURE');
- injected defects, each recorded in data/_truth/rows.parquet:
    duplicate            the same breakdown entered again (same file or the next quarter's file)
    date_incoherente     declared before it happened, or repaired before it happened
    date_future          dated after the extraction date
    date_illisible       '31/02/2025 08:00', 'N/A', 'à vérifier'
    equipement_inconnu   code missing from the equipment register (typo or unknown asset)
    champ_manquant       part or breakdown date left empty

The true breakdown process (v2, fixed before any prediction model was written):
- each part of each machine is a renewal process: the time between two failures follows a
  Weibull law with a part-specific shape (> 1 = wear-out: bearings, belts, filters, cables;
  about 1 = random: sensors, motors) and a mean set by the part's yearly rate;
- each machine has a rate multiplier: frailty (gamma, mean 1) times an age effect
  (+4 % per year of service beyond 8 years);
- breakdowns of one machine never overlap; declaration delays are log-normal with a share of
  late (> 24 h) declarations, which the Flask app is meant to prevent.
So failures ARE partly predictable from a part's time since its last replacement, the machine's
history and its age; v1 of this generator used constant Poisson rates, where nothing was.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from openpyxl import Workbook

from .config import EQUIPMENT_TYPES, GRAVITES, SEED, SITES, Paths

START, END = datetime(2024, 1, 1), datetime(2025, 7, 1)
EXTRACTION_DATE = datetime(2025, 7, 5)

# yearly failure rate per part (synthetic; drives the "frequency by part" KPI)
PART_RATE = {
    "Vérin hydraulique": 1.2, "Joint": 3.5, "Pompe hydraulique": 0.8, "Capteur de pression": 1.5,
    "Filtre": 4.0, "Courroie": 2.5, "Roulement": 2.0, "Clapet": 1.0, "Rouleau": 1.8,
    "Moteur": 0.6, "Garniture mécanique": 2.2, "Turbine": 0.4, "Câble": 0.9, "Frein": 1.4,
    "Moteur de levage": 0.5,
}
# Weibull shape of the time between failures: > 1 means wear-out (risk rises with the part's
# age since its last replacement), about 1 means random failures (electronics, sensors).
PART_SHAPE = {
    "Vérin hydraulique": 1.5, "Joint": 1.8, "Pompe hydraulique": 1.4, "Capteur de pression": 1.0,
    "Filtre": 3.0, "Courroie": 2.2, "Roulement": 2.5, "Clapet": 1.3, "Rouleau": 1.8,
    "Moteur": 1.2, "Garniture mécanique": 2.0, "Turbine": 1.2, "Câble": 2.5, "Frein": 2.0,
    "Moteur de levage": 1.3,
}
DEFECT_RATES = {"duplicate": 0.04, "date_incoherente": 0.02, "date_future": 0.005,
                "date_illisible": 0.01, "equipement_inconnu": 0.01, "champ_manquant": 0.01}

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
        "octobre", "novembre", "décembre"]
TECHS = ["A. Benali", "Y. El Idrissi", "K. Ouahbi", "S. Tazi", "M. Alaoui", "H. Bennani"]

# site -> template; Mohammedia switches template in 2025 (a real-life annoyance)
TEMPLATE_A = ["Code équipement", "Pièce", "Date panne", "Date déclaration", "Fin réparation",
              "Gravité", "Technicien", "Commentaire"]
TEMPLATE_B = ["EQUIPEMENT", "PIECE", "DATE_PANNE", "DATE_DECL", "DATE_FIN", "GRAVITE", "TECH"]
TEMPLATE_C = ["equip", "pièce changée", "survenue le", "déclarée le", "réparée le", "niveau",
              "intervenant", "remarques"]


@dataclass
class SyntheticRun:
    equipements: pd.DataFrame
    pannes: pd.DataFrame          # the true, clean breakdowns
    rows: pd.DataFrame            # every Excel row written, with its truth label
    files: list[str]


# ------------------------------------------------------------------------ reference data
def make_equipements(rng: np.random.Generator, per_type_site: int = 4) -> pd.DataFrame:
    rows = []
    for type_, (prefix, _) in EQUIPMENT_TYPES.items():
        n = 0
        for site in SITES:
            for _ in range(per_type_site):
                n += 1
                rows.append({
                    "code": f"{prefix}-{n:03d}",
                    "type": type_,
                    "site": site,
                    "mise_en_service": date(int(rng.integers(2008, 2023)),
                                            int(rng.integers(1, 13)), 1),
                })
    return pd.DataFrame(rows)


def machine_factors(eq: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Per-machine multiplier on failure rates: frailty (some units are simply worse) times an
    age effect (+4 % per year of service beyond 8 years, -4 % per year below)."""
    age = np.array([(START.date() - d).days / 365.25 for d in eq["mise_en_service"]])
    frailty = rng.gamma(shape=4.0, scale=0.25, size=len(eq))          # mean 1, sd 0.5
    return pd.DataFrame({"code": eq["code"].to_numpy(), "age_ans": age.round(2),
                         "frailty": frailty.round(3),
                         "facteur": (frailty * np.exp(0.04 * (age - 8))).round(3)})


def part_failure_times(rate_per_year: float, shape: float, rng: np.random.Generator,
                       burn_in_days: int = 3 * 365) -> list[float]:
    """Renewal process: Weibull time between failures of one part on one machine (the part is
    replaced at each failure). Simulated from a burn-in before START so that, at START, parts
    are at realistic, varied ages. Returns failure times in days since START."""
    from math import gamma

    mean_days = 365.0 / rate_per_year
    scale = mean_days / gamma(1 + 1 / shape)
    t, out = -float(burn_in_days), []
    horizon = (END - START).days
    while True:
        t += float(scale * rng.weibull(shape))
        if t >= horizon:
            return out
        if t >= 0:
            out.append(t)


def make_pannes(eq: pd.DataFrame, rng: np.random.Generator,
                machines: pd.DataFrame | None = None) -> pd.DataFrame:
    machines = machine_factors(eq, rng) if machines is None else machines
    factors = machines.set_index("code")["facteur"]
    out = []
    for e in eq.itertuples():
        events = []
        for part in EQUIPMENT_TYPES[e.type][1]:
            rate = PART_RATE[part] * float(factors[e.code])
            for t in part_failure_times(rate, PART_SHAPE[part], rng):
                events.append((START + timedelta(days=t), part))
        events.sort()
        free_at = START
        for t, part in events:
            t = max(t, free_at + timedelta(hours=1))            # one breakdown at a time
            t = t.replace(second=0, microsecond=0)
            if t >= END:
                continue
            grav = rng.choice(GRAVITES, p=[0.55, 0.35, 0.10])
            rep_h = float(rng.lognormal(np.log({"mineure": 2, "majeure": 6,
                                                "critique": 18}[grav]), 0.5))
            late = rng.random() < 0.12
            decl_h = float(rng.uniform(26, 240) if late else rng.lognormal(np.log(2.5), 0.8))
            fin = t + timedelta(hours=round(rep_h, 1))
            out.append({"equipement": e.code, "site": e.site, "piece": part,
                        "date_panne": t,
                        "date_declaration": (t + timedelta(hours=min(decl_h, 23.5)
                                             if not late else decl_h)).replace(second=0,
                                                                                 microsecond=0),
                        "date_fin": fin.replace(second=0, microsecond=0),
                        "gravite": grav, "technicien": TECHS[int(rng.integers(len(TECHS)))]})
            free_at = fin
    df = pd.DataFrame(out).sort_values(["date_panne", "equipement"]).reset_index(drop=True)
    df.insert(0, "panne_id", [f"T{i:05d}" for i in range(len(df))])
    return df


# ------------------------------------------------------------------- value variants
def _code_variant(code: str, rng) -> str:
    prefix, num = code.split("-")
    return [code, code.lower(), f"{prefix} {num}", f"{prefix}{num}", f" {code} ",
            f"{prefix.lower()} {num}"][int(rng.integers(6))]


def _part_variant(part: str, rng) -> str:
    import unicodedata

    plain = "".join(c for c in unicodedata.normalize("NFKD", part) if not unicodedata.combining(c))
    return [part, part.upper(), part.lower(), f"{part} ", plain, f"{part}s"][int(rng.integers(6))]


def _grav_variant(g: str, rng) -> str:
    return [g, g.upper(), g.capitalize(), g[:3].capitalize()][int(rng.integers(4))]


def _date_cell(dt: datetime, template: str, rng):
    """Encode a datetime the way the given template's users typically do."""
    if template == "A":
        return dt                                               # real Excel date
    if template == "B":
        return dt.strftime("%d/%m/%Y %H:%M")
    k = int(rng.integers(3))                                    # template C: anything goes
    if k == 0:
        return dt.strftime("%Y-%m-%d %H:%M")
    if k == 1:
        return round((dt - datetime(1899, 12, 30)).total_seconds() / 86400, 6)
    return f"{dt.day} {MOIS[dt.month - 1]} {dt.year} {dt.hour}h{dt.minute:02d}"


# ------------------------------------------------------------------------ writing
def _template_for(site: str, quarter_start: datetime) -> str:
    if site == "Casablanca":
        return "A"
    if site == "Jorf Lasfar":
        return "C"
    return "B" if quarter_start.year == 2024 else "C"


def _quarters() -> list[datetime]:
    q, out = START, []
    while q < END:
        out.append(q)
        q = datetime(q.year + (q.month + 3 > 12), (q.month + 2) % 12 + 1, 1)
    return out


def _corrupt(rec: dict, kind: str, rng, eq_codes: set[str]) -> dict:
    r = dict(rec)
    if kind == "date_incoherente":
        if rng.random() < 0.5:
            r["date_declaration"] = r["date_panne"] - timedelta(hours=float(rng.uniform(2, 72)))
        else:
            r["date_fin"] = r["date_panne"] - timedelta(hours=float(rng.uniform(1, 48)))
    elif kind == "date_future":
        # truly after the extraction date (typically a year typo: 2026 instead of 2024)
        target = EXTRACTION_DATE + timedelta(days=int(rng.integers(10, 300)),
                                             minutes=int(rng.integers(0, 1440)))
        shift = target - r["date_panne"]
        r["date_panne"] += shift
        r["date_declaration"] += shift
        r["date_fin"] += shift
    elif kind == "date_illisible":
        r["date_panne"] = ["31/02/2025 08:00", "N/A", "à vérifier", "??/03/2025"][
            int(rng.integers(4))]
    elif kind == "equipement_inconnu":
        prefix, num = r["equipement"].split("-")
        r["equipement"] = (f"{prefix}-{num}0" if rng.random() < 0.5 else "XXX-999")
        while r["equipement"] in eq_codes:
            r["equipement"] = "XXX-999"
    elif kind == "champ_manquant":
        r["piece" if rng.random() < 0.5 else "date_panne"] = None
    return r


def generate(paths: Paths, seed: int = SEED, per_type_site: int = 4,
             defect_rates: dict[str, float] | None = None) -> SyntheticRun:
    paths.ensure()
    rates = DEFECT_RATES if defect_rates is None else defect_rates
    rng = np.random.default_rng(seed)
    eq = make_equipements(rng, per_type_site)
    machines = machine_factors(eq, rng)
    pannes = make_pannes(eq, rng, machines)
    eq_codes = set(eq["code"])
    _write_register(paths, eq, rng)

    rows, files = [], []
    carry: dict[str, list[dict]] = {}          # cross-file duplicates for the next quarter
    quarters = _quarters()
    for qi, q0 in enumerate(quarters):
        q1 = quarters[qi + 1] if qi + 1 < len(quarters) else END
        for site in SITES:
            tpl = _template_for(site, q0)
            sel = pannes[(pannes.site == site) & (pannes.date_panne >= q0)
                         & (pannes.date_panne < q1)]
            entries: list[tuple[dict, str | None]] = [(r, None) for r in carry.pop(site, [])]
            for rec in sel.to_dict("records"):
                kind = _pick(rates, rng)
                if kind in (None, "duplicate"):
                    entries.append((rec, None))
                    if kind == "duplicate":
                        if rng.random() < 0.3 and qi + 1 < len(quarters):
                            carry.setdefault(site, []).append(rec)
                        else:
                            entries.append((rec, "duplicate"))
                else:
                    entries.append((_corrupt(rec, kind, rng, eq_codes), kind))
            order = rng.permutation(len(entries))
            entries = [entries[i] for i in order]
            # a cross-file duplicate is labelled 'duplicate' where it appears second
            fixed = []
            for rec, kind in entries:
                if kind is None and rec["date_panne"] is not None and \
                        isinstance(rec["date_panne"], datetime) and rec["date_panne"] < q0:
                    kind = "duplicate"
                fixed.append((rec, kind))
            name = f"pannes_{site.lower().replace(' ', '_')}_{q0:%Y}T{(q0.month - 1) // 3 + 1}.xlsx"
            rows += _write_file(paths, name, tpl, site, q0, fixed, rng)
            files.append(name)

    rows_df = pd.DataFrame(rows)
    # an export manifest, as a real data drop would carry: the date the files were extracted
    (paths.inbox / "manifest.json").write_text(
        json.dumps({"extraction_date": EXTRACTION_DATE.date().isoformat(), "files": files},
                   indent=2))
    pannes.to_parquet(paths.truth / "pannes.parquet", index=False)
    machines.to_parquet(paths.truth / "machines.parquet", index=False)
    rows_df.to_parquet(paths.truth / "rows.parquet", index=False)
    return SyntheticRun(eq, pannes, rows_df, files)


def _pick(rates: dict[str, float], rng) -> str | None:
    u, cum = rng.random(), 0.0
    for k, r in rates.items():
        cum += r
        if u < cum:
            return k
    return None


def _write_register(paths: Paths, eq: pd.DataFrame, rng) -> None:
    """The equipment register (master data) as the maintenance department keeps it."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Equipements"
    ws.append(["Code", "Désignation", "Site", "Mise en service"])
    for e in eq.itertuples():
        mes = datetime.combine(e.mise_en_service, datetime.min.time())
        ws.append([e.code, e.type, e.site, mes])
    wb.save(paths.inbox / "referentiel_equipements.xlsx")


def _write_file(paths: Paths, name: str, tpl: str, site: str, q0: datetime,
                entries: list[tuple[dict, str | None]], rng) -> list[dict]:
    wb = Workbook()
    ws = wb.active
    ws.title = "Pannes"
    header = {"A": TEMPLATE_A, "B": TEMPLATE_B, "C": TEMPLATE_C}[tpl]
    if tpl == "C":
        ws.append([f"Suivi des pannes - {site} - T{(q0.month - 1) // 3 + 1} {q0.year}"])
        ws.append([])
    ws.append(header)
    out = []
    for rec, kind in entries:
        def d(v):
            return _date_cell(v, tpl, rng) if isinstance(v, datetime) else v

        values = [
            _code_variant(rec["equipement"], rng) if "-" in rec["equipement"]
            else rec["equipement"],
            _part_variant(rec["piece"], rng) if rec["piece"] else None,
            d(rec["date_panne"]), d(rec["date_declaration"]), d(rec["date_fin"]),
            _grav_variant(rec["gravite"], rng), rec["technicien"],
        ]
        if tpl in ("A", "C"):
            values.append("" if rng.random() < 0.7 else "RAS")
        ws.append(values)
        out.append({"file": name, "row": ws.max_row, "panne_id": rec["panne_id"],
                    "defect": kind})
    wb.save(paths.inbox / name)
    return out
