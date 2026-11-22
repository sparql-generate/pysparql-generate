"""Test settings: every DuckDB connection of the tests is capped (DuckDB takes up to 80 % of
the RAM by default, and the machine is shared: run pytest under a memory cap too, e.g. systemd-run --user --scope -p MemoryMax=8G)."""
import pytest

from sparql_generate import engine as _engine


@pytest.fixture(autouse=True)
def _cap_duckdb(monkeypatch):
    orig = _engine.Engine.__init__

    def init(self, *a, memory_limit=None, threads=None, **k):
        orig(self, *a, memory_limit=memory_limit or "2GB", threads=threads or 4, **k)

    monkeypatch.setattr(_engine.Engine, "__init__", init)
