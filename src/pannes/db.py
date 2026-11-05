"""Data model (equipment, parts, breakdowns), incremental loading and automatic quality checks.

The schema is declared once with SQLAlchemy Core and rendered for both targets:
- SQLite, used locally and in the tests;
- SQL Server: `pannes ddl` writes sql/sqlserver/schema.sql (T-SQL), and DATABASE_URL can point the
  whole pipeline at an mssql+pyodbc connection.

Constraints carry the business rules, so bad data cannot enter even outside the pipeline (for
example through the Flask app): one breakdown per (equipment, part, start time), declaration and
repair end not before the breakdown, severity in an allowed list, foreign keys everywhere.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Unicode,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.schema import CreateIndex, CreateTable

from .clean import clean_files, read_extraction_date, read_register
from .config import GRAVITES, LATE_DECLARATION_HOURS, PARTS, Paths

metadata = MetaData()

sites = Table(
    "sites", metadata,
    Column("site_id", Integer, primary_key=True),
    Column("nom", Unicode(50), nullable=False, unique=True),
)

equipements = Table(
    "equipements", metadata,
    Column("equipement_id", Integer, primary_key=True),
    Column("code", String(20), nullable=False, unique=True),
    Column("designation", Unicode(100), nullable=False),
    Column("site_id", Integer, ForeignKey("sites.site_id"), nullable=False),
    Column("mise_en_service", Date),
)

pieces = Table(
    "pieces", metadata,
    Column("piece_id", Integer, primary_key=True),
    Column("nom", Unicode(100), nullable=False, unique=True),
)

_grav = ", ".join(f"'{g}'" for g in GRAVITES)
pannes = Table(
    "pannes", metadata,
    Column("panne_id", Integer, primary_key=True),
    Column("equipement_id", Integer, ForeignKey("equipements.equipement_id"), nullable=False,
           index=True),
    Column("piece_id", Integer, ForeignKey("pieces.piece_id"), nullable=False, index=True),
    Column("date_panne", DateTime, nullable=False, index=True),
    Column("date_declaration", DateTime),
    Column("date_fin", DateTime),
    Column("gravite", String(10)),
    Column("technicien", Unicode(100)),
    Column("commentaire", Unicode(500)),
    Column("source", String(10), nullable=False),           # 'excel' or 'app'
    Column("source_file", Unicode(200)),
    Column("source_row", Integer),
    Column("charge_le", DateTime, nullable=False),
    UniqueConstraint("equipement_id", "piece_id", "date_panne", name="uq_panne"),
    CheckConstraint("date_declaration IS NULL OR date_declaration >= date_panne",
                    name="ck_declaration_apres_panne"),
    CheckConstraint("date_fin IS NULL OR date_fin >= date_panne", name="ck_fin_apres_panne"),
    CheckConstraint(f"gravite IS NULL OR gravite IN ({_grav})", name="ck_gravite"),
    CheckConstraint("source IN ('excel', 'app')", name="ck_source"),
)

etl_fichiers = Table(
    "etl_fichiers", metadata,
    Column("fichier", Unicode(200), primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("lignes_lues", Integer, nullable=False),
    Column("lignes_chargees", Integer, nullable=False),
    Column("charge_le", DateTime, nullable=False),
)

etl_rejets = Table(
    "etl_rejets", metadata,
    Column("rejet_id", Integer, primary_key=True),
    Column("fichier", Unicode(200), nullable=False, index=True),
    Column("ligne", Integer, nullable=False),
    Column("raison", String(30), nullable=False),
    Column("equipement", Unicode(50)),
    Column("piece", Unicode(100)),
    Column("date_panne_brute", Unicode(50)),
    Column("charge_le", DateTime, nullable=False),
)

qualite_controles = Table(
    "qualite_controles", metadata,
    Column("controle_id", Integer, primary_key=True),
    Column("execute_le", DateTime, nullable=False),
    Column("controle", String(50), nullable=False),
    Column("nb_anomalies", Integer, nullable=False),
    Column("seuil", Float),
)


# ------------------------------------------------------------------------- engine
def get_engine(url: str) -> Engine:
    eng = create_engine(url, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):                      # SQLite enforces FKs only when asked
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return eng


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)
    with engine.begin() as con:
        known = {r[0] for r in con.execute(select(pieces.c.nom))}
        new = [{"nom": p} for p in PARTS if p not in known]
        if new:
            con.execute(pieces.insert(), new)


def sqlserver_ddl() -> str:
    """T-SQL DDL for SQL Server, generated from the same metadata."""
    from sqlalchemy.dialects import mssql

    dialect = mssql.dialect()
    parts = []
    for t in metadata.sorted_tables:
        parts.append(str(CreateTable(t).compile(dialect=dialect)).strip() + ";\nGO")
        for ix in sorted(t.indexes, key=lambda i: i.name):
            parts.append(str(CreateIndex(ix).compile(dialect=dialect)).strip() + ";\nGO")
    return "-- Generated by `pannes ddl` from src/pannes/db.py. Do not edit by hand.\n\n" + \
        "\n\n".join(parts) + "\n"


# ------------------------------------------------------------------------- loading
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upsert_register(engine: Engine, register: pd.DataFrame) -> int:
    """Insert new sites and equipment from the register (existing codes are left untouched)."""
    with engine.begin() as con:
        site_ids = {r.nom: r.site_id for r in con.execute(select(sites))}
        for s in sorted(set(register["site"]) - set(site_ids)):
            site_ids[s] = con.execute(sites.insert().values(nom=s)).inserted_primary_key[0]
        known = {r[0] for r in con.execute(select(equipements.c.code))}
        new = [{"code": r.code, "designation": r.type, "site_id": site_ids[r.site],
                "mise_en_service": r.mise_en_service}
               for r in register.itertuples() if r.code not in known]
        if new:
            con.execute(equipements.insert(), new)
    return len(new)


def load_incremental(paths: Paths, engine: Engine | None = None) -> dict:
    """Load new or changed files from the inbox. Unchanged files are skipped (same SHA-256);
    a changed file replaces everything previously loaded from it."""
    engine = engine or get_engine(paths.database_url())
    init_db(engine)
    register = read_register(paths.inbox / "referentiel_equipements.xlsx")
    n_eq = upsert_register(engine, register)
    with engine.connect() as con:
        seen = {r.fichier: r.sha256 for r in con.execute(select(etl_fichiers))}
    files = sorted(paths.inbox.glob("pannes_*.xlsx"))
    todo = [f for f in files if seen.get(f.name) != sha256(f)]
    report = {"files_in_inbox": len(files), "files_loaded": len(todo),
              "files_skipped": len(files) - len(todo), "equipements_added": n_eq,
              "rows_read": 0, "rows_loaded": 0, "rows_rejected": 0, "by_reason": {}}
    if not todo:
        report["quality"] = run_quality_checks(engine)
        return report

    res = clean_files(todo, register, read_extraction_date(paths.inbox))
    now = datetime.now().replace(microsecond=0)
    with engine.begin() as con:
        for f in todo:                                   # a changed file replaces its old rows
            if f.name in seen:
                con.execute(pannes.delete().where(pannes.c.source_file == f.name))
                con.execute(etl_rejets.delete().where(etl_rejets.c.fichier == f.name))
        eq_ids = {r.code: r.equipement_id for r in con.execute(select(equipements))}
        part_ids = {r.nom: r.piece_id for r in con.execute(select(pieces))}
        existing = {(r.equipement_id, r.piece_id, _dt(r.date_panne)) for r in con.execute(
            select(pannes.c.equipement_id, pannes.c.piece_id, pannes.c.date_panne))}
        rows, already = [], []
        for r in res.clean.itertuples():
            key = (eq_ids[r.equipement], part_ids[r.piece], _dt(r.date_panne))
            if key in existing:                          # loaded earlier from another file
                already.append(r)
                continue
            existing.add(key)
            rows.append({"equipement_id": key[0], "piece_id": key[1], "date_panne": key[2],
                         "date_declaration": _dt(r.date_declaration), "date_fin": _dt(r.date_fin),
                         "gravite": r.gravite, "technicien": r.technicien,
                         "commentaire": r.commentaire, "source": "excel",
                         "source_file": r.source_file, "source_row": int(r.source_row),
                         "charge_le": now})
        if rows:
            con.execute(pannes.insert(), rows)
        rej = [{"fichier": r.source_file, "ligne": int(r.source_row), "raison": r.reason,
                "equipement": _s(r.equipement), "piece": _s(r.piece_raw),
                "date_panne_brute": _s(r.date_panne_raw), "charge_le": now}
               for r in res.rejects.itertuples()]
        rej += [{"fichier": r.source_file, "ligne": int(r.source_row), "raison": "duplicate",
                 "equipement": r.equipement, "piece": r.piece,
                 "date_panne_brute": str(r.date_panne), "charge_le": now} for r in already]
        if rej:
            con.execute(etl_rejets.insert(), rej)
        per_file_loaded = pd.Series([x["source_file"] for x in rows]).value_counts()
        per_file_read = pd.concat([res.clean["source_file"], res.rejects["source_file"]]) \
            .value_counts()
        for f in todo:
            con.execute(etl_fichiers.delete().where(etl_fichiers.c.fichier == f.name))
            con.execute(etl_fichiers.insert().values(
                fichier=f.name, sha256=sha256(f), lignes_lues=int(per_file_read.get(f.name, 0)),
                lignes_chargees=int(per_file_loaded.get(f.name, 0)), charge_le=now))
    by_reason = pd.Series([x["raison"] for x in rej]).value_counts()
    report.update({"rows_read": res.stats["rows_read"], "rows_loaded": len(rows),
                   "rows_rejected": len(rej),
                   "by_reason": {k: int(v) for k, v in by_reason.items()}})
    report["quality"] = run_quality_checks(engine)
    return report


def _dt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    return pd.Timestamp(v).to_pydatetime()


def _s(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


# ---------------------------------------------------------------------- quality checks
# Each check is a query returning the offending rows; the count is logged after every load.
QUALITY_CHECKS: dict[str, tuple[str, float | None]] = {
    "chevauchements": ("""
        select a.panne_id from pannes a join pannes b
          on a.equipement_id = b.equipement_id and a.panne_id < b.panne_id
         and b.date_panne < coalesce(a.date_fin, a.date_panne)
         and a.date_panne < coalesce(b.date_fin, b.date_panne)""", None),
    "declarations_tardives": ("""
        select panne_id from pannes
         where date_declaration is not null
           and {hours_between} > :seuil""", LATE_DECLARATION_HOURS),
    "reparations_sans_fin": ("select panne_id from pannes where date_fin is null", None),
    "reparations_plus_7_jours": ("""
        select panne_id from pannes where date_fin is not null
           and {hours_between_fin} > :seuil""", 168.0),
    "equipements_sans_panne": ("""
        select e.equipement_id from equipements e
         where not exists (select 1 from pannes p where p.equipement_id = e.equipement_id)""",
                               None),
}


def hours_between(engine_or_dialect, a: str, b: str) -> str:
    """Portable 'hours from a to b' SQL expression."""
    name = getattr(engine_or_dialect, "name", engine_or_dialect)
    if name == "sqlite":
        return f"((julianday({b}) - julianday({a})) * 24.0)"
    if name == "mssql":
        return f"(DATEDIFF(minute, {a}, {b}) / 60.0)"
    return f"(EXTRACT(EPOCH FROM ({b} - {a})) / 3600.0)"


def run_quality_checks(engine: Engine) -> dict[str, int]:
    now = datetime.now().replace(microsecond=0)
    out = {}
    hb = hours_between(engine.dialect, "date_panne", "date_declaration")
    hbf = hours_between(engine.dialect, "date_panne", "date_fin")
    with engine.begin() as con:
        for name, (sql, seuil) in QUALITY_CHECKS.items():
            q = text(f"select count(*) from ({sql.format(hours_between=hb, hours_between_fin=hbf)})"
                     " t")
            n = con.execute(q, {"seuil": seuil} if seuil is not None else {}).scalar_one()
            out[name] = int(n)
            con.execute(qualite_controles.insert().values(
                execute_le=now, controle=name, nb_anomalies=int(n), seuil=seuil))
    return out
