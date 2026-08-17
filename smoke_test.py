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
# Blank the app database credentials so the suite can never reach a real one.
# Without this, a developer with a populated .env (and an open tunnel) has their
# tests register users into production: logging in calls _register_login, which
# writes to whatever APP_DB_* points at. Tests that need the app database stub
# `store` functions instead.
os.environ["APP_DB_PASSWORD"] = ""
os.environ["APP_DB_HOST"] = ""
# Point every database setting at a closed local port. Tests that need the app
# database stub store.available() to True, and any *unstubbed* reader then falls
# through to a real engine — which without this resolves the warehouse endpoint
# from .env and makes the suite hang or pass depending on whether a tunnel or
# VPN happens to be up. A refused connection fails in microseconds instead, and
# turns "reached the network" from a flake into a visible error.
os.environ["DB_HOST"] = "127.0.0.1"
os.environ["DB_PORT"] = "1"
os.environ["APP_DB_PORT"] = "1"
# Same for the delegated-auth path: no test should make a real call to Superset.
os.environ["SUPERSET_INTERNAL_URL"] = "http://127.0.0.1:1"

from datetime import UTC
from urllib.parse import urlsplit

import pandas as pd
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import reports, store
from app.main import app

KEYCLOAK_AUTHZ = "https://sso.v360.io/realms/v360/protocol/openid-connect/auth"

# Guard rather than comment: if this ever becomes true, the suite is one login
# away from writing rows into a real report_hub.
assert not store.available(), (
    "the test suite must not have a usable app database — APP_DB_* leaked in "
    "from the environment or .env"
)


def _no_op_user(username: str, **kwargs):
    """Stand-in for store.upsert_user, so a stubbed store.available() can't let
    a login reach a real database."""
    return store.User(id=1, username=username, is_admin=True, roles=["Analytics"])


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
    with TestClient(
        app, base_url="https://testserver"
    ) as client:  # `with` runs lifespan -> bootstrap admin
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
        assert r.status_code == 200 and "Entrar" in r.text  # pt is the default
        assert "Continuar com o SSO V360" in r.text, "SSO button missing from login page"

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

        # The console is reachable and is now *only* the query console —
        # reports moved to their own page.
        r = client.get("/")
        assert r.status_code == 200 and "Conectado como admin" in r.text
        assert 'id="sql"' in r.text, "query editor missing from the console"
        assert 'data-pane="reports"' not in r.text, (
            "the reports pane is still inside the query console"
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
            assert "Conectado como marcelo.ferreira" in r.text, "SSO user not on dashboard"

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
        f.write(
            textwrap.dedent("""
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
        """).strip()
        )

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

    assert str(_inline_code("grain is `c_id` per day")) == ("grain is <code>c_id</code> per day"), (
        str(_inline_code("grain is `c_id` per day"))
    )

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
    """Shaping rules, including the ones the palette validation obliges.

    Pinned to English: this is a test about charts, and asserting on translated
    warning text would make it fail whenever a translation is reworded.
    """
    from app import charts as ch
    from app import i18n

    previous = i18n.get_locale()
    i18n.set_locale("en")
    try:
        _chart_spec_body(ch)
    finally:
        i18n.set_locale(previous)
    print("chart spec: OK")


def _chart_spec_body(ch):
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
    wide_cols = ["x"] + [f"m{i}" for i in range(12)]
    wide_rows = [tuple(["a"] + list(range(12)))]
    capped = ch.build_spec(wide_cols, wide_rows, "bar", "x", wide_cols[1:])
    assert len(capped.datasets) == ch.MAX_SERIES, len(capped.datasets)
    hues = [d["color"] for d in capped.datasets]
    assert len(set(hues)) == len(hues), f"hues were cycled: {hues!r}"

    # too many points is called out, not silently smeared
    many = [(f"d{i}", i) for i in range(ch.MAX_POINTS + 25)]
    big = ch.build_spec(["d", "v"], many, "line", "d", ["v"])
    assert len(big.labels) == ch.MAX_POINTS
    assert any("past what a chart can show" in w for w in big.warnings), big.warnings

    # non-numeric text must not become a silent zero
    assert ch.numeric_columns(["a"], [("hello",)]) == []


def test_dashboard_layout_ordering():
    """move_item swaps neighbours and rewrites positions densely.

    Exercised against a fake connection rather than Postgres: the ordering
    logic is the part that breaks, and it shouldn't need a database to check.
    """
    from app import store

    class _FakeResult:
        def __init__(self, rows=(), scalar=None):
            self._rows = list(rows)
            self._scalar = scalar
            self.rowcount = len(self._rows)

        def __iter__(self):
            return iter(self._rows)

        def scalar(self):
            return self._scalar

        def first(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

    class _FakeConn:
        """Answers the three queries move_item issues, and records updates."""

        def __init__(self, item_ids):
            self.item_ids = item_ids
            self.writes = []

        def execute(self, statement, params=None):
            sql = str(statement)
            if "SELECT id FROM dashboards" in sql:
                return _FakeResult(scalar=7)
            if "SELECT id FROM dashboard_items" in sql:
                return _FakeResult(rows=[(i,) for i in self.item_ids])
            if "UPDATE dashboard_items SET position" in sql:
                self.writes.append((params["i"], params["p"]))
                return _FakeResult()
            return _FakeResult()  # the updated_at touch

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _run(item_ids, item_id, delta):
        conn = _FakeConn(item_ids)

        class _Eng:
            def begin(self_inner):
                return conn

        original_engine = store.engine
        store.engine = lambda: _Eng()
        try:
            ok = store.move_item("dash", item_id, delta)
        finally:
            store.engine = original_engine
        return ok, dict(conn.writes)

    ok, positions = _run([10, 20, 30], 20, -1)
    assert ok, "moving the middle tile up should succeed"
    # 20 swaps with 10 -> order 20, 10, 30 written densely from zero
    assert positions == {20: 0, 10: 1, 30: 2}, positions

    ok, positions = _run([10, 20, 30], 20, 1)
    assert ok
    assert positions == {10: 0, 30: 1, 20: 2}, positions

    # Edges refuse rather than wrapping around
    ok, positions = _run([10, 20, 30], 10, -1)
    assert not ok and positions == {}, positions
    ok, positions = _run([10, 20, 30], 30, 1)
    assert not ok and positions == {}, positions

    # An unknown tile is rejected, not silently reordered
    ok, _ = _run([10, 20], 99, -1)
    assert not ok

    # Duplicate/gapped positions still normalise to 0..n-1
    ok, positions = _run([5, 6, 7, 8], 7, -1)
    assert sorted(positions.values()) == [0, 1, 2, 3], positions
    print("dashboard layout ordering: OK")


def test_dashboard_widths_are_validated():
    """Only known widths reach the database — the grid spans depend on it."""
    from app import store

    assert store.DEFAULT_WIDTH in store.WIDTHS
    assert set(store.WIDTHS) == {"third", "half", "full"}
    # set_item_width rejects anything else before touching a connection
    assert store.set_item_width("d", 1, "enormous") is False
    print("dashboard widths: OK")


def test_dashboard_pages_render():
    """Actually render the dashboard view and editor.

    This is the test that catches template-level breakage. Parsing a template —
    which is all the CI `templates` job does — never executes its macros, so a
    macro imported without `with context` parses fine and then fails at render
    time with KeyError: 'request'. That happened; this is the guard.
    """
    from app import db as db_mod
    from app import store

    chart = store.Chart(
        id=1,
        slug="cap",
        title="Capturas por dia",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="line",
        x_column="dia",
        y_columns=["total"],
    )
    dash = store.Dashboard(id=1, slug="ops", title="Operação diária", created_by="admin")
    dash.items = [store.DashboardItem(id=11, chart=chart, position=0, width="full")]

    saved = (
        store.available,
        store.get_dashboard,
        store.list_charts,
        store.list_dashboards,
        db_mod.execute,
    )
    store.available = lambda: True
    store.get_dashboard = lambda slug, with_items=True: dash if slug == "ops" else None
    store.list_charts = lambda: [chart]
    store.list_dashboards = lambda: [dash]
    db_mod.execute = lambda sql, database=None, max_rows=None, params=None: db_mod.QueryResult(
        returns_rows=True,
        columns=["dia", "total"],
        rows=[("2026-07-01", 10), ("2026-07-02", 14)],
        rowcount=2,
    )
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            r = client.get("/dashboards")
            assert r.status_code == 200, r.text[:300]
            assert "Operação diária" in r.text

            # The page itself runs no queries: it ships tile shells, and each
            # tile fetches its own data. That's what makes a big dashboard
            # appear immediately instead of after every query on it.
            ran = []
            real_execute = db_mod.execute

            def _counting(sql, database=None, max_rows=None, params=None):
                ran.append(sql)
                return real_execute(sql, database, max_rows, params)

            db_mod.execute = _counting
            r = client.get("/dashboards/ops")
            assert r.status_code == 200, r.text[:400]
            assert "Capturas por dia" in r.text
            assert 'data-tile="11"' in r.text, "tile shell missing"
            assert "bi-tile-skel" in r.text, "no skeleton while the tile loads"
            assert not ran, f"the dashboard page ran {len(ran)} quer(ies) before painting"

            # …and the tile endpoint is what actually runs it.
            r = client.get("/dashboards/ops/tiles/11")
            assert r.status_code == 200, r.text[:300]
            body = r.json()
            assert body["renders_as"] == "canvas", body
            assert body["spec"]["labels"] == ["2026-07-01", "2026-07-02"], body["spec"]
            assert ran, "the tile endpoint didn't run the chart's query"
            db_mod.execute = real_execute

            # A tile is a real endpoint, so it re-checks access for itself.
            r = client.get("/dashboards/ops/tiles/999")
            assert r.status_code == 404, r.status_code

            r = client.get("/dashboards/ops/edit")
            assert r.status_code == 200, r.text[:400]
            assert "/items/11/remove" in r.text, "remove action missing"
            assert "bi-drag" in r.text, "no drag handle in the editor"
            assert "dashboard-layout.js" in r.text, "layout editor not loaded"

            r = client.get("/dashboards/nope", follow_redirects=False)
            assert r.status_code == 404, r.status_code
    finally:
        (
            store.available,
            store.get_dashboard,
            store.list_charts,
            store.list_dashboards,
            db_mod.execute,
        ) = saved
    print("dashboard pages render: OK")


def test_tags_and_listing_views():
    """Tags label and filter; they never grant. Plus the three view modes.

    Same rule as folders, and worth pinning for the same reason: the moment a
    label decides who sees something, "how do I find this" and "may I read it"
    become one question.
    """
    from app import main as main_mod
    from app import store

    assert "tag" not in store.RESOURCE_TYPES, (
        "tag became a permission resource type — tagging would start granting"
    )

    chart_a = store.Chart(
        id=1,
        slug="vendas",
        title="Vendas",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="bar",
        x_column="a",
        y_columns=["b"],
    )
    chart_b = store.Chart(
        id=2,
        slug="custos",
        title="Custos",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="line",
        x_column="a",
        y_columns=["b"],
    )
    tagged = {"vendas": [store.Tag(id=1, name="Financeiro", slug="financeiro")]}

    saved = (
        store.available,
        store.list_charts,
        store.list_tags,
        store.tags_for,
        store.keys_with_tag,
        store.set_tags,
        store.upsert_user,
        store.get_chart,
        store.slug_for,
    )
    calls = {}
    # URLs carry ids; the handler works in slugs. Without this the resolver
    # falls back to the raw id and the assertion below reads "1", not "vendas".
    store.slug_for = lambda table, ident: (
        {"1": "vendas", "2": "custos"}.get(str(ident)) if str(ident).isdigit() else str(ident)
    )
    store.available = lambda: True
    store.list_charts = lambda: [chart_a, chart_b]
    # Called two ways: with a type for the filter bar, without for the picker's
    # whole vocabulary. "Reservado" is defined but carried by nothing, so it
    # must reach the picker and stay out of the bar.
    store.list_tags = lambda rt=None: (
        [store.Tag(id=1, name="Financeiro", slug="financeiro", count=1)]
        if rt
        else [
            store.Tag(id=1, name="Financeiro", slug="financeiro", count=1, chart_count=1),
            store.Tag(id=2, name="Reservado", slug="reservado"),
        ]
    )
    store.tags_for = lambda rt, keys: {k: v for k, v in tagged.items() if k in keys}
    store.keys_with_tag = lambda rt, slug: ["vendas"] if slug == "financeiro" else []
    store.set_tags = lambda rt, key, names: (
        calls.__setitem__("set", (rt, key, [n.strip() for n in names if n.strip()])) or True
    )
    store.get_chart = lambda s: {"vendas": chart_a, "custos": chart_b}.get(s)
    store.upsert_user = _no_op_user
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            # Default view is the card; the switcher offers all three.
            body = client.get("/charts").text
            assert "bi-viewsw" in body, "no view switcher"
            for name in main_mod.VIEWS:
                assert f"view={name}" in body, f"{name} view missing from the switcher"
            assert "Financeiro" in body, "tags not shown on the listing"

            # Markup, not class names: every class also appears in the inlined
            # stylesheet, so `"bi-listtbl" in body` is true on every page.
            LIST_MARKUP = '<table class="bi-restbl bi-listtbl">'
            body = client.get("/charts?view=list").text
            assert LIST_MARKUP in body, "list view did not render a table"
            assert 'data-chart="1"' not in body, "list view should not fetch previews"

            body = client.get("/charts?view=preview").text
            assert 'data-chart="1"' in body, "preview cards don't fetch"
            assert "chart-preview.js" in body
            assert LIST_MARKUP not in body

            # The choice is remembered, so browsing doesn't reset it.
            remembered = client.get("/charts").text
            assert 'data-chart="1"' in remembered, "the view choice did not stick"
            assert LIST_MARKUP not in remembered

            # Filtering by tag narrows the list and nothing else.
            body = client.get("/charts?view=box&tag=financeiro").text
            assert "Vendas" in body and "Custos" not in body, "tag filter did not narrow"

            # Retagging is free text, and lands as a list of names. More than
            # one tag per item is the whole point of tags being many-to-many —
            # a chart is "financeiro" *and* "mensal".
            r = client.post(
                "/charts/1/tags", data={"tags": "Financeiro, Mensal , "}, follow_redirects=False
            )
            assert r.status_code == 303, r.status_code
            assert calls["set"] == ("chart", "vendas", ["Financeiro", "Mensal"]), calls["set"]

            # …and the editor has to *say* so. It was a single pre-filled text
            # box: once an item had one tag the placeholder was hidden and
            # nothing hinted a second was allowed, so in practice nothing ever
            # carried two. Picking from a list is what fixes that.
            body = client.get("/charts?view=box").text
            assert "tag-editor.js" in body, "the tag picker is not loaded"
            assert 'id="bi-tagvocab"' in body, "no vocabulary for the picker"
            assert '"Reservado"' in body, (
                "a tag defined on /admin/tags but carried by nothing is missing from "
                "the picker — there would then be no way to apply it at all"
            )
            assert body.count('id="bi-tagvocab"') == 1, (
                "one copy of the vocabulary per card: on a 580-chart listing that is "
                "a megabyte of markup repeating the same few words"
            )
            # The bar is still per-type, so an unused tag isn't offered as a
            # filter that would return nothing.
            bar = body.split('id="bi-tagvocab"')[0]
            assert "Reservado" not in bar.split('class="bi-tagbar"')[-1].split("</div>")[0]

            # The preview endpoint returns a spec, not a page.
            from app import db as db_mod

            real = db_mod.execute
            db_mod.execute = lambda sql, database=None, max_rows=None, params=None: (
                db_mod.QueryResult(
                    returns_rows=True, columns=["a", "b"], rows=[("x", 1)], rowcount=1
                )
            )
            try:
                payload = client.get("/charts/1/data").json()
                assert payload["renders_as"] == "canvas", payload
                assert payload["spec"]["labels"] == ["x"], payload
            finally:
                db_mod.execute = real
    finally:
        (
            store.available,
            store.list_charts,
            store.list_tags,
            store.tags_for,
            store.keys_with_tag,
            store.set_tags,
            store.upsert_user,
            store.get_chart,
            store.slug_for,
        ) = saved
    print("tags and listing views: OK")


def test_preview_cache():
    """Previews come from a cache, carry their age, and never outlive an edit.

    A chart's numbers move when the *data* moves, not when someone edits the
    chart — so a cached spec is a likeness, not a reading, and every response
    says how old it is.
    """
    from datetime import datetime, timedelta

    from app import db as db_mod
    from app import store

    chart = store.Chart(
        id=7,
        slug="vendas",
        title="Vendas",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="bar",
        x_column="a",
        y_columns=["b"],
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cache: dict = {}
    ran = []

    saved = (
        store.available,
        store.get_chart,
        store.slug_for,
        store.upsert_user,
        store.get_chart_preview,
        store.put_chart_preview,
        store.drop_chart_preview,
        db_mod.execute,
    )
    store.available = lambda: True
    store.get_chart = lambda s: chart if s == "vendas" else None
    store.slug_for = lambda table, ident: "vendas" if str(ident) == "7" else str(ident)
    store.upsert_user = _no_op_user
    store.get_chart_preview = lambda cid: cache.get(cid)
    store.put_chart_preview = lambda cid, spec: cache.__setitem__(cid, (spec, datetime.now(UTC)))
    store.drop_chart_preview = lambda cid: cache.pop(cid, None)

    def _execute(sql, database=None, max_rows=None, params=None):
        ran.append(sql)
        return db_mod.QueryResult(
            returns_rows=True, columns=["a", "b"], rows=[("x", 1)], rowcount=1
        )

    db_mod.execute = _execute
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            # Cold: runs the query, caches it, and reports itself as fresh.
            first = client.get("/charts/7/data").json()
            assert first["cached"] is False and first["age"], first
            assert len(ran) == 1, ran

            # Warm: no query at all.
            second = client.get("/charts/7/data").json()
            assert second["cached"] is True, second
            assert len(ran) == 1, "the cached preview still hit the warehouse"
            assert second["spec"] == first["spec"]

            # ?refresh=1 forces it.
            client.get("/charts/7/data?refresh=1")
            assert len(ran) == 2, "refresh did not re-run the query"

            # Older than the TTL: rebuilt.
            spec, _ = cache[7]
            cache[7] = (spec, datetime.now(UTC) - timedelta(hours=48))
            client.get("/charts/7/data")
            assert len(ran) == 3, "a stale preview was served"

            # Edited since the snapshot: rebuilt, because the query moved.
            spec, _ = cache[7]
            cache[7] = (spec, datetime.now(UTC))
            chart.updated_at = datetime.now(UTC) + timedelta(minutes=1)
            client.get("/charts/7/data")
            assert len(ran) == 4, "a preview of the pre-edit query was served"

            # An empty result is served but never kept. An ETL that truncates
            # and reloads leaves its tables empty for a minute; caching that
            # pins "no data" on the card long after the data is back.
            chart.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
            cache.clear()
            empty = db_mod.QueryResult(
                returns_rows=True, columns=["a", "b"], rows=[], rowcount=0
            )
            db_mod.execute = lambda sql, database=None, max_rows=None, params=None: (
                ran.append(sql) or empty
            )
            body = client.get("/charts/7/data").json()
            assert not body.get("spec", {}).get("labels"), body
            assert 7 not in cache, "an empty preview was cached"
            client.get("/charts/7/data")
            assert len(ran) == 6, "the empty preview was served from a cache"
    finally:
        (
            store.available,
            store.get_chart,
            store.slug_for,
            store.upsert_user,
            store.get_chart_preview,
            store.put_chart_preview,
            store.drop_chart_preview,
            db_mod.execute,
        ) = saved
    print("preview cache: OK")


def test_interface_language():
    """Portuguese by default, English by choice, and the choice sticks."""
    from app import i18n, store

    # ── the translator itself ──
    previous = i18n.get_locale()
    try:
        i18n.set_locale("pt")
        assert i18n.t("Charts") == "Gráficos"
        i18n.set_locale("en")
        assert i18n.t("Charts") == "Charts", "English must return the source string"
        # A string with no translation falls back rather than blowing up, so a
        # missed one shows as English instead of an empty label.
        i18n.set_locale("pt")
        assert i18n.t("Not translated yet") == "Not translated yet"
        assert i18n.t("Signed in as {user}", user="ana") == "Conectado como ana"
        # An unknown code is the default, not a crash.
        assert i18n.set_locale("klingon") == i18n.DEFAULT == "pt"
    finally:
        i18n.set_locale(previous)

    # ── through the app ──
    saved = (store.available, store.upsert_user, store.set_user_locale, store.get_user_locale)
    stored = {}
    store.available = lambda: True
    store.upsert_user = _no_op_user
    store.set_user_locale = lambda u, code: stored.__setitem__(u, code) or True
    store.get_user_locale = lambda u: stored.get(u)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            # The login page is Portuguese before anyone has chosen anything.
            body = client.get("/login/local").text
            assert "Entrar" in body and "Log in" not in body, "login page is not pt by default"

            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})
            body = client.get("/").text
            assert "<span>Consulta</span>" in body, "nav is not translated"
            assert "Conectado como admin" in body

            # ?lang= wins for one request, so a link can carry a language.
            body = client.get("/?lang=en").text
            assert "<span>Query</span>" in body, "?lang=en did not switch"

            # The switcher persists it: session for now, user row for next time.
            r = client.post("/lang/en", follow_redirects=False)
            assert r.status_code == 303, r.status_code
            assert stored.get("admin") == "en", stored
            body = client.get("/").text
            assert "<span>Query</span>" in body, "the choice did not stick"
            assert "Signed in as admin" in body

            # And back again.
            client.post("/lang/pt", follow_redirects=False)
            assert client.get("/").text.count("<span>Consulta</span>") == 1
            assert stored.get("admin") == "pt"

            # A nonsense code falls back rather than 500ing or blanking the UI.
            client.post("/lang/klingon", follow_redirects=False)
            assert "<span>Consulta</span>" in client.get("/").text
    finally:
        (store.available, store.upsert_user, store.set_user_locale, store.get_user_locale) = saved
    print("interface language: OK")


def test_urls_address_charts_and_dashboards_by_id():
    """URLs use ids; slugs still resolve so old links keep working.

    A slug comes from the title, so renaming a chart used to break every link
    and bookmark pointing at it. Permissions still speak in slugs — a grant
    reading `capturas-por-dia` is auditable, `417` is not — so the id only
    exists at the URL layer.
    """
    from app import db as db_mod
    from app import store

    chart = store.Chart(
        id=7,
        slug="capturas-por-dia",
        title="Capturas por dia",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="line",
        x_column="dia",
        y_columns=["total"],
    )
    dash = store.Dashboard(id=3, slug="ops", title="Operação", created_by="admin")
    dash.items = [store.DashboardItem(id=11, chart=chart, position=0, width="full")]

    saved = (
        store.available,
        store.get_dashboard,
        store.get_chart,
        store.list_charts,
        store.list_dashboards,
        store.list_filters,
        store.slug_for,
        store.id_for,
        store.upsert_user,
        db_mod.execute,
    )
    store.available = lambda: True
    store.get_dashboard = lambda s, with_items=True: dash if s == "ops" else None
    store.get_chart = lambda s: chart if s == "capturas-por-dia" else None
    store.list_charts = lambda: [chart]
    store.list_dashboards = lambda: [dash]
    store.list_filters = lambda s: []
    store.upsert_user = _no_op_user
    # The resolver: ids map to slugs, unknown ids do not.
    ids = {("charts", "7"): "capturas-por-dia", ("dashboards", "3"): "ops"}
    store.slug_for = lambda table, ident: (
        ids.get((table, str(ident))) if str(ident).isdigit() else str(ident)
    )
    store.id_for = lambda table, slug: {"ops": 3, "capturas-por-dia": 7}.get(slug)
    db_mod.execute = lambda sql, database=None, max_rows=None, params=None: db_mod.QueryResult(
        returns_rows=True, columns=["dia", "total"], rows=[("2026-07-01", 10)], rowcount=1
    )
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            # The id is the address.
            assert client.get("/dashboards/3").status_code == 200
            assert client.get("/charts/7").status_code == 200
            assert client.get("/dashboards/3/tiles/11").status_code == 200

            # Links the pages emit are id-shaped, not slug-shaped.
            listing = client.get("/dashboards").text
            assert "/dashboards/3" in listing, "the index still links by slug"
            assert "/dashboards/ops" not in listing, listing[:200]
            charts_page = client.get("/charts").text
            assert "/charts/7" in charts_page and "/charts/capturas-por-dia" not in charts_page

            # A link shared before the switch still resolves.
            assert client.get("/dashboards/ops").status_code == 200
            assert client.get("/charts/capturas-por-dia").status_code == 200

            # An id that doesn't exist is a 404, not a slug lookup that misses.
            assert client.get("/dashboards/9999").status_code == 404
            assert client.get("/charts/9999").status_code == 404
    finally:
        (
            store.available,
            store.get_dashboard,
            store.get_chart,
            store.list_charts,
            store.list_dashboards,
            store.list_filters,
            store.slug_for,
            store.id_for,
            store.upsert_user,
            db_mod.execute,
        ) = saved
    print("urls address charts and dashboards by id: OK")


def test_dashboard_filters_bind_values():
    """Filters reach the SQL as bound parameters, never as text.

    The column is configuration (an editor typed it) and goes into the query;
    the value is input (a viewer typed it) and must not. This is the one place
    in the app where a mistake is an injection hole, so it gets its own test.
    """
    from app import filters as f

    sql = "SELECT c_id, SUM(v) FROM t WHERE 1=1 {{ filters }} GROUP BY 1"

    # Nothing chosen: the token disappears and the query is unchanged otherwise.
    out, params = f.apply(sql, f.resolve([], {}), "chart-a")
    assert "{{" not in out and params == {}, (out, params)
    assert "WHERE 1=1  GROUP BY 1" in out.replace("\n", " "), out

    class D:
        def __init__(self, **kw):
            self.__dict__.update(
                {
                    "key": "",
                    "label": "",
                    "filter_type": "select",
                    "column_expr": "",
                    "values_sql": "",
                    "source_db": "",
                    "default_value": "",
                    "applies_to": [],
                }
            )
            self.__dict__.update(kw)

    defs = [
        D(key="cliente", label="Cliente", filter_type="select", column_expr="c_id"),
        D(key="janela", label="Janela", filter_type="daterange", column_expr="d"),
        D(key="busca", label="Busca", filter_type="text", column_expr="nome"),
    ]
    nasty = "x'; DROP TABLE charts; --"
    active = f.resolve(
        defs, {"cliente": [nasty, "acme"], "janela": ["2026-01-01", "2026-02-01"], "busca": ["ana"]}
    )
    out, params = f.apply(sql, active, "chart-a")

    # The dangerous string is a *value*, and appears nowhere in the SQL text.
    assert nasty not in out, "a filter value was interpolated into the SQL"
    assert "DROP TABLE" not in out
    assert nasty in params.values(), "the value never made it to the parameters"
    assert out.count(":flt_cliente_") == 2, out
    assert ":flt_janela_from" in out and ":flt_janela_to" in out, out
    assert params["flt_busca"] == "%ana%", params
    # The column, which is configuration, does appear.
    assert "c_id IN (" in out and "nome ILIKE" in out, out

    # Scope: a filter naming specific charts leaves the others alone.
    scoped = f.resolve(
        [D(key="cliente", filter_type="select", column_expr="c_id", applies_to=["chart-a"])],
        {"cliente": ["acme"]},
    )
    hit, hit_params = f.apply(sql, scoped, "chart-a")
    miss, miss_params = f.apply(sql, scoped, "chart-b")
    assert "c_id IN" in hit and hit_params
    assert "c_id IN" not in miss and miss_params == {}, miss

    # A chart with no token is left exactly as it was.
    plain = "SELECT 1"
    same, no_params = f.apply(plain, active, "chart-a")
    assert same == plain and no_params == {}
    assert not f.accepts_filters(plain) and f.accepts_filters(sql)

    # Bad dates are dropped rather than passed through.
    bad = f.resolve(
        [D(key="janela", filter_type="daterange", column_expr="d")], {"janela": ["not-a-date", ""]}
    )
    out2, params2 = f.apply(sql, bad, "chart-a")
    assert params2 == {} and "flt_janela" not in out2, (out2, params2)

    assert f.valid_key("cliente_2") and not f.valid_key("Cliente") and not f.valid_key("a b")
    print("dashboard filters bind values: OK")


def test_list_pages_render_without_folder_ui():
    """The three list pages render, and show no sign that folders exist.

    Folders are built but deliberately unwired (see main.py's folders section),
    so a filed item must look exactly like an unfiled one. This renders for real
    rather than parsing, which is the only way to catch a template that imports
    the folder macros again — and asserts the absence, so switching the UI back
    on is always a deliberate act with a failing test to acknowledge.
    """
    from app import store

    fin = store.Folder(id=1, name="Financeiro", slug="financeiro", description="Faturamento.")
    chart = store.Chart(
        id=1,
        slug="vendas",
        title="Vendas por dia",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="line",
        x_column="dia",
        y_columns=["total"],
        folder_id=1,
    )
    loose = store.Chart(
        id=2,
        slug="avulso",
        title="Gráfico avulso",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="bar",
        x_column="a",
    )
    dash = store.Dashboard(id=1, slug="ops", title="Operação", folder_id=1)

    saved = (
        store.available,
        store.list_charts,
        store.list_dashboards,
        store.list_folders,
        store.list_reports,
        store.list_users,
        store.list_roles,
        store.folder_counts,
        store.is_admin,
        store.upsert_user,
        store.set_user_admin,
    )
    store.available = lambda: True
    store.list_charts = lambda: [chart, loose]
    store.list_dashboards = lambda: [dash]
    store.list_folders = lambda: [fin]
    # Every reader the four pages below touch: available() is stubbed True, so
    # an unstubbed one reaches a real engine instead of rendering.
    store.list_reports = lambda: []
    store.list_users = lambda: [
        store.User(id=1, username="admin", email="a@v360.io", is_admin=True, roles=[])
    ]
    store.list_roles = lambda: []
    store.folder_counts = lambda: {1: 2}
    store.is_admin = lambda u: True
    store.upsert_user = _no_op_user
    store.set_user_admin = lambda u, v: True
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            # Both charts are listed — the filed one and the unfiled one — and
            # nothing distinguishes them.
            r = client.get("/charts")
            assert r.status_code == 200, r.text[:400]
            assert "Vendas por dia" in r.text and "Gráfico avulso" in r.text

            r = client.get("/dashboards")
            assert r.status_code == 200, r.text[:400]
            assert "Operação" in r.text

            r = client.get("/reports")
            assert r.status_code == 200, r.text[:400]

            # Markup only. The folder CSS still ships in the shared stylesheet
            # — it's parked with the rest of the feature — so asserting on class
            # names would match base.html rather than anything rendered.
            for path in ("/charts", "/dashboards", "/reports", "/admin/users"):
                body = client.get(path).text
                for trace in (
                    'name="folder_id"',  # the "move to folder" select
                    "/folders/file",  # the form it posts to
                    ">Ungrouped<",  # the leftovers heading, as rendered
                    "Financeiro",  # this fixture's folder name
                    "/admin/folders",  # a link to the folders screen
                ):
                    assert trace not in body, f"folder UI is back on {path}: {trace!r}"
    finally:
        (
            store.available,
            store.list_charts,
            store.list_dashboards,
            store.list_folders,
            store.list_reports,
            store.list_users,
            store.list_roles,
            store.folder_counts,
            store.is_admin,
            store.upsert_user,
            store.set_user_admin,
        ) = saved
    print("list pages render, no folder UI: OK")


def test_folders_are_organisation_only():
    """A folder groups the list pages and can never change who sees what.

    This is the property the whole design rests on, and an earlier version got
    it wrong: folders were a permission bundle, and "where does this live" and
    "who may read it" became the same question. So it is pinned three ways —
    the vocabulary, the resolver, and the writer.
    """
    from app import main, store

    # 1. Not part of the permission vocabulary at all. If `folder` ever becomes
    #    a resource type, it becomes grantable on the roles screen.
    assert "folder" not in store.RESOURCE_TYPES, (
        "folder is a resource type again — it would be grantable, and grouping "
        "would start deciding access"
    )
    assert not hasattr(store, "FOLDERABLE"), "the permission-era folder API is back"

    # 2. access_for() must never read a folder. Rather than trust a reading of
    #    the code, watch every statement it executes against a fake connection.
    seen: list[str] = []

    class _R:
        def __init__(self, mapping=None, rows=()):
            self._mapping, self._rows = mapping, list(rows)

        def first(self):
            return self if self._mapping is not None else None

        def __iter__(self):
            return iter(self._rows)

    class _Conn:
        def execute(self, statement, params=None):
            sql = str(statement)
            seen.append(sql)
            if "is_admin, is_active FROM users" in sql:
                return _R(mapping={"id": 2, "is_admin": False, "is_active": True})
            if "role_permissions" in sql:
                return _R(rows=[("chart", "vendas")])
            return _R()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    original = store.engine
    store.engine = lambda: type("E", (), {"connect": staticmethod(lambda: _Conn())})()
    try:
        access = store.access_for("ana")
    finally:
        store.engine = original

    assert access.allows(store.CHART, "vendas")
    assert not any("folder" in sql.lower() for sql in seen), (
        f"access_for touched folders: {[s for s in seen if 'folder' in s.lower()]}"
    )

    # 3. Grouping runs on an already-filtered list, so it can only ever reorder
    #    what the caller decided to show. An item whose folder is unknown falls
    #    into the ungrouped bucket rather than disappearing.
    fin = store.Folder(id=1, name="Financeiro", slug="financeiro", position=0)
    ops = store.Folder(id=2, name="Operações", slug="operacoes", position=1)
    empty = store.Folder(id=3, name="Vazia", slug="vazia", position=2)
    items = [
        store.Chart(
            id=i,
            slug=slug,
            title=slug,
            source_db="analytics",
            sql="SELECT 1",
            chart_type="bar",
            x_column="a",
            folder_id=fid,
        )
        for i, (slug, fid) in enumerate(
            [("a", 1), ("b", None), ("c", 2), ("d", 999)],
            start=1,
        )
    ]
    groups = main._grouped(items, [fin, ops, empty])
    shown = [(f.name if f else None, [c.slug for c in group]) for f, group in groups]
    assert shown == [("Financeiro", ["a"]), ("Operações", ["c"]), (None, ["b", "d"])], shown
    # Nothing is lost or duplicated by grouping — the whole point.
    assert sorted(c.slug for _, g in groups for c in g) == ["a", "b", "c", "d"]

    # 4. Losing the folders entirely must cost the headings and nothing else.
    #    _folders() fails open to [], so this is the shape of a real outage:
    #    charts readable, folders not. Every chart still has to be listed.
    blind = main._grouped(items, [])
    assert blind == [(None, items)], "a folder read failure hid filed content"
    print("folders are organisation only: OK")


def test_admin_panel_and_gate():
    """The admin screens, and the guarantee that the gate is checked live."""
    from app import db as db_mod
    from app import store

    people = [
        store.User(
            id=1,
            username="marcelo.ferreira",
            email="m@v360.io",
            is_admin=True,
            auth_via="superset",
            roles=["Analytics"],
        ),
        store.User(id=2, username="ana.silva", email="a@v360.io", auth_via="superset", roles=[]),
    ]
    roles = [
        store.Role(
            id=1,
            name="Analytics",
            is_default=True,
            permissions={store.DATABASE: ["analytics"]},
            member_count=1,
        ),
        store.Role(
            id=2,
            name="Finance",
            permissions={store.DATABASE: ["analytics", "dw_v360"]},
            member_count=0,
        ),
    ]
    admin_flag = {"value": True}
    calls = {"set_roles": None, "set_perms": None}

    saved = (
        store.available,
        store.is_admin,
        store.list_users,
        store.list_roles,
        store.set_user_roles,
        store.set_role_permissions,
        db_mod.list_databases,
        store.upsert_user,
        store.set_user_admin,
    )
    store.available = lambda: True
    # Logging in below runs _register_login. Without stubbing these, a stubbed
    # available() would let the test write rows to a real report_hub.
    store.upsert_user = _no_op_user
    store.set_user_admin = lambda u, v: True
    store.is_admin = lambda u: admin_flag["value"] and u == "admin"
    store.list_users = lambda: list(people)
    store.list_roles = lambda: list(roles)
    store.set_user_roles = lambda u, r: calls.__setitem__("set_roles", (u, list(r))) or True
    store.set_role_permissions = lambda i, t, k: (
        calls.__setitem__("set_perms", (i, t, list(k))) or True
    )
    db_mod.list_databases = lambda: ["analytics", "dw_v360", "keycloak"]
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            r = client.get("/admin/users")
            assert r.status_code == 200, r.text[:300]
            assert "marcelo.ferreira" in r.text and "ana.silva" in r.text
            # The list is read-only now: it shows the roles a user *has*, and
            # nothing else. "Finance" has no members, so it must not appear.
            assert "Analytics" in r.text, "assigned role missing from the list"
            assert "Finance" not in r.text, (
                "the users list is still rendering every role — editing belongs "
                "on the user's own page"
            )
            assert "/admin/users/ana.silva" in r.text, "no link to the user page"

            r = client.get("/admin/roles")
            assert r.status_code == 200, r.text[:300]
            # grants are picked from the real instance list, not typed
            assert 'value="keycloak"' in r.text, "database checkboxes missing"
            assert "default" in r.text

            # posting the full set replaces a user's roles
            r = client.post(
                "/admin/users/ana.silva/roles", data={"roles": ["Finance"]}, follow_redirects=False
            )
            assert r.status_code == 303
            assert calls["set_roles"] == ("ana.silva", ["Finance"]), calls["set_roles"]

            r = client.post(
                "/admin/roles/2/permissions",
                data={"resource_type": "database", "keys": ["analytics", "dw_v360"]},
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert calls["set_perms"] == (2, "database", ["analytics", "dw_v360"]), calls[
                "set_perms"
            ]

            # /admin redirects into the users tab
            r = client.get("/admin", follow_redirects=False)
            assert r.status_code == 303 and "/admin/users" in r.headers["location"]

            # Revoking admin mid-session must take effect at once — the gate
            # reads the database, it does not trust session['is_admin'].
            admin_flag["value"] = False
            for path in ("/admin/users", "/admin/roles", "/admin"):
                r = client.get(path, follow_redirects=False)
                assert r.status_code == 403, f"{path} -> {r.status_code}, stale session honoured"
            r = client.post(
                "/admin/roles/2/permissions",
                data={"resource_type": "database", "keys": []},
                follow_redirects=False,
            )
            assert r.status_code == 403, "write route not gated"
    finally:
        (
            store.available,
            store.is_admin,
            store.list_users,
            store.list_roles,
            store.set_user_roles,
            store.set_role_permissions,
            db_mod.list_databases,
            store.upsert_user,
            store.set_user_admin,
        ) = saved
    print("admin panel + gate: OK")


def test_tag_management():
    """Creating and deleting tags, and the sweep that must never come back.

    Tagging stays free-text on the listings — this screen is for defining the
    vocabulary up front and for cleaning it up, which is admin-only because a
    delete reaches across everybody's screens at once.
    """
    import inspect

    from app import store

    # A tag nobody uses yet: it belongs on this screen (that's the point of
    # being able to create one) even though the listing filter bars skip it.
    tags = [
        store.Tag(id=1, name="Financeiro", slug="financeiro", count=14, chart_count=12,
                  dashboard_count=2),
        store.Tag(id=2, name="Diretoria", slug="diretoria", count=0),
    ]
    admin_flag = {"value": True}
    calls: dict = {"created": None, "deleted": None}

    saved = (
        store.available,
        store.is_admin,
        store.list_tags,
        store.create_tag,
        store.delete_tag,
        store.list_roles,
        store.upsert_user,
        store.set_user_admin,
    )
    store.available = lambda: True
    store.upsert_user = _no_op_user
    store.set_user_admin = lambda u, v: True
    store.is_admin = lambda u: admin_flag["value"] and u == "admin"
    store.list_roles = list  # the roles page, only to prove it links here
    store.list_tags = lambda resource_type=None: list(tags)
    store.create_tag = lambda name, created_by="": (
        calls.__setitem__("created", (name, created_by))
        or (None if name.strip().lower() == "financeiro" else store.Tag(id=3, name=name, slug="x"))
    )
    store.delete_tag = lambda slug: calls.__setitem__("deleted", slug) or True
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            r = client.get("/admin/tags")
            assert r.status_code == 200, r.text[:300]
            assert "Financeiro" in r.text and "Diretoria" in r.text, "tags not listed"
            # The split counts are what make a delete an informed decision.
            assert ">12<" in r.text and ">2<" in r.text, "per-type usage counts missing"
            assert "/admin/tags/financeiro/delete" in r.text, "no delete action"

            # Reachable from the rest of admin. The folders screen was built,
            # worked, and was linked from nowhere — so it may as well not have
            # existed. A tab on a sibling page is what makes this real.
            assert "/admin/tags" in client.get("/admin/roles").text, (
                "nothing links to /admin/tags — the screen is orphaned"
            )

            r = client.post(
                "/admin/tags", data={"name": "  Operação  "}, follow_redirects=False
            )
            assert r.status_code == 303
            assert calls["created"] == ("  Operação  ", "admin"), calls["created"]

            # A name already taken is not an error: the wanted end state holds.
            r = client.post("/admin/tags", data={"name": "Financeiro"}, follow_redirects=False)
            assert r.status_code == 303, "a duplicate name blew up instead of no-opping"

            r = client.post("/admin/tags/financeiro/delete", follow_redirects=False)
            assert r.status_code == 303
            assert calls["deleted"] == "financeiro", calls["deleted"]

            # Same live gate as the rest of admin: the session is not trusted.
            admin_flag["value"] = False
            assert client.get("/admin/tags", follow_redirects=False).status_code == 403
            r = client.post("/admin/tags", data={"name": "x"}, follow_redirects=False)
            assert r.status_code == 403, "create is not gated"
            r = client.post("/admin/tags/financeiro/delete", follow_redirects=False)
            assert r.status_code == 403, "delete is not gated"
    finally:
        (
            store.available,
            store.is_admin,
            store.list_tags,
            store.create_tag,
            store.delete_tag,
            store.list_roles,
            store.upsert_user,
            store.set_user_admin,
        ) = saved

    # set_tags used to delete every tag no resource carried, on the grounds
    # that an unused tag is noise in the filter bar. With a management screen
    # that is now destructive: a tag created there starts unused, and this runs
    # on every tag edit anywhere, so the next person to retag a chart would
    # silently wipe the vocabulary somebody had just defined.
    assert "DELETE FROM tags" not in inspect.getsource(store.set_tags), (
        "set_tags sweeps unused tags again — that deletes tags created on "
        "/admin/tags before anyone gets to use them. Deletion is explicit now, "
        "via delete_tag()."
    )
    # And it must not create, either. Tagging is open to anyone who can build;
    # defining the vocabulary is not. If applying a tag can invent one, the
    # admin screen is decoration and the list drifts into near-duplicates that
    # each find a different subset.
    assert "INSERT INTO tags" not in inspect.getsource(store.set_tags), (
        "set_tags creates tags again — tagging something would then be a way "
        "around /admin/tags for anyone with the builder feature."
    )
    print("tag management: OK")


def test_dashboard_page_runs_no_warehouse_queries():
    """Opening a dashboard must not wait on the warehouse. At all.

    The tiles have fetched themselves since the async rewrite, but the filter
    drawer's option lists — one `SELECT DISTINCT` per select filter — were
    still built while rendering. On the Automatismo dashboard that was eleven
    queries totalling 292 seconds before a byte was sent, for a drawer most
    visits never open. They belong behind /filter-options, which is cached and
    only asked for when somebody opens the thing.
    """
    from datetime import datetime

    from app import db as db_mod
    from app import store

    chart = store.Chart(
        id=1, slug="vendas", title="Vendas", source_db="analytics",
        sql="SELECT a, b FROM t WHERE 1=1 {{ filters }}",
        chart_type="bar", x_column="a", y_columns=["b"],
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    dash = store.Dashboard(
        slug="automatismo", title="Automatismo", id=9,
        items=[store.DashboardItem(id=5, position=0, chart=chart, grid_x=0, grid_y=0,
                                   grid_w=6, grid_h=50)],
    )

    class F:
        key = "cliente"
        label = "Cliente"
        filter_type = "select"
        column_expr = "c_id"
        values_sql = "SELECT DISTINCT c_id FROM huge_table"
        source_db = "analytics"
        default_value = ""
        applies_to = ["vendas"]

    ran: list[str] = []
    saved = (
        store.available, store.get_dashboard, store.list_filters, store.slug_for,
        store.upsert_user, store.get_filter_options, store.put_filter_options,
        db_mod.execute,
    )
    store.available = lambda: True
    store.get_dashboard = lambda s, with_items=True: dash if s == "automatismo" else None
    store.list_filters = lambda s: [F()]
    store.slug_for = lambda table, ident: "automatismo" if str(ident) == "9" else str(ident)
    store.upsert_user = _no_op_user
    store.get_filter_options = lambda s: {}
    store.put_filter_options = lambda s, k, v: None

    def _execute(sql, database=None, max_rows=None, params=None):
        ran.append(sql)
        return db_mod.QueryResult(
            returns_rows=True, columns=["c_id"], rows=[("acme",)], rowcount=1
        )

    db_mod.execute = _execute
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            body = client.get("/dashboards/9").text
            assert ran == [], f"the dashboard page hit the warehouse: {ran}"
            assert "filter-options" in body, "the drawer has nowhere to fetch its options"
            assert "huge_table" not in body, "an options query leaked into the page"

            # Asked for explicitly, they run — and are cached on the way out.
            payload = client.get("/dashboards/9/filter-options").json()
            assert payload["options"] == {"cliente": ["acme"]}, payload
            assert ran == ["SELECT DISTINCT c_id FROM huge_table"], ran

            # A filtered link still carries its selection with no JS involved.
            body = client.get("/dashboards/9?cliente=acme").text
            assert 'value="acme"' in body and "selected" in body, (
                "a filtered link lost its selection without the options fetch"
            )
    finally:
        (
            store.available, store.get_dashboard, store.list_filters, store.slug_for,
            store.upsert_user, store.get_filter_options, store.put_filter_options,
            db_mod.execute,
        ) = saved
    print("dashboard page runs no warehouse queries: OK")


def test_imported_layout_mirrors_superset():
    """An imported dashboard must be the original, not a tidied-up version.

    The translator used to round every height into a coarser row, squeeze out
    blank bands and flatten text blocks to two rows — each defensible on its
    own, and together they moved everything. It also never stacked a COLUMN,
    dropped dividers, and read a chart's own name where the dashboard had
    renamed it.
    """
    import json

    from tools.superset_migrate import translate as T

    def node(kind, children=(), **meta):
        return {"type": kind, "children": list(children), "meta": meta}

    pos = {
        "ROOT_ID": node("ROOT", ["GRID_ID"]),
        "GRID_ID": node("GRID", ["HEAD", "ROW1", "DIV", "TABS1"]),
        "HEAD": node("HEADER", text="Operação", headerSize="MEDIUM_HEADER"),
        "ROW1": node("ROW", ["C1", "COL1"]),
        "C1": node("CHART", chartId=11, sliceName="Vendas", width=4, height=30),
        "COL1": node("COLUMN", ["C2", "MD1"], width=8),
        "C2": node(
            "CHART", chartId=12, sliceName="Custos", sliceNameOverride="Custos 2026",
            width=8, height=20,
        ),
        "MD1": node("MARKDOWN", code="**nota**", width=8, height=10),
        "DIV": node("DIVIDER"),
        "TABS1": node("TABS", ["TAB_A", "TAB_B"]),
        "TAB_A": node("TAB", ["ROW_A"], text="Resumo"),
        "ROW_A": node("ROW", ["C3"]),
        "C3": node("CHART", chartId=13, sliceName="SLA", width=12, height=40),
        "TAB_B": node("TAB", ["ROW_B"], text="Detalhe"),
        "ROW_B": node("ROW", ["C4"]),
        "C4": node("CHART", chartId=14, sliceName="Chamados", width=6, height=40),
    }
    tiles, tabs = T.layout_of(json.dumps(pos))
    by_id = {t.node_id: t for t in tiles}

    assert tabs == ["Resumo", "Detalhe"], tabs
    assert len(tiles) == 7, [t.node_id for t in tiles]

    # Heights arrive as Superset stores them. 30 is not 30/7 rounded to 4.
    assert by_id["C1"].h == 30, by_id["C1"]
    assert by_id["MD1"].h == 10, "a text block was flattened again"

    # A header spans its parent; it carries no width of its own.
    assert (by_id["HEAD"].x, by_id["HEAD"].w) == (0, 12), by_id["HEAD"]
    assert by_id["HEAD"].content.startswith("## "), by_id["HEAD"].content

    # Side by side in the row: the column starts where the chart ends.
    assert by_id["C1"].x == 0 and by_id["COL1" if "COL1" in by_id else "C2"].x == 4
    # …and the column stacks its own children instead of laying them across.
    assert by_id["C2"].x == by_id["MD1"].x == 4, "COLUMN laid out sideways"
    assert by_id["MD1"].y > by_id["C2"].y, "COLUMN did not stack"

    # The rule Superset drew is a tile, not a dropped node.
    assert by_id["DIV"].kind == "divider", by_id["DIV"]

    # The name the dashboard gives a chart wins over the chart's own.
    assert by_id["C2"].title == "Custos 2026", by_id["C2"].title
    assert by_id["C1"].title == "Vendas"

    # Each tab is its own pane, so both start at the top rather than the
    # second one being pushed below the first.
    assert by_id["C3"].tab == "Resumo" and by_id["C4"].tab == "Detalhe"
    assert by_id["C3"].y == by_id["C4"].y == 0, "tabs share one vertical cursor"

    # Nothing may sit on top of anything else in the same pane.
    for i, a in enumerate(tiles):
        for b in tiles[i + 1:]:
            if a.tab != b.tab:
                continue
            assert not (
                a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h
            ), f"{a.node_id} overlaps {b.node_id}"

    # Nested tabs keep their path, so two tabs named the same stay apart.
    nested = {
        "ROOT_ID": node("ROOT", ["GRID_ID"]),
        "GRID_ID": node("GRID", ["T1"]),
        "T1": node("TABS", ["TA"]),
        "TA": node("TAB", ["T2"], text="Norte"),
        "T2": node("TABS", ["TB"]),
        "TB": node("TAB", ["R"], text="Resumo"),
        "R": node("ROW", ["C"]),
        "C": node("CHART", chartId=21, sliceName="x", width=6, height=20),
    }
    _tiles, nested_tabs = T.layout_of(json.dumps(nested))
    assert "Norte / Resumo" in nested_tabs, nested_tabs
    print("imported layout mirrors superset: OK")


def test_every_resource_type_is_grantable():
    """Each permission type must offer checkboxes on the roles screen.

    A type missing from _grantable() renders an empty section: the permission
    can't be granted at all, and because each section posts its full set, an
    existing grant of that type is wiped the moment the section is saved. That
    is invisible on the page — an empty section looks exactly like "nothing
    exists yet" — so it gets a test rather than a careful reading.
    """
    from app import datasets as ds_mod
    from app import db as db_mod
    from app import main, reports, store

    saved = (
        db_mod.list_databases,
        ds_mod.list_datasets,
        reports.all_reports,
        store.list_charts,
        store.list_dashboards,
    )
    db_mod.list_databases = lambda: ["analytics"]
    ds_mod.list_datasets = lambda: [ds_mod.Dataset(name="companies", kind="table", column_count=5)]
    reports.all_reports = lambda: [
        reports.Report(key="faturamento", title="F", database="analytics", sql="SELECT 1")
    ]
    store.list_charts = lambda: [
        store.Chart(
            id=1,
            slug="c1",
            title="C",
            source_db="analytics",
            sql="SELECT 1",
            chart_type="bar",
            x_column="a",
        )
    ]
    store.list_dashboards = lambda: [store.Dashboard(id=1, slug="d1", title="D")]
    try:
        grantable = main._grantable()
        missing = [t for t in store.RESOURCE_TYPES if t not in grantable]
        assert not missing, f"no checkboxes would render for: {missing}"
        empty = [t for t in store.RESOURCE_TYPES if not grantable[t]]
        assert not empty, f"every source was stubbed non-empty, yet these are empty: {empty}"
        # A UI-created report is as grantable as a reports.toml one.
        assert "faturamento" in grantable[store.REPORT]
    finally:
        (
            db_mod.list_databases,
            ds_mod.list_datasets,
            reports.all_reports,
            store.list_charts,
            store.list_dashboards,
        ) = saved
    print("every resource type grantable: OK")


def test_user_detail_page():
    """The per-user page: roles are edited here, and it explains the result."""
    from app import store

    ana = store.User(
        id=2,
        username="ana.silva",
        email="a@v360.io",
        display_name="Ana Silva",
        auth_via="superset",
        roles=["Finance"],
    )
    roles = [
        store.Role(
            id=1, name="Analytics", is_default=True, permissions={store.DATABASE: ["analytics"]}
        ),
        store.Role(
            id=2,
            name="Finance",
            description="Billing warehouses.",
            permissions={store.DATABASE: ["dw_v360"], store.FEATURE: ["sql_console"]},
        ),
    ]
    saved = (
        store.available,
        store.is_admin,
        store.get_user,
        store.list_users,
        store.list_roles,
        store.access_for,
        store.upsert_user,
        store.set_user_admin,
        store.set_user_roles,
    )
    store.available = lambda: True
    store.is_admin = lambda u: True
    store.upsert_user = _no_op_user
    store.set_user_admin = lambda u, v: True
    store.set_user_roles = lambda u, r: True
    store.get_user = lambda u: ana if u == "ana.silva" else None
    store.list_users = lambda: [ana]
    store.list_roles = lambda: roles
    store.access_for = lambda u: store.Access(
        username=u,
        granted={store.DATABASE: {"dw_v360"}, store.FEATURE: {"sql_console"}},
    )
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            r = client.get("/admin/users/ana.silva")
            assert r.status_code == 200, r.text[:300]
            # every role is offered here (this is where you assign them)
            assert "Analytics" in r.text and "Finance" in r.text
            # ...and the page explains what those roles resolve to
            assert "Effective access" in r.text
            assert "dw_v360" in r.text, "effective grants not shown"
            assert "sql_console" in r.text
            # the role that grants it is attributable
            assert "via Finance" in r.text, "grant source not shown"

            r = client.get("/admin/users/nobody", follow_redirects=False)
            assert r.status_code == 404, r.status_code

            # saving roles returns to the user's page, not the list
            r = client.post(
                "/admin/users/ana.silva/roles",
                data={"roles": ["Analytics"]},
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert "/admin/users/ana.silva" in r.headers["location"], r.headers["location"]
    finally:
        (
            store.available,
            store.is_admin,
            store.get_user,
            store.list_users,
            store.list_roles,
            store.access_for,
            store.upsert_user,
            store.set_user_admin,
            store.set_user_roles,
        ) = saved
    print("user detail page: OK")


def test_nav_hides_what_you_cannot_reach():
    """A feature you have no permission for isn't in the sidebar at all."""
    from app import main as main_mod
    from app import store

    saved = (
        store.available,
        store.upsert_user,
        store.list_charts,
        store.list_dashboards,
        store.list_folders,
        store.list_reports,
    )
    store.available = lambda: True
    store.upsert_user = _no_op_user
    # Every reader the pages below touch. available() is stubbed True, so an
    # unstubbed one would fall back to the real DB_HOST and make this test hit
    # the network — passing or timing out depending on where it's run.
    store.list_charts = lambda: []
    store.list_dashboards = lambda: []
    store.list_folders = lambda: []
    store.list_reports = lambda: []

    # Every signed-in page, not just the console. The nav is built per page from
    # the `access` that page passes down, so one route forgetting to pass it
    # shows the full sidebar on that page alone — which is how /charts shipped
    # briefly listing features the viewer had no grant for.
    PAGES = ("/", "/charts", "/dashboards", "/reports", "/datasets")

    def _render_nav(access, path="/"):
        main_mod.app.dependency_overrides[main_mod.access_for] = lambda: access
        try:
            with TestClient(app, base_url="https://testserver") as client:
                client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})
                return client.get(path).text
        finally:
            main_mod.app.dependency_overrides.pop(main_mod.access_for, None)

    try:
        narrow = store.Access(
            username="ana",
            granted={store.DATABASE: {"analytics"}, store.FEATURE: {"sql_console"}},
        )
        for path in PAGES:
            body = _render_nav(narrow, path)
            for hidden in (
                "<span>Gráficos</span>",
                "<span>Painéis</span>",
                "<span>Conjuntos de dados</span>",
                "<span>Relatórios</span>",
            ):
                assert hidden not in body, f"{hidden} shown without a grant, on {path}"

        # The one entry she does have is present.
        assert "<span>Consulta</span>" in _render_nav(narrow), (
            "granted feature missing from the nav"
        )

        # An admin sees the lot
        body = _render_nav(store.Access(username="boss", everything=True))
        for shown in (
            "<span>Consulta</span>",
            "<span>Gráficos</span>",
            "<span>Painéis</span>",
            "<span>Conjuntos de dados</span>",
        ):
            assert shown in body, f"{shown} hidden from an admin"

        # No grants at all -> no nav entries
        body = _render_nav(store.Access(username="new"))
        for hidden in (
            "<span>Consulta</span>",
            "<span>Gráficos</span>",
            "<span>Painéis</span>",
            "<span>Conjuntos de dados</span>",
        ):
            assert hidden not in body, f"{hidden} shown to a user with no grants"
    finally:
        (
            store.available,
            store.upsert_user,
            store.list_charts,
            store.list_dashboards,
            store.list_folders,
            store.list_reports,
        ) = saved
    print("nav hidden by permission: OK")


def test_last_admin_cannot_be_removed():
    """Removing the only administrator would make the panel unreachable."""
    from app import store

    only = [store.User(id=1, username="admin", is_admin=True)]
    two = only + [store.User(id=2, username="other", is_admin=True)]
    state = {"users": only, "set": []}

    saved = (
        store.available,
        store.is_admin,
        store.list_users,
        store.list_roles,
        store.set_user_admin,
        store.upsert_user,
    )
    store.available = lambda: True
    store.is_admin = lambda u: True
    store.list_users = lambda: list(state["users"])
    store.list_roles = lambda: []
    store.upsert_user = _no_op_user  # keep the login off any real database
    store.set_user_admin = lambda u, v: state["set"].append((u, v)) or True
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

            r = client.post(
                "/admin/users/admin/admin", data={"value": "false"}, follow_redirects=False
            )
            assert r.status_code == 400, r.status_code
            assert "only administrator" in r.text
            assert state["set"] == [], "the last admin was removed anyway"

            # With a second admin present it goes through
            state["users"] = two
            r = client.post(
                "/admin/users/admin/admin", data={"value": "false"}, follow_redirects=False
            )
            assert r.status_code == 303, r.status_code
            assert state["set"] == [("admin", False)], state["set"]
    finally:
        (
            store.available,
            store.is_admin,
            store.list_users,
            store.list_roles,
            store.set_user_admin,
            store.upsert_user,
        ) = saved
    print("last admin protected: OK")


def test_access_resolution_fails_closed():
    """access_for(): admins and ('*','*') roles get everything; nobody else
    gets anything they weren't granted."""
    from app import store

    users = {
        "boss": {"id": 1, "is_admin": True, "is_active": True},
        "ana": {"id": 2, "is_admin": False, "is_active": True},
        "root_role": {"id": 3, "is_admin": False, "is_active": True},
        "gone": {"id": 4, "is_admin": False, "is_active": False},
    }
    perms = {
        2: [("database", "analytics"), ("feature", "sql_console"), ("chart", "cap")],
        3: [("*", "*")],
    }

    class _R:
        def __init__(self, mapping=None, rows=()):
            self._mapping = mapping
            self._rows = list(rows)

        def first(self):
            return self if self._mapping is not None else None

        def __iter__(self):
            return iter(self._rows)

    class _Conn:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "is_admin, is_active FROM users" in sql:
                row = users.get(params["u"])
                return _R(mapping=row) if row else _R()
            if "role_permissions" in sql:
                return _R(rows=perms.get(params["id"], []))
            return _R()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _run(username):
        original = store.engine
        conn = _Conn()
        store.engine = lambda: type("E", (), {"connect": staticmethod(lambda: conn)})()
        try:
            return store.access_for(username)
        finally:
            store.engine = original

    boss = _run("boss")
    assert boss.everything, "an admin must reach everything"
    assert boss.allows(store.DATABASE, "dw_whirlpool")

    # A role holding ('*','*') is equivalent to the admin flag for access
    assert _run("root_role").everything, "the ('*','*') grant must mean everything"

    ana = _run("ana")
    assert not ana.everything
    assert ana.allows(store.DATABASE, "analytics")
    assert not ana.allows(store.DATABASE, "dw_whirlpool"), "ungranted database allowed"
    assert ana.allows(store.FEATURE, "sql_console")
    assert not ana.allows(store.FEATURE, "chart_builder")
    assert ana.allows(store.CHART, "cap") and not ana.allows(store.CHART, "other")
    # filter() narrows a real list rather than trusting the caller
    assert ana.filter(store.DATABASE, ["analytics", "dw_vale", "keycloak"]) == ["analytics"]

    for who in ("gone", "nobody"):
        a = _run(who)
        assert not a.everything
        assert a.granted == {}, who
        assert not a.allows(store.DATABASE, "analytics"), f"{who} was allowed in"

    # NO_ACCESS is the safe default a context builder falls back to
    assert not store.NO_ACCESS.allows(store.DATABASE, "analytics")
    assert store.NO_ACCESS.filter(store.DATABASE, ["analytics"]) == []
    print("access resolution fails closed: OK")


def test_enforcement_at_every_route():
    """The point of the whole feature: an ungranted resource is refused by the
    server, not merely hidden in the UI.

    Every case here posts or requests something the user has no grant for, the
    way someone editing a form field or pasting a URL would.
    """
    from app import charts as charts_mod
    from app import datasets as ds
    from app import db as db_mod
    from app import main as main_mod
    from app import store

    _install_catalog_stub(ds)

    # Granted: the analytics database, the sql_console feature, one chart, one
    # dashboard. Everything else must be refused.
    limited = store.Access(
        username="ana",
        granted={
            store.DATABASE: {"analytics"},
            store.FEATURE: {"sql_console"},
            store.CHART: {"mine"},
            store.DASHBOARD: {"ops"},
        },
    )

    mine = store.Chart(
        id=1,
        slug="mine",
        title="Mine",
        source_db="analytics",
        sql="SELECT 1",
        chart_type="bar",
        x_column="a",
        y_columns=["b"],
    )
    theirs = store.Chart(
        id=2,
        slug="theirs",
        title="Theirs",
        source_db="dw_whirlpool",
        sql="SELECT 1",
        chart_type="bar",
        x_column="a",
        y_columns=["b"],
    )
    ops = store.Dashboard(id=1, slug="ops", title="Ops")
    ops.items = [
        store.DashboardItem(id=1, chart=mine, position=0, width="full"),
        store.DashboardItem(id=2, chart=theirs, position=1, width="half"),
    ]
    secret = store.Dashboard(id=2, slug="secret", title="Secret")

    saved = (
        main_mod.access_for,
        store.available,
        store.list_charts,
        store.get_chart,
        store.list_dashboards,
        store.get_dashboard,
        db_mod.list_databases,
        db_mod.execute,
        store.upsert_user,
    )
    main_mod.app.dependency_overrides[main_mod.access_for] = lambda: limited
    store.available = lambda: True
    store.upsert_user = _no_op_user
    store.list_charts = lambda: [mine, theirs]
    store.get_chart = lambda s: {"mine": mine, "theirs": theirs}.get(s)
    store.list_dashboards = lambda: [ops, secret]
    store.get_dashboard = lambda s, with_items=True: {"ops": ops, "secret": secret}.get(s)
    db_mod.list_databases = lambda: ["analytics", "dw_whirlpool", "keycloak"]
    db_mod.execute = lambda sql, database=None, max_rows=None, params=None: db_mod.QueryResult(
        returns_rows=True, columns=["a", "b"], rows=[(1, 2)], rowcount=1
    )
    try:
        with TestClient(app, base_url="https://testserver") as client:
            # `admin` is the only local account; the identity is irrelevant here
            # because access_for is overridden to return `limited`.
            r = client.post(
                "/login/local",
                data={"username": "admin", "password": "s3cret-pass"},
                follow_redirects=False,
            )
            assert r.status_code == 303, f"login failed: {r.status_code}"

            # ── the reported hole: querying an ungranted database ──
            r = client.post(
                "/query",
                data={"sql": "SELECT 1", "database": "dw_whirlpool"},
                follow_redirects=False,
            )
            assert r.status_code == 403, f"ungranted database queried! {r.status_code}"
            r = client.post(
                "/query/export",
                data={"sql": "SELECT 1", "database": "keycloak"},
                follow_redirects=False,
            )
            assert r.status_code == 403, "export bypassed the database gate"
            # the granted one still works
            r = client.post("/query", data={"sql": "SELECT 1", "database": "analytics"})
            assert r.status_code == 200, r.status_code

            # ── the picker never names what you can't reach ──
            r = client.get("/")
            assert "dw_whirlpool" not in r.text and "keycloak" not in r.text, (
                "ungranted database names leaked into the console page"
            )

            # ── charts ──
            body = client.get("/charts").text
            assert "Mine" in body and "Theirs" not in body, "ungranted chart listed"
            assert client.get("/charts/theirs", follow_redirects=False).status_code == 403
            assert client.get("/charts/mine").status_code == 200
            # no chart_builder feature -> builder refused entirely
            assert client.get("/charts/new", follow_redirects=False).status_code == 403
            r = client.post(
                "/charts/new",
                data={"sql": "SELECT 1", "source_db": "analytics"},
                follow_redirects=False,
            )
            assert r.status_code == 403, "builder ran without the feature grant"
            r = client.post(
                "/charts/save",
                data={
                    "sql": "SELECT 1",
                    "source_db": "dw_whirlpool",
                    "title": "x",
                    "chart_type": "bar",
                    "x_column": "a",
                },
                follow_redirects=False,
            )
            assert r.status_code == 403, "save bypassed the gate"

            # ── dashboards ──
            body = client.get("/dashboards").text
            assert "Ops" in body and "Secret" not in body, "ungranted dashboard listed"
            assert client.get("/dashboards/secret", follow_redirects=False).status_code == 403
            r = client.get("/dashboards/ops")
            assert r.status_code == 200
            # the tile built on an ungranted chart is dropped from a dashboard
            # the user *can* see
            assert "Mine" in r.text and "Theirs" not in r.text, (
                "a dashboard leaked a chart the user has no grant for"
            )
            # no dashboard_builder feature
            assert client.get("/dashboards/ops/edit", follow_redirects=False).status_code == 403
            r = client.post("/dashboards", data={"title": "x"}, follow_redirects=False)
            assert r.status_code == 403
            r = client.post(
                "/dashboards/ops/items", data={"chart_slug": "mine"}, follow_redirects=False
            )
            assert r.status_code == 403, "layout mutation not gated"

            # ── datasets: needs the feature, the database AND the dataset ──
            assert client.get("/datasets", follow_redirects=False).status_code == 403
            assert client.get("/datasets/companies", follow_redirects=False).status_code == 403

            # With the feature + database but no per-dataset grant, the catalog
            # opens and is empty rather than listing tables you can't open.
            main_mod.app.dependency_overrides[main_mod.access_for] = lambda: store.Access(
                username="ana",
                granted={
                    store.DATABASE: {"analytics"},
                    store.FEATURE: {"dataset_catalog"},
                    store.DATASET: {"companies"},
                },
            )
            r = client.get("/datasets")
            assert r.status_code == 200, r.status_code
            assert "companies" in r.text, "granted dataset missing"
            assert "captura" not in r.text, "ungranted dataset name leaked into the catalog"
            assert client.get("/datasets/companies").status_code == 200
            r = client.get("/datasets/captura", follow_redirects=False)
            assert r.status_code == 403, f"ungranted dataset opened: {r.status_code}"
            main_mod.app.dependency_overrides[main_mod.access_for] = lambda: limited

            # ── reports ──
            assert client.get("/report/anything/export", follow_redirects=False).status_code == 403
    finally:
        main_mod.app.dependency_overrides.pop(main_mod.access_for, None)
        (
            main_mod.access_for,
            store.available,
            store.list_charts,
            store.get_chart,
            store.list_dashboards,
            store.get_dashboard,
            db_mod.list_databases,
            db_mod.execute,
            store.upsert_user,
        ) = saved
    assert charts_mod.MAX_SERIES  # keeps the import meaningful
    print("enforcement at every route: OK")


def test_row_limit_is_clamped():
    """The console's row box can't exceed the configured ceiling."""
    from app.config import get_settings
    from app.main import _row_limit

    s = get_settings()
    assert _row_limit(50) == 50
    assert _row_limit("250") == 250
    # over the ceiling -> clamped, not honoured
    assert _row_limit(s.query_max_rows * 10) == s.query_max_rows
    assert _row_limit(999_999_999) == s.query_max_rows
    # nonsense falls back rather than erroring, so a typo can't lose the query
    assert _row_limit("") == s.query_default_rows
    assert _row_limit(None) == s.query_default_rows
    assert _row_limit("; DROP TABLE") == s.query_default_rows
    # zero and negatives are meaningless
    assert _row_limit(0) == 1
    assert _row_limit(-5) == 1
    print("row limit clamped: OK")


def test_wildcard_grant_covers_a_whole_type():
    """key='*' grants every resource of that type, including future ones."""
    from app import store

    a = store.Access(username="x", granted={store.CHART: {store.ANY}})
    assert a.allows(store.CHART, "anything-at-all")
    assert a.filter(store.CHART, ["a", "b"]) == ["a", "b"]
    assert not a.allows(store.DASHBOARD, "a"), "'*' must not leak across types"
    assert a.has_any(store.CHART) and not a.has_any(store.DASHBOARD)
    print("wildcard grant: OK")


def test_dashboards_disabled_without_app_db():
    """No app database -> the tab explains itself instead of erroring."""
    import importlib

    from app import config, store
    from app import main as main_mod

    saved = os.environ.get("APP_DB_PASSWORD")
    os.environ["APP_DB_PASSWORD"] = ""
    config.get_settings.cache_clear()
    store.reset_engine()
    reloaded = importlib.reload(main_mod)
    try:
        with TestClient(reloaded.app, base_url="https://testserver") as client:
            client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})
            r = client.get("/dashboards")
            assert r.status_code == 503, r.status_code
            assert "not configured" in r.text
            r = client.get("/dashboards/anything")
            assert r.status_code == 503, r.status_code
    finally:
        if saved is None:
            os.environ.pop("APP_DB_PASSWORD", None)
        else:
            os.environ["APP_DB_PASSWORD"] = saved
        config.get_settings.cache_clear()
        store.reset_engine()
        importlib.reload(main_mod)
    print("dashboards disabled path: OK")


def test_charts_disabled_without_app_db():
    """With no app database the Charts tab explains itself instead of 500ing."""
    import importlib

    from app import config, store
    from app import main as main_mod

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
            assert "Conectado como marcelo.ferreira" in r.text, "delegated login failed"

            # Logout goes through Superset — clearing only our cookie would let
            # the next request sign straight back in.
            r = client.post("/logout", follow_redirects=False)
            assert r.headers["location"] == "https://bi.v360.io/logout/", r.headers["location"]

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
            "[[report]]\n"
            'key = "t_report"\n'
            'title = "Temp report"\n'
            'database = "main"\n'
            'sql = "SELECT * FROM t ORDER BY id"\n'
        )

    with TestClient(app, base_url="https://testserver") as client:
        client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})

        # The console shows the DB picker; the report lives on /reports now
        r = client.get("/")
        assert ">main<" in r.text and ">other<" in r.text, "db picker not populated"
        assert "Temp report" not in r.text, "reports still rendered inside the console"

        r = client.get("/reports")
        assert r.status_code == 200, r.status_code
        assert "Temp report" in r.text, "report missing from its own page"

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
    test_dashboard_layout_ordering()
    test_dashboard_widths_are_validated()
    test_dashboard_pages_render()
    test_tags_and_listing_views()
    test_preview_cache()
    test_interface_language()
    test_urls_address_charts_and_dashboards_by_id()
    test_dashboard_filters_bind_values()
    test_list_pages_render_without_folder_ui()
    test_folders_are_organisation_only()
    test_admin_panel_and_gate()
    test_tag_management()
    test_dashboard_page_runs_no_warehouse_queries()
    test_imported_layout_mirrors_superset()
    test_every_resource_type_is_grantable()
    test_user_detail_page()
    test_nav_hides_what_you_cannot_reach()
    test_last_admin_cannot_be_removed()
    test_access_resolution_fails_closed()
    test_row_limit_is_clamped()
    test_wildcard_grant_covers_a_whole_type()
    test_enforcement_at_every_route()
    # Last: these reload app.main, which rebinds this module's `app` reference.
    test_dashboards_disabled_without_app_db()
    test_charts_disabled_without_app_db()
    test_superset_delegated_mode()
    test_superset_break_glass()
    test_sso_only_mode()
    print("\nAll smoke tests passed.")
