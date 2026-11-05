# anticipation-de-pannes

Breakdown data for industrial mechanical equipment, from messy site spreadsheets to predictive
maintenance: cleaning of heterogeneous Excel files, an incremental Python load into a constrained
relational model (SQL Server target), automatic quality checks, SQL reliability KPIs, a Flask app
so breakdowns are declared directly in the database, and a model that ranks which part of which
machine is likely to fail in the next 30 days.

> **Read this first.** This is a **demonstration rebuild** of a data-engineering internship
> project. It uses **no real company data**: a generator reproduces the problems of real site
> files (duplicates, incoherent dates, heterogeneous formats) on synthetic breakdowns whose
> failure process includes part wear and fragile machines. SQL Server is the target (T-SQL schema
> and views are generated and kept in sync by CI) but everything here runs and is tested on
> **SQLite**; no SQL Server instance and no Power BI report were used.

## How it works

```
sites' Excel files (3 templates, 4 date encodings)        equipment register (Excel)
        │                                                         │
        v                                                         v
  clean.py: find header, map columns, parse dates, normalise codes/parts/severities
        │   reject rows with a reason: missing field, unreadable date, unknown equipment,
        │   unknown part, future date, incoherent dates, duplicate
        v
  db.py: incremental load (SHA-256 per file: unchanged skipped, changed reloaded)
        │   tables sites / equipements / pieces / pannes with FK, UNIQUE and CHECK constraints
        │   etl_fichiers, etl_rejets, qualite_controles (5 checks after every load)
        v
  kpi.py: views v_pannes_detail, v_kpi_piece, v_kpi_equipement, v_kpi_mensuel ──> BI report
  prevision.py: monthly snapshots -> 30-day failure risk per machine-part ──> previsions_risque
        ^
  app.py: Flask form + JSON API writing straight to the database   /risques: ranked risk list
          server-side declaration time, delay reason required after 24 h, no overlaps
```

## Quickstart

Python 3.12+.

```bash
pip install -e ".[dev]"
pannes run                    # demo: 2024 files, nothing new, 2025 files arrive; KPIs; risk backtest
pytest -q                     # 96 tests
pannes serve                  # Flask app on http://localhost:5000 (risk list at /risques)
pannes predict                # re-run the risk backtest and score current risk
pannes ddl                    # regenerate sql/sqlserver/schema.sql and views.sql
```

SQL Server: `pip install -e ".[sqlserver]"`, set
`DATABASE_URL=mssql+pyodbc://user:pass@server/db?driver=ODBC+Driver+18+for+SQL+Server`, then
`pannes load && pannes kpi` (untested here: no SQL Server available).

## Results (demo run: 120 machines, 18 quarterly files, seed 42)

Full report: [`reports/results.md`](reports/results.md).

**Incremental load.** 1,226 Excel rows describing 1,180 true breakdowns.

| Run | Files loaded | Files skipped | Rows loaded | Rows rejected |
|---|---|---|---|---|
| 2024 files | 12 | 0 | 759 | 67 |
| Same inbox again | 0 | 12 | 0 | 0 |
| 2025 files arrive | 6 | 12 | 358 | 42 |

**Cleaning.** 109 rows rejected (8.9 %): duplicate 46, incoherent dates 22, unreadable date 15,
unknown equipment 11, missing field 8, future date 7. Checked against the generator's labels,
every defective row is rejected with the right reason and no clean row is rejected. The rules
were written for these defect types, so this shows they work as specified, not that they catch
unknown errors.

**KPIs.** 1,117 breakdowns in the database; 11.7 % were declared more than 24 h late (the problem
the Flask app targets); MTTR 6.0 h; mean availability per machine 99.57 %. Bearings (Roulement)
fail most often, 11.0 times a month across the fleet.

![KPIs](reports/kpi.png)

**Predictive maintenance.** On the 1st of each month, 432 machine-parts are scored for "fails
within 30 days" using only past breakdowns (time since that part last failed, recent machine
history, machine age, part, type, site). Trained on 2024, tested once on the 2025 cutoffs; 95 %
intervals by bootstrap over machines.

| Model | AUC [95 % CI] | Top 20 per month: real failures |
|---|---|---|
| Fleet rate per part (baseline) | 0.660 [0.634, 0.693] | 23 / 100 |
| Machine-part own rate | 0.687 [0.658, 0.714] | 39 / 100 |
| Logistic regression | 0.723 [0.693, 0.750] | 42 / 100 |
| **Gradient boosting** | **0.724 [0.695, 0.754]** | 41 / 100 |

- Ranking improves clearly over the fleet-rate baseline (gradient boosting gains +0.035 to +0.092
  AUC): a monthly inspection list of the top 20 machine-parts hits about twice as many real
  failures as one built from average part rates.
- It is still a modest model: the top 20 catch only 41 of 292 failures in the test period, and
  the most useful signals are the part's fleet rate and the time since its last failure.
- Probabilities are not well calibrated at the top for gradient boosting (highest bin: 0.48
  predicted, 0.34 observed); logistic regression is better calibrated (0.30 vs 0.31). Read the
  risk list as a ranking, not as exact probabilities.
- For spare parts (expected failures per part and month) the models do **not** beat simple
  rates (MAE 1.46 for gradient boosting vs 1.33 for the fleet rate).

![Backtest](reports/prevision.png)

**Flask app.** Declaring a breakdown that overlaps an open one on the same machine is refused with
the conflicting breakdown named; a breakdown older than 24 h is accepted only with a delay reason;
dates in the future, unknown equipment or parts and duplicates are refused. `/risques` lists the
machine-parts with the highest 30-day risk, filterable by site.

## Design choices

- **Reject, never drop silently:** every Excel row ends up in `pannes` or in `etl_rejets` with its
  file, row and reason; a test checks the two add up to the rows read.
- **Rules in the database:** UNIQUE (equipment, part, start), CHECK on date order and allowed
  values, foreign keys; the app and the loader cannot bypass them.
- **One schema, two dialects:** SQLAlchemy Core generates the SQLite tables and the T-SQL DDL;
  KPI views differ only by date functions and are generated for both.
- **Prediction without leakage:** features at a cutoff only use earlier breakdowns (tested by
  deleting the future and checking nothing changes); training labels end before the test period.
- **Honest baselines:** every model is compared with what a planner would do without ML (average
  rates), with intervals that account for correlated failures of the same machine.

## Limitations

- Synthetic data; real files will contain problems this generator does not produce, and real
  failures may be less (or more) predictable than this wear-out simulation. The generator was
  changed to include wear and fragile machines before any model was written, but the same person
  wrote both.
- Not run on SQL Server; the T-SQL is generated and compiles, but was not executed.
- No Power BI report; the KPI views are what it would connect to.
- Migrations: only new nullable columns are added automatically; anything else needs Alembic.
- The app has no authentication; it is a demonstration of the entry rules, not a production app.

## Layout

```
src/pannes/    config, synthetic, clean, db, kpi, prevision, declarations, app, report, cli;
               templates/
sql/sqlserver/ schema.sql and views.sql (generated T-SQL)
reports/       generated results and figures
tests/         96 tests
```
