"""Reference data (equipment types, parts, sites), thresholds and the working-directory layout.

Layout:
    inbox/            Excel files as received from the sites (what the pipeline reads)
    data/pannes.db    the database (SQLite locally; SQL Server through DATABASE_URL)
    data/_truth/      ground truth of the synthetic generator (never read by the pipeline)
    reports/          generated metrics and figures (committed)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SEED = 42

SITES = ["Casablanca", "Mohammedia", "Jorf Lasfar"]

# equipment type -> (code prefix, parts that fail on it)
EQUIPMENT_TYPES: dict[str, tuple[str, list[str]]] = {
    "Presse hydraulique": ("PRS", ["Vérin hydraulique", "Joint", "Pompe hydraulique",
                                   "Capteur de pression"]),
    "Compresseur": ("CMP", ["Filtre", "Courroie", "Roulement", "Clapet"]),
    "Convoyeur": ("CNV", ["Courroie", "Rouleau", "Moteur", "Roulement"]),
    "Pompe centrifuge": ("PMP", ["Garniture mécanique", "Roulement", "Turbine"]),
    "Pont roulant": ("PNT", ["Câble", "Frein", "Moteur de levage"]),
}
PARTS = sorted({p for _, parts in EQUIPMENT_TYPES.values() for p in parts})

GRAVITES = ["mineure", "majeure", "critique"]

# A declaration more than this many hours after the breakdown is "late" (KPI and Flask app).
LATE_DECLARATION_HOURS = 24.0


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def truth(self) -> Path:
        return self.data / "_truth"

    @property
    def db(self) -> Path:
        return self.data / "pannes.db"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    def database_url(self) -> str:
        """$DATABASE_URL (e.g. an mssql+pyodbc URL) or the local SQLite file."""
        return os.environ.get("DATABASE_URL") or f"sqlite:///{self.db}"

    def ensure(self) -> Paths:
        for d in (self.inbox, self.data, self.truth, self.reports):
            d.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(root: str | os.PathLike | None = None) -> Paths:
    """Explicit root > $PANNES_HOME > current directory."""
    return Paths(Path(root or os.environ.get("PANNES_HOME") or Path.cwd()).resolve())
