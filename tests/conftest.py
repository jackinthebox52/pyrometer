import os
import tempfile

# The DB path is read when pyrometer.server.db is imported, so set it before any test imports it.
os.environ["PYROMETER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "bootstrap.duckdb")
os.environ.pop("PYROMETER_AUTH", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """A brand-new DuckDB file per test, so projects and runs never leak between tests."""
    from pyrometer.server import db

    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setattr(db, "_conn", None)
    yield
    if db._conn is not None:
        db._conn.close()
