"""Flask app for declaring breakdowns.  Run:  flask --app pannes.app run   (or `pannes serve`)

Pages: GET / (form + latest breakdowns), POST /declarer, POST /pannes/<id>/cloturer,
       GET /risques (ranked 30-day risk, filterable by site).
CSV:   GET /pannes.csv (recent breakdowns), GET /risques.csv (risk list, honours ?site=).
JSON:  GET /api/pannes, POST /api/pannes (same rules as the form), GET /api/kpi, GET /health.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for

from .config import LATE_DECLARATION_HOURS, get_paths
from .db import get_engine, init_db
from .declarations import close, declare, recent, reference_lists

HIGH_RISK = 0.25     # threshold flagged in the risk list and its summary


def _csv_response(header: list[str], rows, filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _risk_query(engine, site: str):
    """(sites, rows) for the risk list; rows filtered to `site` when it is a known site."""
    from sqlalchemy import inspect, text

    if not inspect(engine).has_table("previsions_risque"):
        return [], []
    with engine.connect() as c:
        sites = [r[0] for r in c.execute(text(
            "select distinct site from previsions_risque order by site"))]
        q = "select * from previsions_risque"
        params = {}
        if site in sites:
            q += " where site = :site"
            params["site"] = site
        rows = c.execute(text(q + " order by proba_30j desc limit 200"),
                         params).mappings().all()
    return sites, rows


def create_app(database_url: str | None = None, now=None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev-only-change-me"           # flash messages only
    engine = get_engine(database_url or get_paths().database_url())
    init_db(engine)
    clock = now or (lambda: datetime.now().replace(second=0, microsecond=0))
    app.extensions["pannes_engine"] = engine

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def index():
        return render_template("index.html", ref=reference_lists(engine), pannes=recent(engine),
                               late_hours=LATE_DECLARATION_HOURS,
                               now=clock().strftime("%Y-%m-%dT%H:%M"), form={})

    @app.post("/declarer")
    def declarer():
        res = declare(engine, request.form, clock)
        if not res.ok:
            for e in res.errors:
                flash(e, "error")
            return render_template("index.html", ref=reference_lists(engine),
                                   pannes=recent(engine), late_hours=LATE_DECLARATION_HOURS,
                                   now=clock().strftime("%Y-%m-%dT%H:%M"),
                                   form=request.form), 422
        msg = f"Panne n°{res.panne_id} enregistrée."
        if res.late:
            msg += " Déclaration tardive : le motif a été enregistré."
        flash(msg, "ok")
        return redirect(url_for("index"))

    @app.post("/pannes/<int:panne_id>/cloturer")
    def cloturer(panne_id: int):
        res = close(engine, panne_id, request.form.get("date_fin"), clock)
        flash(f"Panne n°{panne_id} clôturée." if res.ok else " ".join(res.errors),
              "ok" if res.ok else "error")
        return redirect(url_for("index"))

    @app.get("/risques")
    def risques():
        site = request.args.get("site", "")
        sites, rows = _risk_query(engine, site)
        n_high = sum(1 for r in rows if r["proba_30j"] >= HIGH_RISK)
        asof = str(rows[0]["calcule_le"])[:10] if rows else ""
        return render_template("risques.html", rows=rows[:40], sites=sites, site=site,
                               asof=asof, n_high=n_high)

    @app.get("/risques.csv")
    def export_risques():
        cols = ["equipement", "type", "site", "piece", "jours_depuis_derniere", "jamais_vue",
                "n_piece_365", "proba_30j", "proba_taux_flotte", "calcule_le"]
        _, rows = _risk_query(engine, request.args.get("site", ""))
        return _csv_response(cols, ([r[c] for c in cols] for r in rows), "risques.csv")

    @app.get("/pannes.csv")
    def export_pannes():
        cols = ["panne_id", "code", "piece", "date_panne", "date_declaration", "date_fin",
                "gravite", "source", "motif_retard"]
        rows = recent(engine, limit=10000)
        return _csv_response(cols, ([r.get(c) for c in cols] for r in rows), "pannes.csv")

    @app.get("/api/kpi")
    def api_kpi():
        from .kpi import create_views, kpi_summary

        create_views(engine)
        return jsonify(kpi_summary(engine))

    @app.get("/api/pannes")
    def api_list():
        limit = min(int(request.args.get("limit", 20)), 200)
        return jsonify([{k: (v.isoformat() if isinstance(v, datetime) else v)
                         for k, v in r.items()} for r in recent(engine, limit)])

    @app.post("/api/pannes")
    def api_declare():
        res = declare(engine, request.get_json(force=True, silent=True) or {}, clock)
        body = {"ok": res.ok, "errors": res.errors, "panne_id": res.panne_id, "late": res.late}
        return jsonify(body), (201 if res.ok else 422)

    return app


app = None


def __getattr__(name):            # lazy `flask --app pannes.app` without touching the DB on import
    if name == "app":
        return create_app()
    raise AttributeError(name)
