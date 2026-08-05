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
    print("chart spec: OK")


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
    db_mod.execute = lambda sql, database=None: db_mod.QueryResult(
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

            r = client.get("/dashboards/ops")
            assert r.status_code == 200, r.text[:400]
            assert "Capturas por dia" in r.text
            assert 'id="tile-0"' in r.text, "tile canvas missing"
            assert '"tile-0"' in r.text, "chart spec payload missing"
            assert "bi-tile-full" in r.text, "tile width not applied to the grid"

            r = client.get("/dashboards/ops/edit")
            assert r.status_code == 200, r.text[:400]
            assert "Move earlier" in r.text, "editor controls missing"
            assert "/items/11/remove" in r.text, "remove action missing"

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

    saved_avail, saved_upsert = store.available, store.upsert_user
    store.available = lambda: True
    store.upsert_user = _no_op_user

    def _render_nav(access):
        main_mod.app.dependency_overrides[main_mod.access_for] = lambda: access
        try:
            with TestClient(app, base_url="https://testserver") as client:
                client.post("/login/local", data={"username": "admin", "password": "s3cret-pass"})
                return client.get("/").text
        finally:
            main_mod.app.dependency_overrides.pop(main_mod.access_for, None)

    try:
        # Only the query console, on one database
        body = _render_nav(
            store.Access(
                username="ana",
                granted={store.DATABASE: {"analytics"}, store.FEATURE: {"sql_console"}},
            )
        )
        assert "<span>Query</span>" in body, "granted feature missing from the nav"
        for hidden in (
            "<span>Charts</span>",
            "<span>Dashboards</span>",
            "<span>Datasets</span>",
            "<span>Reports</span>",
        ):
            assert hidden not in body, f"{hidden} shown without a grant"

        # An admin sees the lot
        body = _render_nav(store.Access(username="boss", everything=True))
        for shown in (
            "<span>Query</span>",
            "<span>Charts</span>",
            "<span>Dashboards</span>",
            "<span>Datasets</span>",
        ):
            assert shown in body, f"{shown} hidden from an admin"

        # No grants at all -> no nav entries
        body = _render_nav(store.Access(username="new"))
        for hidden in (
            "<span>Query</span>",
            "<span>Charts</span>",
            "<span>Dashboards</span>",
            "<span>Datasets</span>",
        ):
            assert hidden not in body, f"{hidden} shown to a user with no grants"
    finally:
        store.available, store.upsert_user = saved_avail, saved_upsert
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
    db_mod.execute = lambda sql, database=None: db_mod.QueryResult(
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

            # ── datasets: needs the feature as well as the database ──
            assert client.get("/datasets", follow_redirects=False).status_code == 403
            assert client.get("/datasets/companies", follow_redirects=False).status_code == 403

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
            assert "Signed in as marcelo.ferreira" in r.text, "delegated login failed"

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
    test_dashboard_layout_ordering()
    test_dashboard_widths_are_validated()
    test_dashboard_pages_render()
    test_admin_panel_and_gate()
    test_user_detail_page()
    test_nav_hides_what_you_cannot_reach()
    test_last_admin_cannot_be_removed()
    test_access_resolution_fails_closed()
    test_wildcard_grant_covers_a_whole_type()
    test_enforcement_at_every_route()
    # Last: these reload app.main, which rebinds this module's `app` reference.
    test_dashboards_disabled_without_app_db()
    test_charts_disabled_without_app_db()
    test_superset_delegated_mode()
    test_superset_break_glass()
    test_sso_only_mode()
    print("\nAll smoke tests passed.")
