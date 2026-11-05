"""Flask app for declaring breakdowns.  Run:  flask --app pannes.app run   (or `pannes serve`)

Pages: GET / (form + latest breakdowns), POST /declarer, POST /pannes/<id>/cloturer.
JSON:  GET /api/pannes, POST /api/pannes (same rules as the form), GET /health.
"""
from __future__ import annotations

from datetime import datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from .config import LATE_DECLARATION_HOURS, get_paths
from .db import get_engine, init_db
from .declarations import close, declare, recent, reference_lists


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
