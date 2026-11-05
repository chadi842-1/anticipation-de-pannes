"""Cleaning: read heterogeneous Excel files into one normalised table, with explicit rejections.

Steps, per file:
1. find the header row (it is not always row 1) and map the columns through an alias table;
2. parse dates whatever their encoding (Excel date, serial number, dd/mm/yyyy, ISO, French text);
3. normalise equipment codes ('prs 012' -> 'PRS-012'), parts and severities;
4. reject each row for the FIRST rule it breaks, in this order:
     champ_manquant, date_illisible, equipement_inconnu, piece_inconnue, date_future,
     date_incoherente
5. across all files, a breakdown already seen (same equipment, part and start time) is a
   duplicate; the first occurrence (by file name, then row) is kept.

Nothing is silently dropped: every Excel row ends up either in `clean` or in `rejects` with a
reason and its source file and row.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import openpyxl
import pandas as pd

from .config import GRAVITES, PARTS

COLUMN_ALIASES: dict[str, set[str]] = {
    "equipement": {"code_equipement", "equipement", "equip", "code"},
    "piece": {"piece", "piece_changee"},
    "date_panne": {"date_panne", "survenue_le"},
    "date_declaration": {"date_declaration", "date_decl", "declaree_le"},
    "date_fin": {"fin_reparation", "date_fin", "reparee_le"},
    "gravite": {"gravite", "niveau"},
    "technicien": {"technicien", "tech", "intervenant"},
    "commentaire": {"commentaire", "remarques"},
}
REQUIRED = ["equipement", "piece", "date_panne"]
REASONS = ["champ_manquant", "date_illisible", "equipement_inconnu", "piece_inconnue",
           "date_future", "date_incoherente", "duplicate"]
MOIS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
        "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}
STR_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y-%m-%d")


# ------------------------------------------------------------------------- normalisers
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_header(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", strip_accents(str(s or "")).lower()).strip("_")


def norm_code(v) -> str | None:
    if is_blank(v):
        return None
    s = re.sub(r"\s+", "", str(v)).upper()
    m = re.fullmatch(r"([A-Z]{3})-?(\d{3,4})", s)
    return f"{m.group(1)}-{m.group(2)}" if m else s


_PART_KEYS = {strip_accents(p).lower(): p for p in PARTS}


def norm_part(v) -> str | None:
    if is_blank(v):
        return None
    k = re.sub(r"\s+", " ", strip_accents(str(v)).lower()).strip()
    if k in _PART_KEYS:
        return _PART_KEYS[k]
    if k.endswith("s") and k[:-1] in _PART_KEYS:          # plural
        return _PART_KEYS[k[:-1]]
    k2 = re.sub(r"\b(\w+)s\b", r"\1", k)                  # plural inside: 'capteur de pressions'
    return _PART_KEYS.get(k2, "?" + str(v).strip())


def norm_gravite(v) -> str | None:
    if is_blank(v):
        return None
    k = strip_accents(str(v)).lower().strip()
    for g in GRAVITES:
        if k and g.startswith(k[:3]):
            return g
    return None


def parse_date(v) -> datetime | None:
    """Excel date, Excel serial number, common strings or French text -> datetime (or None)."""
    if is_blank(v):
        return None
    if isinstance(v, datetime):
        return v.replace(second=0, microsecond=0)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 < v < 80000:                             # Excel serial (1954-2119)
            dt = datetime(1899, 12, 30) + timedelta(days=float(v))
            return (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)
        return None
    s = str(v).strip()
    for fmt in STR_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.fullmatch(r"(\d{1,2}) ([a-zéû]+) (\d{4})(?: (\d{1,2})h(\d{2}))?",
                     s.lower())
    if m and strip_accents(m.group(2)) in MOIS:
        try:
            return datetime(int(m.group(3)), MOIS[strip_accents(m.group(2))], int(m.group(1)),
                            int(m.group(4) or 0), int(m.group(5) or 0))
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------------- reading
def find_header(rows: list[tuple], max_scan: int = 10) -> tuple[int, dict[int, str]]:
    """(0-based header row index, {column index: canonical name}) of the best-matching row."""
    best = (-1, {})
    for i, row in enumerate(rows[:max_scan]):
        mapping = {}
        for j, cell in enumerate(row):
            h = norm_header(cell)
            for canon, aliases in COLUMN_ALIASES.items():
                if h in aliases and canon not in mapping.values():
                    mapping[j] = canon
        if len(mapping) > len(best[1]):
            best = (i, mapping)
    if not set(REQUIRED) <= set(best[1].values()):
        raise ValueError(f"no header with {REQUIRED} found in the first {max_scan} rows")
    return best


def read_file(path: Path) -> pd.DataFrame:
    """One Excel file -> raw rows with canonical column names and source references."""
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    h, mapping = find_header(rows)
    out = []
    for i, row in enumerate(rows[h + 1:], start=h + 2):     # Excel rows are 1-based
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue
        rec = {canon: (row[j] if j < len(row) else None) for j, canon in mapping.items()}
        rec.update({"source_file": path.name, "source_row": i})
        out.append(rec)
    df = pd.DataFrame(out)
    for c in COLUMN_ALIASES:
        if c not in df.columns:
            df[c] = None
    return df


def read_register(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    df.columns = [norm_header(c) for c in df.columns]
    return pd.DataFrame({"code": df["code"].map(norm_code), "type": df["designation"],
                         "site": df["site"], "mise_en_service": pd.to_datetime(
                             df["mise_en_service"]).dt.date})


def read_extraction_date(inbox: Path) -> datetime:
    f = inbox / "manifest.json"
    if f.exists():
        return datetime.fromisoformat(json.loads(f.read_text())["extraction_date"]) + \
            timedelta(days=1)
    return datetime.now()


# ------------------------------------------------------------------------- cleaning
@dataclass
class CleanResult:
    clean: pd.DataFrame
    rejects: pd.DataFrame
    stats: dict = field(default_factory=dict)


def is_blank(v) -> bool:
    """None, NaN/NaT (pandas turns empty cells into these) or an empty string."""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _reason(r: dict, register: set[str], as_of: datetime) -> str | None:
    if r["equipement"] is None or is_blank(r["piece_raw"]) or is_blank(r["date_panne_raw"]):
        return "champ_manquant"
    if r["date_panne"] is None or (not is_blank(r["date_declaration_raw"])
                                   and r["date_declaration"] is None):
        return "date_illisible"
    if r["equipement"] not in register:
        return "equipement_inconnu"
    if r["piece"] is None or r["piece"].startswith("?"):
        return "piece_inconnue"
    if r["date_panne"] > as_of:
        return "date_future"
    if (r["date_declaration"] is not None and r["date_declaration"] < r["date_panne"]) or \
            (r["date_fin"] is not None and r["date_fin"] < r["date_panne"]):
        return "date_incoherente"
    return None


def clean_files(files: list[Path], register: pd.DataFrame, as_of: datetime) -> CleanResult:
    raw = pd.concat([read_file(f) for f in sorted(files)], ignore_index=True)
    reg = set(register["code"])
    recs = []
    for r in raw.to_dict("records"):
        rec = {
            "equipement": norm_code(r["equipement"]),
            "piece_raw": r["piece"], "piece": norm_part(r["piece"]),
            "date_panne_raw": r["date_panne"], "date_panne": parse_date(r["date_panne"]),
            "date_declaration_raw": r["date_declaration"],
            "date_declaration": parse_date(r["date_declaration"]),
            "date_fin": parse_date(r["date_fin"]),
            "gravite": norm_gravite(r["gravite"]),
            "technicien": None if is_blank(r["technicien"]) else str(r["technicien"]).strip(),
            "commentaire": None if is_blank(r["commentaire"]) else str(r["commentaire"]).strip(),
            "source_file": r["source_file"], "source_row": r["source_row"],
        }
        rec["reason"] = _reason(rec, reg, as_of)
        recs.append(rec)
    df = pd.DataFrame(recs).sort_values(["source_file", "source_row"]).reset_index(drop=True)
    # duplicates are judged only among rows that passed every other rule
    ok = df["reason"].isna()
    again = df[ok].duplicated(subset=["equipement", "piece", "date_panne"], keep="first")
    df.loc[again[again].index, "reason"] = "duplicate"
    clean = df[df["reason"].isna()].drop(columns=["reason", "piece_raw", "date_panne_raw",
                                                  "date_declaration_raw"])
    rejects = df[df["reason"].notna()].copy()
    for c in ("piece_raw", "date_panne_raw", "date_declaration_raw"):
        rejects[c] = rejects[c].map(lambda v: None if v is None else str(v))
    stats = {"files": len(files), "rows_read": int(len(df)), "rows_clean": int(len(clean)),
             "rows_rejected": int(len(rejects)),
             "rejected_share": round(len(rejects) / len(df), 4) if len(df) else 0.0,
             "by_reason": {k: int(v) for k, v in rejects["reason"].value_counts().items()},
             "as_of": as_of.isoformat(timespec="minutes")}
    return CleanResult(clean.reset_index(drop=True), rejects.reset_index(drop=True), stats)


def score_against_truth(res: CleanResult, truth_rows: pd.DataFrame) -> dict:
    """Synthetic data only: compare each row's outcome with the generator's label."""
    pred = pd.concat([
        res.clean[["source_file", "source_row"]].assign(pred=None),
        res.rejects[["source_file", "source_row", "reason"]].rename(columns={"reason": "pred"}),
    ])
    m = truth_rows.rename(columns={"file": "source_file", "row": "source_row"}).merge(
        pred, on=["source_file", "source_row"], how="left", validate="one_to_one")
    m["defect"] = m["defect"].where(m["defect"].notna(), None)
    # Which copy of a duplicated breakdown is "the duplicate" is arbitrary: score each group of
    # copies (clean original + labelled duplicates) as right when exactly one copy is kept and
    # the others are rejected as duplicates, and relabel the truth to match that copy choice.
    grp = m[m["defect"].isin([None, "duplicate"]) | m["defect"].isna()]
    for _, g in grp[grp["panne_id"].isin(m.loc[m["defect"] == "duplicate", "panne_id"])] \
            .groupby("panne_id"):
        kept = g[g["pred"].isna()]
        if len(kept) == 1 and (g.loc[g.index != kept.index[0], "pred"] == "duplicate").all():
            m.loc[g.index, "defect"] = "duplicate"
            m.loc[kept.index, "defect"] = None
    per = {}
    for kind, g in m[m["defect"].notna()].groupby("defect"):
        per[kind] = {"injected": int(len(g)), "caught_with_right_reason": int((g["pred"] == kind)
                                                                              .sum()),
                     "rejected_any_reason": int(g["pred"].notna().sum())}
    clean_truth = m[m["defect"].isna()]
    return {"rows": int(len(m)),
            "clean_rows_wrongly_rejected": int(clean_truth["pred"].notna().sum()),
            "defective_rows_kept": int((m["defect"].notna() & m["pred"].isna()).sum()),
            "per_defect": per}
