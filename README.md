# anticipation-de-pannes

Breakdown data for industrial mechanical equipment, from messy site spreadsheets to reliability
KPIs: cleaning of heterogeneous Excel files, an incremental Python load into a constrained
relational model (SQL Server target), automatic quality checks, SQL KPI views for a BI report, and
a Flask app so breakdowns are declared directly in the database instead of late in Excel.

> **Read this first.** This is a **demonstration rebuild** of a data-engineering internship
> project. It uses **no real company data**: a generator reproduces the problems of real site
> files (duplicates, incoherent dates, heterogeneous formats) on synthetic breakdowns. SQL Server
> is the target (T-SQL schema and views are generated and kept in sync by CI) but everything here
> runs and is tested on **SQLite**; no SQL Server instance and no Power BI report were used.

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
        ^
  app.py: Flask form + JSON API writing straight to the database
          server-side declaration time, delay reason required after 24 h, no overlaps
```

## Quickstart

Python 3.12+.

```bash
pip install -e ".[dev]"
pannes run                    # demo: 2024 files, nothing new, 2025 files arrive; then KPIs
pytest -q                     # 84 tests
pannes serve                  # Flask app on http://localhost:5000
pannes ddl                    # regenerate sql/sqlserver/schema.sql and views.sql
```

SQL Server: `pip install -e ".[sqlserver]"`, set
`DATABASE_URL=mssql+pyodbc://user:pass@server/db?driver=ODBC+Driver+18+for+SQL+Server`, then
`pannes load && pannes kpi` (untested here: no SQL Server available).

## Results (demo run: 120 machines, 18 quarterly files, seed 42)

Full report: [`reports/results.md`](reports/results.md).

**Incremental load.** 1,158 Excel rows describing 1,105 true breakdowns.

| Run | Files loaded | Files skipped | Rows loaded | Rows rejected |
|---|---|---|---|---|
| 2024 files | 12 | 0 | 705 | 67 |
| Same inbox again | 0 | 12 | 0 | 0 |
| 2025 files arrive | 6 | 12 | 343 | 43 |

**Cleaning.** 110 rows rejected (9.5 %): duplicate 53, incoherent dates 22, unknown equipment 12,
missing field 10, unreadable date 9, future date 4. Checked against the generator's labels, every
defective row is rejected with the right reason and no clean row is rejected. The rules were
written for these defect types, so this shows they work as specified, not that they catch unknown
errors.

**KPIs.** 1,048 breakdowns in the database; 12.5 % were declared more than 24 h late (the problem
the Flask app targets); MTTR 5.6 h; mean availability per machine 99.62 %. Bearings (Roulement)
fail most often, 11.2 times a month across the fleet.

![KPIs](reports/kpi.png)

**Flask app.** Declaring a breakdown that overlaps an open one on the same machine is refused with
the conflicting breakdown named; a breakdown older than 24 h is accepted only with a delay reason;
dates in the future, unknown equipment or parts and duplicates are refused.

## Design choices

- **Reject, never drop silently:** every Excel row ends up in `pannes` or in `etl_rejets` with its
  file, row and reason; a test checks the two add up to the rows read.
- **Rules in the database:** UNIQUE (equipment, part, start), CHECK on date order and allowed
  values, foreign keys; the app and the loader cannot bypass them.
- **One schema, two dialects:** SQLAlchemy Core generates the SQLite tables and the T-SQL DDL;
  KPI views differ only by date functions and are generated for both.
- **Incremental by file fingerprint:** re-running is safe; a corrected file replaces its rows.
- **Business rules outside Flask** (`declarations.py`), tested without a web server.

## Limitations

- Synthetic data; real files will contain problems this generator does not produce.
- Not run on SQL Server; the T-SQL is generated and compiles, but was not executed.
- No Power BI report; the KPI views are what it would connect to.
- Migrations: only new nullable columns are added automatically; anything else needs Alembic.
- The app has no authentication; it is a demonstration of the entry rules, not a production app.

## Layout

```
src/pannes/   config, synthetic, clean, db, kpi, declarations, app, report, cli; templates/
sql/sqlserver/ schema.sql and views.sql (generated T-SQL)
reports/      generated results and figure
tests/        84 tests
```
