import os
import tempfile

# Env MUST be set before importing the app (settings + user store read it at import).
_tmp = tempfile.mkdtemp()
os.environ["USERS_FILE"] = os.path.join(_tmp, "users.json")
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["INITIAL_ADMIN_USER"] = "admin"
os.environ["INITIAL_ADMIN_PASSWORD"] = "s3cret-pass"
os.environ["REPORTS_FILE"] = os.path.join(_tmp, "reports.toml")
# "both" so one process can exercise the SSO path and the break-glass password
# path. test_sso_only_mode() reloads the app under AUTH_MODE=sso separately.
os.environ["AUTH_MODE"] = "both"
os.environ["SSO_CLIENT_ID"] = "report-hub-test"
os.environ["SSO_CLIENT_SECRET"] = "test-client-secret"
os.environ["SSO_REDIRECT_URI"] = "https://testserver/auth/callback"

from urllib.parse import urlsplit

import pandas as pd
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import reports
from app.main import app

KEYCLOAK_AUTHZ = "https://sso.v360.io/realms/v360/protocol/openid-connect/auth"


def _redirect_path(response) -> str:
    # Starlette's url_for() returns an absolute URL (e.g. http://testserver/login),
    # so compare the path, not the whole Location header.
    return urlsplit(response.headers["location"]).path


class _FakeOIDCClient:
    """Stands in for the authlib client so tests never call out to sso.v360.io."""

    def __init__(self, claims: dict):
        self.claims = claims
        self.redirect_uris: list[str] = []

    async def authorize_redirect(self, request, redirect_uri):
        self.redirect_uris.append(redirect_uri)
        return RedirectResponse(f"{KEYCLOAK_AUTHZ}?state=fake", status_code=302)

    async def authorize_access_token(self, request):
        return {"access_token": "fake-token", "userinfo": self.claims}


def test_auth_flow():
    # https base_url so the Secure session cookie (https_only=True) is retained.
    with TestClient(app, base_url="https://testserver") as client:  # `with` runs lifespan -> bootstrap admin
        # Unauthenticated dashboard redirects to /login
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and _redirect_path(r) == "/login", r.status_code

        # Protected POST routes redirect too (the new query console)
        r = client.post("/query", data={"sql": "SELECT 1"}, follow_redirects=False)
        assert r.status_code == 303 and _redirect_path(r) == "/login", r.status_code

        # With SSO live, /login is not a page — it hands off to Keycloak
        r = client.get("/login", follow_redirects=False)
        assert r.status_code == 303 and _redirect_path(r) == "/auth/sso", r.status_code

        # The break-glass form still renders, and offers both paths
        r = client.get("/login/local")
        assert r.status_code == 200 and "Log in" in r.text
        assert "Continue with V360 SSO" in r.text, "SSO button missing from login page"

        # Wrong password rejected
        r = client.post(
            "/login/local",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 401, r.status_code

        # Correct password -> redirect to /
        r = client.post(
            "/login/local",
            data={"username": "admin", "password": "s3cret-pass"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and _redirect_path(r) == "/", r.status_code

        # Now the dashboard is reachable and shows the user + Query/Reports tabs
        r = client.get("/")
        assert r.status_code == 200 and "Signed in as admin" in r.text
        assert 'data-tab="query"' in r.text and 'data-tab="reports"' in r.text, (
            "query/reports tabs missing from dashboard"
        )

        # Logout clears the session. Password sessions never touch Keycloak.
        r = client.post("/logout", follow_redirects=False)
        assert r.status_code == 303 and _redirect_path(r) == "/login"
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
    print("auth flow: OK")


def test_sso_flow():
    from app import main as main_mod

    fake = _FakeOIDCClient(
        {
            "preferred_username": "marcelo.ferreira",
            "email": "marcelo.ferreira@v360.io",
            "name": "Marcelo Ferreira",
            "given_name": "Marcelo",
            "family_name": "Ferreira",
        }
    )
    original = main_mod.oidc.get_client
    main_mod.oidc.get_client = lambda: fake
    try:
        with TestClient(app, base_url="https://testserver") as client:
            # /auth/sso redirects out to Keycloak, using the configured URI
            r = client.get("/auth/sso", follow_redirects=False)
            assert r.status_code == 302, r.status_code
            assert r.headers["location"].startswith(KEYCLOAK_AUTHZ), r.headers["location"]
            assert fake.redirect_uris == ["https://testserver/auth/callback"], (
                f"wrong redirect_uri sent to Keycloak: {fake.redirect_uris}"
            )

            # The callback establishes the session under preferred_username,
            # matching how Superset names the same person.
            r = client.get("/auth/callback?code=fake&state=fake", follow_redirects=False)
            assert r.status_code == 303 and _redirect_path(r) == "/", r.status_code

            r = client.get("/")
            assert r.status_code == 200, r.status_code
            assert "Signed in as marcelo.ferreira" in r.text, "SSO user not on dashboard"

            # Logout is local-only by default so BI 360 stays signed in
            r = client.post("/logout", follow_redirects=False)
            assert _redirect_path(r) == "/login", r.headers["location"]
            assert "sso.v360.io" not in r.headers["location"], (
                "default logout must not hit Keycloak's end_session endpoint"
            )
            r = client.get("/", follow_redirects=False)
            assert r.status_code == 303, "session survived logout"
    finally:
        main_mod.oidc.get_client = original
    print("sso flow: OK")


def test_superset_delegated_mode():
    """AUTH_MODE=superset: identity comes from Superset's /api/v1/me/."""
    import importlib

    from app import config
    from app import main as main_mod

    os.environ["AUTH_MODE"] = "superset"
    config.get_settings.cache_clear()
    reloaded = importlib.reload(main_mod)

    calls: list[str] = []

    async def fake_identify(cookie_value):
        calls.append(cookie_value)
        if cookie_value == "valid-superset-cookie":
            return {
                "username": "marcelo.ferreira",
                "email": "marcelo.ferreira@v360.io",
                "first_name": "Marcelo",
                "last_name": "Ferreira",
                "is_anonymous": False,
            }
        return None

    original = reloaded.superset_session.identify
    reloaded.superset_session.identify = fake_identify
    try:
        assert reloaded.settings.superset_auth_enabled
        with TestClient(reloaded.app, base_url="https://testserver") as client:
            # No Superset cookie -> bounced to the Superset login, which owns
            # the registered Keycloak redirect URI.
            r = client.get("/", follow_redirects=False)
            assert r.status_code == 303 and _redirect_path(r) == "/login"
            r = client.get("/login", follow_redirects=False)
            loc = r.headers["location"]
            assert loc.startswith("https://bi.v360.io/login/"), loc
            assert "next=" in loc, f"no next= param to bring the user back: {loc}"
            assert calls == [], "identify() called without a cookie present"

            # A cookie Superset rejects must not grant access either.
            # Sent as a header: httpx's jar won't attach a cookie to the
            # "testserver" host without a domain match.
            r = client.get(
                "/login",
                headers={"Cookie": "session=stale-cookie"},
                follow_redirects=False,
            )
            assert r.headers["location"].startswith("https://bi.v360.io/login/")
            assert calls == ["stale-cookie"], calls

            # A cookie Superset accepts logs the user straight in
            r = client.get(
                "/login",
                headers={"Cookie": "session=valid-superset-cookie"},
                follow_redirects=False,
            )
            assert r.status_code == 303 and _redirect_path(r) == "/", r.status_code
            r = client.get("/")
            assert "Signed in as marcelo.ferreira" in r.text, "delegated login failed"

            # Logout goes through Superset — clearing only our cookie would let
            # the next request sign straight back in.
            r = client.post("/logout", follow_redirects=False)
            assert r.headers["location"] == "https://bi.v360.io/logout/", r.headers[
                "location"
            ]

            # Direct OIDC endpoints are inert in this mode
            assert client.get("/auth/sso", follow_redirects=False).status_code == 404
            assert client.get("/login/local", follow_redirects=False).status_code == 404
    finally:
        reloaded.superset_session.identify = original
        os.environ["AUTH_MODE"] = "both"
        config.get_settings.cache_clear()
        importlib.reload(main_mod)
    print("superset delegated mode: OK")


def test_superset_break_glass():
    """ENABLE_LOCAL_LOGIN works alongside delegated auth, for when BI is down."""
    import importlib

    from app import config
    from app import main as main_mod

    os.environ["AUTH_MODE"] = "superset"
    os.environ["ENABLE_LOCAL_LOGIN"] = "true"
    config.get_settings.cache_clear()
    reloaded = importlib.reload(main_mod)
    try:
        with TestClient(reloaded.app, base_url="https://testserver") as client:
            r = client.get("/login/local")
            assert r.status_code == 200, r.status_code
            assert "Continue with BI 360" in r.text, "BI 360 button missing"
            r = client.post(
                "/login/local",
                data={"username": "admin", "password": "s3cret-pass"},
                follow_redirects=False,
            )
            assert r.status_code == 303 and _redirect_path(r) == "/", r.status_code
    finally:
        os.environ["AUTH_MODE"] = "both"
        os.environ.pop("ENABLE_LOCAL_LOGIN", None)
        config.get_settings.cache_clear()
        importlib.reload(main_mod)
    print("superset break-glass: OK")


def test_sso_only_mode():
    """Under AUTH_MODE=sso the password endpoints must not exist at all."""
    import importlib

    from app import config
    from app import main as main_mod

    os.environ["AUTH_MODE"] = "sso"
    config.get_settings.cache_clear()
    reloaded = importlib.reload(main_mod)
    try:
        assert reloaded.settings.sso_enabled and not reloaded.settings.password_login_enabled
        with TestClient(reloaded.app, base_url="https://testserver") as client:
            r = client.get("/login/local", follow_redirects=False)
            assert r.status_code == 404, r.status_code
            r = client.post(
                "/login/local",
                data={"username": "admin", "password": "s3cret-pass"},
                follow_redirects=False,
            )
            assert r.status_code == 404, r.status_code
            r = client.get("/login", follow_redirects=False)
            assert _redirect_path(r) == "/auth/sso", r.headers["location"]
    finally:
        os.environ["AUTH_MODE"] = "both"
        config.get_settings.cache_clear()
        importlib.reload(main_mod)
    print("sso-only mode: OK")


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
        client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

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
    test_sso_flow()
    test_report_serialization()
    test_read_sql_pattern()
    test_query_picker_and_reports()
    # Last: these reload app.main, which rebinds this module's `app` reference.
    test_superset_delegated_mode()
    test_superset_break_glass()
    test_sso_only_mode()
    print("\nAll smoke tests passed.")
