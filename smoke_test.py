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


CATALOG_FIXTURE = [
    # (name, kind, reltuples, column_count)
    ("companies", "table", 1420, 5),
    ("captura", "matview", 98000, 12),
    ("cs_healthscore", "view", -1, 7),
    ("aux_task_classification", "table", 30, 3),
    ("temp_scratch", "table", 5, 2),
    ("teste_pagina1_20260716171556", "table", 9, 4),
]


def _install_catalog_stub(datasets_mod):
    """Point the datasets module at fixtures so tests need no Postgres."""
    datasets_mod._fetch_objects = lambda: list(CATALOG_FIXTURE)
    datasets_mod._fetch_columns = lambda name: [
        ("id", "integer", False, "nextval('x')"),
        ("legal_name", "text", True, None),
    ]
    datasets_mod._fetch_preview = lambda name, limit: (
        ["id", "legal_name"],
        [(1, "Whirlpool"), (2, None)],
    )


def test_datasets_catalog():
    """Discovery + manifest curation, with no database involved."""
    import textwrap

    from app import datasets as ds

    manifest = os.path.join(_tmp, "datasets.toml")
    os.environ["DATASETS_FILE"] = manifest
    with open(manifest, "w") as f:
        f.write(textwrap.dedent('''
            [settings]
            hide = ["temp_*", "teste_*"]

            [[folder]]
            key = "captura"
            title = "Captura"
            match = ["captura*"]

            [[folder]]
            key = "core"
            title = "Core"
            datasets = ["companies"]

            [[dataset]]
            name = "companies"
            title = "Companies"
            description = "One row per company."

              [[dataset.example]]
              title = "Recent"
              sql = "SELECT * FROM companies LIMIT 10"
        ''').strip())

    from app import config

    config.get_settings.cache_clear()
    _install_catalog_stub(ds)
    try:
        found = ds.list_datasets()
        names = [d.name for d in found]
        assert "temp_scratch" not in names, "hide pattern temp_* not applied"
        assert "teste_pagina1_20260716171556" not in names, "hide pattern teste_* not applied"
        assert names == ["companies", "captura", "cs_healthscore", "aux_task_classification"], names

        companies = ds.get_dataset("companies")
        assert companies.title == "Companies" and companies.documented
        assert companies.folder == "core" and len(companies.examples) == 1

        # reltuples of -1 means "never analyzed" -> unknown, not zero
        assert ds.get_dataset("cs_healthscore").approx_rows is None
        assert ds.get_dataset("captura").approx_rows == 98000

        # A hidden dataset must not be resolvable by name either
        assert ds.get_dataset("temp_scratch") is None, "hidden dataset still reachable"

        grouped = ds.group(found)
        titles = [(f.title, [d.name for d in items]) for f, items in grouped]
        assert titles[0] == ("Captura", ["captura"]), titles
        assert titles[1] == ("Core", ["companies"]), titles
        assert titles[2][0] == "Ungrouped", titles
        assert set(titles[2][1]) == {"cs_healthscore", "aux_task_classification"}
    finally:
        os.environ.pop("DATASETS_FILE", None)
        config.get_settings.cache_clear()
    print("datasets catalog: OK")


def _dataset_rows(response) -> list[str]:
    """The dataset names actually rendered as rows.

    Matching on the row markup, not `in response.text` — names also appear in
    the stylesheet and empty-state snippets, which makes substring checks lie.
    """
    import re

    return re.findall(r'class="bi-dsname">([^<]+)<', response.text)


def test_datasets_pages():
    from app import datasets as ds

    _install_catalog_stub(ds)
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

        r = client.get("/datasets")
        assert r.status_code == 200, r.status_code
        assert _dataset_rows(r) == [
            "companies",
            "captura",
            "cs_healthscore",
            "aux_task_classification",
        ], _dataset_rows(r)
        assert "matview" in r.text, "kind badge missing"

        # search narrows the list
        r = client.get("/datasets?q=captura")
        assert _dataset_rows(r) == ["captura"], _dataset_rows(r)

        # kind filter
        r = client.get("/datasets?kind=view")
        assert _dataset_rows(r) == ["cs_healthscore"], _dataset_rows(r)

        # detail page carries all four sections
        r = client.get("/datasets/companies")
        assert r.status_code == 200, r.status_code
        for needle in ("Preview", "Data catalog", "Example queries", "legal_name"):
            assert needle in r.text, f"missing {needle!r} on the detail page"

        # unknown dataset -> 404, not a crash
        r = client.get("/datasets/does_not_exist", follow_redirects=False)
        assert r.status_code == 404, r.status_code

        # a hidden dataset must not be reachable by direct URL
        r = client.get("/datasets/temp_scratch", follow_redirects=False)
        assert r.status_code == 404, "hidden dataset served by direct URL"

        # datasets require a login like every other page
        client.post("/logout")
        r = client.get("/datasets", follow_redirects=False)
        assert r.status_code == 303, r.status_code
    print("datasets pages: OK")


def test_inline_code_filter():
    """Backticks become <code>, but manifest prose can never inject markup."""
    from app.main import _inline_code

    assert str(_inline_code("grain is `c_id` per day")) == (
        "grain is <code>c_id</code> per day"
    ), str(_inline_code("grain is `c_id` per day"))

    hostile = _inline_code("<script>alert(1)</script> and `x`")
    assert "<script>" not in str(hostile), hostile
    assert "&lt;script&gt;" in str(hostile) and "<code>x</code>" in str(hostile), hostile
    assert str(_inline_code(None)) == ""
    print("inline code filter: OK")


def test_dataset_preview_is_not_injectable():
    """A dataset name only reaches SQL after the catalog vouches for it."""
    from app import datasets as ds

    _install_catalog_stub(ds)
    captured = {}

    def _spy(name, limit):
        captured["name"] = name
        return (["a"], [(1,)])

    ds._fetch_preview = _spy
    # Names the catalog never returned must not reach _fetch_preview at all.
    for hostile in ("companies; DROP TABLE x", '"; DELETE FROM y --', "temp_scratch"):
        assert ds.get_dataset(hostile) is None, f"{hostile!r} resolved"
    assert "name" not in captured, "a query ran for an unknown dataset"

    ds.get_preview("companies")
    assert captured["name"] == "companies"
    print("dataset preview guard: OK")


def test_chart_spec():
    """Shaping rules, including the ones the palette validation obliges."""
    from app import charts as ch

    cols = ["dia", "total", "erros"]
    rows = [("2026-01-01", 10, 1), ("2026-01-02", 20, 3)]

    assert ch.numeric_columns(cols, rows) == ["total", "erros"]

    spec = ch.build_spec(cols, rows, "bar", "dia", ["total", "erros"])
    assert spec.labels == ["2026-01-01", "2026-01-02"], spec.labels
    assert [d["label"] for d in spec.datasets] == ["total", "erros"]
    # hues are assigned by slot, in the validated order
    assert spec.datasets[0]["color"] == ch.SERIES_COLORS[0]
    assert spec.datasets[1]["color"] == ch.SERIES_COLORS[1]
    assert spec.show_legend is True

    # one series names itself in the title -> no legend box
    assert ch.build_spec(cols, rows, "line", "dia", ["total"]).show_legend is False

    # a pie encodes one whole
    pie = ch.build_spec(cols, rows, "pie", "dia", ["total", "erros"])
    assert len(pie.datasets) == 1, pie.datasets
    assert any("one measure" in w for w in pie.warnings), pie.warnings
    assert isinstance(pie.datasets[0]["color"], list), "pie colours vary per slice"

    # unknown columns are reported, not silently charted
    bad = ch.build_spec(cols, rows, "bar", "nope", ["total"])
    assert not bad.datasets and bad.warnings
    dropped = ch.build_spec(cols, rows, "bar", "dia", ["total", "ghost"])
    assert [d["label"] for d in dropped.datasets] == ["total"]
    assert any("ghost" in w for w in dropped.warnings)

    # series are capped rather than cycled — two series must never share a hue
    wide_cols = ["x"] + ["m%d" % i for i in range(12)]
    wide_rows = [tuple(["a"] + list(range(12)))]
    capped = ch.build_spec(wide_cols, wide_rows, "bar", "x", wide_cols[1:])
    assert len(capped.datasets) == ch.MAX_SERIES, len(capped.datasets)
    hues = [d["color"] for d in capped.datasets]
    assert len(set(hues)) == len(hues), "hues were cycled: %r" % hues

    # too many points is called out, not silently smeared
    many = [("d%d" % i, i) for i in range(ch.MAX_POINTS + 25)]
    big = ch.build_spec(["d", "v"], many, "line", "d", ["v"])
    assert len(big.labels) == ch.MAX_POINTS
    assert any("past what a chart can show" in w for w in big.warnings), big.warnings

    # non-numeric text must not become a silent zero
    assert ch.numeric_columns(["a"], [("hello",)]) == []
    print("chart spec: OK")


def test_charts_disabled_without_app_db():
    """With no app database the Charts tab explains itself instead of 500ing."""
    import importlib

    from app import config
    from app import main as main_mod
    from app import store

    saved = os.environ.get("APP_DB_PASSWORD")
    os.environ["APP_DB_PASSWORD"] = ""
    config.get_settings.cache_clear()
    store.reset_engine()
    reloaded = importlib.reload(main_mod)
    try:
        assert not reloaded.store.available()
        with TestClient(reloaded.app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})
            r = client.get("/charts")
            assert r.status_code == 503, r.status_code
            assert "not configured" in r.text
            r = client.get("/charts/new")
            assert r.status_code == 503, r.status_code
    finally:
        if saved is None:
            os.environ.pop("APP_DB_PASSWORD", None)
        else:
            os.environ["APP_DB_PASSWORD"] = saved
        config.get_settings.cache_clear()
        store.reset_engine()
        importlib.reload(main_mod)
    print("charts disabled path: OK")


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
    test_datasets_catalog()
    test_datasets_pages()
    test_inline_code_filter()
    test_dataset_preview_is_not_injectable()
    test_chart_spec()
    # Last: these reload app.main, which rebinds this module's `app` reference.
    test_charts_disabled_without_app_db()
    test_superset_delegated_mode()
    test_superset_break_glass()
    test_sso_only_mode()
    print("\nAll smoke tests passed.")
