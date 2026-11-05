"""Check Phase 1 Python dependencies and SQLite capabilities without writing trip data."""

import importlib
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def main() -> int:
    failures = []
    if sys.version_info[:2] != (3, 13):
        failures.append("Python 3.13 is required by the current project baseline")

    for name in (
        "fastapi",
        "uvicorn",
        "pydantic_settings",
        "sqlalchemy",
        "alembic",
        "httpx",
        "pytest",
    ):
        try:
            importlib.import_module(name)
            print(f"PASS dependency: {name}")
        except ImportError:
            failures.append(f"Missing dependency: {name}")

    try:
        offset = datetime(2026, 11, 3, tzinfo=ZoneInfo("Asia/Kolkata")).utcoffset()
        assert offset == timedelta(hours=5, minutes=30)
        print("PASS Hyderabad timezone data")
    except (KeyError, AssertionError) as exc:
        failures.append(f"Timezone support failed: {exc}")

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            "CREATE TABLE item(id TEXT PRIMARY KEY);"
            "CREATE TABLE edge(parent TEXT REFERENCES item(id), "
            "child TEXT REFERENCES item(id));"
            "INSERT INTO item VALUES ('flight'), ('transfer'), ('hotel');"
            "INSERT INTO edge VALUES ('flight', 'transfer'), ('transfer', 'hotel');"
        )
        descendants = connection.execute(
            "WITH RECURSIVE affected(id) AS ("
            "SELECT child FROM edge WHERE parent = ? UNION "
            "SELECT edge.child FROM edge JOIN affected ON edge.parent = affected.id) "
            "SELECT id FROM affected ORDER BY id",
            ("flight",),
        ).fetchall()
        assert descendants == [("hotel",), ("transfer",)]
        try:
            connection.execute("INSERT INTO edge VALUES ('missing', 'hotel')")
        except sqlite3.IntegrityError:
            connection.rollback()
        else:
            raise AssertionError("Foreign-key enforcement is unavailable")
        connection.execute("INSERT INTO item VALUES ('temporary')")
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM item WHERE id = 'temporary'"
        ).fetchone() == (0,)
        print(f"PASS SQLite {sqlite3.sqlite_version}: traversal, foreign keys, rollback")
    except (sqlite3.Error, AssertionError) as exc:
        failures.append(f"SQLite capability check failed: {exc}")
    finally:
        connection.close()

    for failure in failures:
        print(f"FAIL {failure}")
    print("Environment checks passed" if not failures else "Environment needs attention")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
