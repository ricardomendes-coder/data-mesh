import os
import tempfile

# Env MUST be set before importing the app (settings + user store read it at import).
_tmp = tempfile.mkdtemp()
os.environ["USERS_FILE"] = os.path.join(_tmp, "users.json")
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["INITIAL_ADMIN_USER"] = "admin"
os.environ["INITIAL_ADMIN_PASSWORD"] = "s3cret-pass"
os.environ["REPORTS_FILE"] = os.path.join(_tmp, "reports.toml")

from urllib.parse import urlsplit

import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import reports
from app.main import app


def _redirect_path(response) -> str:
    # Starlette's url_for() returns an absolute URL (e.g. http://testserver/login),
    # so compare the path, not the whole Location header.
    return urlsplit(response.headers["location"]).path


def test_auth_flow():
    # https base_url so the Secure session cookie (https_only=True) is retained.
    with TestClient(app, base_url="https://testserver") as client:  # `with` runs lifespan -> bootstrap admin
        # Unauthenticated dashboard redirects to /login
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and _redirect_path(r) == "/login", r.status_code

        # Protected POST routes redirect too (the new query console)
        r = client.post("/query", data={"sql": "SELECT 1"}, follow_redirects=False)
        assert r.status_code == 303 and _redirect_path(r) == "/login", r.status_code

        # Login page renders
        r = client.get("/login")
        assert r.status_code == 200 and "Log in" in r.text

        # Wrong password rejected
        r = client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 401, r.status_code

        # Correct password -> redirect to /
        r = client.post(
            "/login",
            data={"username": "admin", "password": "s3cret-pass"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and _redirect_path(r) == "/", r.status_code

        # Now the dashboard is reachable and shows the user + query console
        r = client.get("/")
        assert r.status_code == 200 and "Signed in as admin" in r.text
        assert "Run a query" in r.text, "query console missing from dashboard"

        # Logout clears the session
        r = client.post("/logout", follow_redirects=False)
        assert r.status_code == 303 and _redirect_path(r) == "/login"
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
    print("auth flow: OK")


def test_report_serialization():
    df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})

    csv_bytes = reports.to_csv_bytes(df)
    assert csv_bytes.startswith(b"id,name"), csv_bytes[:20]

    xlsx_bytes = reports.to_xlsx_bytes(df)
    # xlsx files are zip archives -> start with PK
    assert xlsx_bytes[:2] == b"PK", xlsx_bytes[:2]
    # round-trip it back
    back = pd.read_excel(__import__("io").BytesIO(xlsx_bytes))
    assert list(back.columns) == ["id", "name"] and len(back) == 2
    print("report serialization: OK")


def test_read_sql_pattern():
    # Validates the exact pd.read_sql(text(...)) pattern used in db.run_query,
    # against sqlite (no SSH/Postgres available in this sandbox).
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (a INTEGER, b TEXT)"))
        conn.execute(text("INSERT INTO t VALUES (1, 'x'), (2, 'y')"))
        conn.commit()
        df = pd.read_sql(text("SELECT a, b FROM t ORDER BY a"), conn)
    assert list(df.columns) == ["a", "b"] and df.iloc[0]["b"] == "x"
    print("read_sql pattern: OK")


def test_query_picker_and_reports():
    # Stub the "server": a temp SQLite DB stands in, and list_databases() returns
    # a fixed set (no Postgres in the sandbox). Patching the app.db module is
    # enough because main.py and reports.py both call it by attribute.
    from app import db

    dbfile = os.path.join(_tmp, "t.db")
    eng = create_engine(f"sqlite:///{dbfile}")
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER, name TEXT)"))
        conn.execute(text("INSERT INTO t VALUES (1, 'alice'), (2, NULL)"))
        conn.commit()

    db._engine = lambda database=None: create_engine(f"sqlite:///{dbfile}")
    db.list_databases = lambda: ["main", "other"]

    with open(os.environ["REPORTS_FILE"], "w") as f:
        f.write(
            '[[report]]\n'
            'key = "t_report"\n'
            'title = "Temp report"\n'
            'database = "main"\n'
            'sql = "SELECT * FROM t ORDER BY id"\n'
        )

    with TestClient(app, base_url="https://testserver") as client:
        client.post("/login", data={"username": "admin", "password": "s3cret-pass"})

        # Dashboard shows the DB picker + the manifest report
        r = client.get("/")
        assert ">main<" in r.text and ">other<" in r.text, "db picker not populated"
        assert "Temp report" in r.text, "report not listed"

        # Query against a listed database -> rendered table (NULL shown blank)
        r = client.post("/query", data={"sql": "SELECT * FROM t ORDER BY id", "database": "main"})
        assert r.status_code == 200 and "alice" in r.text, r.status_code

        # A database not in the list is rejected
        r = client.post("/query", data={"sql": "SELECT 1", "database": "bogus"})
        assert r.status_code == 400 and "Unknown database" in r.text, r.status_code

        # Report export -> CSV attachment named after the report key
        r = client.get("/report/t_report/export?format=csv")
        assert r.status_code == 200 and r.content.startswith(b"id,name"), r.content[:30]
        assert 'filename="t_report_' in r.headers["content-disposition"]

        # Unknown report key -> 404
        r = client.get("/report/nope/export", follow_redirects=False)
        assert r.status_code == 404, r.status_code
    print("query picker + reports: OK")


if __name__ == "__main__":
    test_auth_flow()
    test_report_serialization()
    test_read_sql_pattern()
    test_query_picker_and_reports()
    print("\nAll smoke tests passed.")
