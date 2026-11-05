"""Shared fixture: one loaded database built once per test session."""
import pytest

from pannes.config import get_paths
from pannes.db import get_engine, load_incremental
from pannes.synthetic import generate


@pytest.fixture(scope="session")
def loaded(tmp_path_factory):
    paths = get_paths(tmp_path_factory.mktemp("pannes_home")).ensure()
    run = generate(paths, seed=5)
    report = load_incremental(paths)
    return paths, run, report, get_engine(paths.database_url())


@pytest.fixture
def home_env(loaded, monkeypatch):
    paths = loaded[0]
    monkeypatch.setenv("PANNES_HOME", str(paths.root))
    return paths
