"""Business rules for declaring a breakdown directly in the database (used by the Flask app).

Why an app at all: in the spreadsheets, breakdowns were often declared days late and sometimes
overlapped (a machine "broken twice at once"). The app closes both gaps at entry time:

- the declaration time is the SERVER clock, so it cannot be back-dated;
- a breakdown that started more than LATE_DECLARATION_HOURS ago can still be declared, but only
  with a reason for the delay (stored in motif_retard, and counted by the KPIs);
- a new breakdown cannot start while the same machine has an open breakdown or inside the
  interval of an existing one; a repair end cannot make two breakdowns overlap;
- duplicates are refused by the database's unique key, with a readable message.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from .clean import norm_gravite
from .config import GRAVITES, LATE_DECLARATION_HOURS
from .db import equipements, pannes, pieces, sites


@dataclass
class Result:
    ok: bool
    errors: list[str] = field(default_factory=list)
    panne_id: int | None = None
    late: bool = False


def _now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def parse_form_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _overlap(con, equipement_id: int, start: datetime, end: datetime | None,
             exclude: int | None = None):
    """Existing breakdown of this machine overlapping [start, end] (end None = still open)."""
    far = datetime(9999, 1, 1)
    p_end = func.coalesce(pannes.c.date_fin, far)
    q = select(pannes.c.panne_id, pannes.c.date_panne, pannes.c.date_fin).where(
        pannes.c.equipement_id == equipement_id,
        pannes.c.date_panne < (end or far),
        or_(p_end > start, and_(pannes.c.date_fin.is_(None), pannes.c.date_panne <= start)),
    )
    if exclude is not None:
        q = q.where(pannes.c.panne_id != exclude)
    return con.execute(q.limit(1)).first()


def declare(engine: Engine, form: dict, now: Callable[[], datetime] = _now) -> Result:
    errors = []
    t = now()
    code = (form.get("equipement") or "").strip()
    part = (form.get("piece") or "").strip()
    start = parse_form_datetime(form.get("date_panne"))
    end = parse_form_datetime(form.get("date_fin"))
    grav = norm_gravite(form.get("gravite"))
    tech = (form.get("technicien") or "").strip() or None
    motif = (form.get("motif_retard") or "").strip() or None
    comment = (form.get("commentaire") or "").strip() or None

    with engine.connect() as con:
        eq = con.execute(select(equipements).where(equipements.c.code == code)).first()
        pc = con.execute(select(pieces).where(pieces.c.nom == part)).first()
    if eq is None:
        errors.append(f"Équipement inconnu : {code or '(vide)'}.")
    if pc is None:
        errors.append(f"Pièce inconnue : {part or '(vide)'}.")
    if start is None:
        errors.append("Date de panne manquante ou illisible.")
    elif start > t:
        errors.append("La date de panne est dans le futur.")
    if form.get("gravite") and grav is None:
        errors.append(f"Gravité invalide (attendu : {', '.join(GRAVITES)}).")
    if end is not None and start is not None and end < start:
        errors.append("La fin de réparation est antérieure à la panne.")
    if end is not None and end > t:
        errors.append("La fin de réparation est dans le futur.")
    late = start is not None and (t - start) > timedelta(hours=LATE_DECLARATION_HOURS)
    if late and not motif:
        errors.append(f"Panne survenue il y a plus de {LATE_DECLARATION_HOURS:g} h : "
                      "indiquez le motif du retard de déclaration.")
    if errors:
        return Result(False, errors)

    with engine.begin() as con:
        clash = _overlap(con, eq.equipement_id, start, end)
        if clash is not None:
            fin = clash.date_fin.strftime("%d/%m/%Y %H:%M") if clash.date_fin else "en cours"
            return Result(False, [f"Chevauchement avec la panne n°{clash.panne_id} de {code} "
                                  f"({clash.date_panne:%d/%m/%Y %H:%M} → {fin})."])
        try:
            pid = con.execute(pannes.insert().values(
                equipement_id=eq.equipement_id, piece_id=pc.piece_id, date_panne=start,
                date_declaration=t, date_fin=end, gravite=grav, technicien=tech,
                commentaire=comment, motif_retard=motif if late else None, source="app",
                charge_le=t)).inserted_primary_key[0]
        except IntegrityError:
            return Result(False, ["Cette panne est déjà déclarée (même équipement, pièce et "
                                  "date)."])
    return Result(True, [], int(pid), late)


def close(engine: Engine, panne_id: int, date_fin: str | None,
          now: Callable[[], datetime] = _now) -> Result:
    end = parse_form_datetime(date_fin)
    t = now()
    with engine.connect() as con:
        p = con.execute(select(pannes).where(pannes.c.panne_id == panne_id)).first()
    if p is None:
        return Result(False, [f"Panne n°{panne_id} introuvable."])
    if end is None:
        return Result(False, ["Date de fin manquante ou illisible."])
    if end < p.date_panne:
        return Result(False, ["La fin de réparation est antérieure à la panne."])
    if end > t:
        return Result(False, ["La fin de réparation est dans le futur."])
    with engine.begin() as con:
        clash = _overlap(con, p.equipement_id, p.date_panne, end, exclude=panne_id)
        if clash is not None:
            return Result(False, [f"Chevauchement avec la panne n°{clash.panne_id}."])
        con.execute(pannes.update().where(pannes.c.panne_id == panne_id).values(date_fin=end))
    return Result(True, [], panne_id)


def reference_lists(engine: Engine) -> dict:
    with engine.connect() as con:
        eqs = con.execute(select(equipements.c.code, equipements.c.designation, sites.c.nom)
                          .join(sites).order_by(sites.c.nom, equipements.c.code)).all()
        parts = [r[0] for r in con.execute(select(pieces.c.nom).order_by(pieces.c.nom))]
    return {"equipements": [{"code": c, "designation": d, "site": s} for c, d, s in eqs],
            "pieces": parts, "gravites": GRAVITES}


def recent(engine: Engine, limit: int = 20) -> list[dict]:
    with engine.connect() as con:
        rows = con.execute(
            select(pannes.c.panne_id, equipements.c.code, pieces.c.nom.label("piece"),
                   pannes.c.date_panne, pannes.c.date_declaration, pannes.c.date_fin,
                   pannes.c.gravite, pannes.c.source, pannes.c.motif_retard)
            .join(equipements).join(pieces)
            .order_by(pannes.c.date_declaration.desc().nulls_last(), pannes.c.panne_id.desc())
            .limit(limit)).mappings().all()
    return [dict(r) for r in rows]
