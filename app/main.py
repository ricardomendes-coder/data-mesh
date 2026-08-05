import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuthError
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.middleware.sessions import SessionMiddleware

from . import charts, datasets, db, oidc, reports, store, superset_session, users
from .auth import NotAuthenticated, end_session, get_current_user, require_login, start_session
from .config import get_settings

# uvicorn only configures its own loggers and leaves root without a handler, so
# our logger.info() calls were being dropped. Give root a handler but keep it at
# WARNING — raising the root level to INFO would also turn on httpx, sqlalchemy
# and friends. Only this app's logger opts in to INFO.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("report_hub")
logger.setLevel(logging.INFO)
settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _inline_code(value: str) -> Markup:
    """Render `backticked` spans from datasets.toml prose as <code>.

    Descriptions constantly name columns, and raw backticks read as noise. The
    text is HTML-escaped *first*, so manifest prose can never inject markup —
    only the <code> tags added afterwards are live.
    """
    # str() is load-bearing: re.sub over a Markup concatenates through
    # Markup.__add__, which would escape the <code> tags we're inserting.
    escaped = str(escape(value or ""))
    return Markup(re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped))


templates.env.filters["inline_code"] = _inline_code

# Cap how many result rows are rendered in the browser. The full result set is
# still available via Export — this only bounds the HTML we build per request.
QUERY_DISPLAY_LIMIT = 500


def _file_response(df, fmt: str, basename: str) -> Response:
    """Serialize a DataFrame to CSV/xlsx and return it as a file download."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "xlsx":
        data = reports.to_xlsx_bytes(df)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{basename}_{timestamp}.xlsx"
    else:
        data = reports.to_csv_bytes(df)
        media = "text/csv"
        filename = f"{basename}_{timestamp}.csv"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the bootstrap admin on first run (only if no users exist yet).
    # Skipped under pure SSO, where the local user store is unused.
    if (
        settings.password_login_enabled
        and not users.any_users()
        and settings.initial_admin_user
        and settings.initial_admin_password
    ):
        users.add_user(settings.initial_admin_user, settings.initial_admin_password)
        logger.info("Created bootstrap admin user %r", settings.initial_admin_user)

    if store.available():
        try:
            store.init_schema()
            logger.info("App database ready: %s", settings.app_db_name)
        except Exception:
            # Charts degrade to an error banner; the rest of the app is
            # unaffected, so a bad app-DB config must not stop startup.
            logger.exception("Could not initialise the app database")
    else:
        logger.info("App database not configured — charts are disabled")

    if settings.superset_auth_enabled:
        logger.info(
            "Auth: delegated to Superset at %s (identity from /api/v1/me/)",
            settings.superset_internal_url,
        )
    if settings.sso_enabled:
        logger.info("Auth: SSO via %s", settings.sso_metadata_url)
    if settings.password_login_enabled:
        logger.info("Auth: local password login enabled (/login/local)")
    if not (
        settings.superset_auth_enabled or settings.sso_enabled or settings.password_login_enabled
    ):
        # Almost always AUTH_MODE=sso with the client id/secret missing. Log it
        # loudly rather than crash-looping, so /healthz stays reachable.
        logger.error(
            "No login method is usable: AUTH_MODE=%r but SSO_CLIENT_ID/"
            "SSO_CLIENT_SECRET are not both set. Nobody can sign in.",
            settings.auth_mode,
        )
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie=settings.session_cookie_name,
    path=settings.session_cookie_path,
    max_age=settings.session_max_age,
    same_site="lax",  # must not be "strict": the SSO redirect back from
    # Keycloak is a cross-site navigation and would drop the cookie, losing
    # the OAuth state and failing every login.
    https_only=True,
)

# Brand assets (logo favicon, login artwork). Mounted before the routes so
# `url_for('static', path=...)` resolves in the templates; behind nginx these
# are served as /report/static/... via the app's root_path.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(NotAuthenticated)
async def _redirect_to_login(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url=request.url_for("login_form"), status_code=303)


def _register_login(request: Request, username: str, claims: dict | None, via: str) -> None:
    """Record the login in the app database, creating the user on first sight.

    Self-registration, the way Superset's AUTH_USER_REGISTRATION works: the
    identity provider decides who exists, this app decides what they may see.

    Deliberately best-effort. If the app database is down, people must still be
    able to log in and use the query console — losing the audit of last_seen_at
    is a far smaller problem than an outage locking everyone out.
    """
    if not store.available():
        return
    claims = claims or {}
    try:
        user = store.upsert_user(
            username,
            email=str(claims.get("email") or ""),
            display_name=str(claims.get("name") or ""),
            auth_via=via,
        )
        # Bootstrap: whoever INITIAL_ADMIN_USER names becomes admin on login, so
        # a fresh install always has someone who can reach the admin screens.
        if (
            settings.initial_admin_user
            and username == settings.initial_admin_user
            and not user.is_admin
        ):
            store.set_user_admin(username, True)
            user.is_admin = True
            logger.info("Granted admin to the bootstrap user %r", username)
        # Cached for the nav only; every admin action re-checks against the
        # database, so a revoked admin can't act on a stale session.
        request.session["is_admin"] = bool(user.is_admin)
    except Exception:
        logger.exception("Could not record the login for %r", username)


def _login_page(request: Request, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "title": settings.app_title,
            "sso_enabled": settings.sso_enabled,
            "superset_enabled": settings.superset_auth_enabled,
            "superset_login_url": superset_session.login_url(),
            "password_login_enabled": settings.password_login_enabled,
        },
        status_code=status_code,
    )


@app.get("/login")
async def login_form(request: Request):
    """Entry point for every unauthenticated request.

    There is never anything to choose between here, so this always redirects
    rather than showing an interstitial "sign in with..." page.
    """
    if get_current_user(request):
        return RedirectResponse(request.url_for("console"), status_code=303)

    if settings.superset_auth_enabled:
        # Same origin as Superset, so its cookie is already on this request.
        cookie = request.cookies.get(settings.superset_cookie_name)
        if cookie:
            result = await superset_session.identify(cookie)
            if result:
                username = result["username"]
                claims = superset_session.claims_from_result(result)
                start_session(
                    request,
                    username,
                    via="superset",
                    claims=claims,
                )
                _register_login(request, username, claims, "superset")
                logger.info("Superset-delegated login: %s", username)
                return RedirectResponse(request.url_for("console"), status_code=303)
        # Not signed in to BI 360 (or the cookie is stale). Superset owns the
        # only Keycloak redirect URI that is actually registered, so send them
        # there and let it run the OAuth flow on our behalf.
        return RedirectResponse(superset_session.login_url(), status_code=303)

    if settings.sso_enabled:
        return RedirectResponse(request.url_for("sso_login"), status_code=303)
    if settings.password_login_enabled:
        return RedirectResponse(request.url_for("login_local_form"), status_code=303)
    return _login_page(
        request,
        error="Login is not configured on this server. Check the server logs.",
        status_code=503,
    )


@app.get("/auth/sso")
async def sso_login(request: Request):
    """Kick off the OIDC authorization-code flow."""
    if not settings.sso_enabled:
        raise HTTPException(status_code=404, detail="SSO is not enabled")
    # An explicit redirect URI avoids depending on X-Forwarded-Proto being set
    # correctly all the way through the ALB -> nginx -> uvicorn chain, and it
    # has to match Keycloak's registered value exactly anyway.
    redirect_uri = settings.sso_redirect_uri or str(request.url_for("sso_callback"))
    return await oidc.get_client().authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def sso_callback(request: Request):
    """Where Keycloak sends the user back with an authorization code."""
    if not settings.sso_enabled:
        raise HTTPException(status_code=404, detail="SSO is not enabled")

    try:
        token = await oidc.get_client().authorize_access_token(request)
    except OAuthError as exc:
        # Covers user-cancelled consent, a stale/replayed state, and clock skew
        # on the id_token. None of it is actionable by the user beyond retrying.
        logger.warning("SSO callback rejected: %s — %s", exc.error, exc.description)
        return _login_page(
            request, error="Single sign-on failed. Please try again.", status_code=401
        )

    claims = token.get("userinfo") or {}
    if not claims:
        # No id_token in the response (realm not returning one for this client);
        # fall back to the userinfo endpoint, as Superset's manager does.
        claims = await oidc.get_client().userinfo(token=token)

    try:
        username = oidc.username_from_claims(claims)
    except ValueError:
        logger.error("SSO token carried no identity claim: %s", sorted(claims))
        return _login_page(
            request,
            error="Your SSO account is missing a username. Contact the data team.",
            status_code=403,
        )

    start_session(request, username, via="sso", claims=claims)
    _register_login(request, username, dict(claims), "sso")
    logger.info("SSO login: %s", username)
    return RedirectResponse(request.url_for("console"), status_code=303)


@app.get("/login/local", response_class=HTMLResponse)
def login_local_form(request: Request):
    """Break-glass password form. Only mounted when AUTH_MODE allows it."""
    if not settings.password_login_enabled:
        raise HTTPException(status_code=404, detail="Password login is disabled")
    if get_current_user(request):
        return RedirectResponse(request.url_for("console"), status_code=303)
    return _login_page(request)


@app.post("/login/local")
def login_local_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not settings.password_login_enabled:
        raise HTTPException(status_code=404, detail="Password login is disabled")
    if users.verify_user(username, password):
        start_session(request, username, via="password")
        _register_login(request, username, None, "password")
        return RedirectResponse(request.url_for("console"), status_code=303)
    logger.warning("Failed password login for %r", username)
    return _login_page(request, error="Invalid username or password.", status_code=401)


@app.post("/logout")
async def logout(request: Request):
    via = request.session.get("auth_via")
    end_session(request)

    if via == "superset" and settings.superset_auth_enabled:
        # Dropping only our cookie would be theatre: the very next request would
        # find the Superset cookie still there and sign the user back in. Send
        # them through Superset's logout so "log out" means what it says — which
        # does also end their BI 360 session.
        return RedirectResponse(settings.superset_logout_url, status_code=303)

    # Local-only logout by default. The Keycloak client is shared with Superset,
    # so ending the Keycloak session here would also sign the user out of BI 360
    # — set SSO_SINGLE_LOGOUT=true if that is what you want.
    if via == "sso" and settings.sso_enabled and settings.sso_single_logout:
        metadata = await oidc.get_client().load_server_metadata()
        end_session_endpoint = metadata.get("end_session_endpoint")
        if end_session_endpoint:
            post_logout = settings.sso_logout_redirect_uri or str(request.url_for("login_form"))
            params = {
                "client_id": settings.sso_client_id,
                "post_logout_redirect_uri": post_logout,
            }
            return RedirectResponse(f"{end_session_endpoint}?{urlencode(params)}", status_code=303)

    return RedirectResponse(request.url_for("login_form"), status_code=303)


def _shell_context(user: str, active_nav: str, **extra) -> dict:
    """Context every signed-in page needs — the bits _shell.html renders."""
    context = {
        "user": user,
        "title": settings.app_title,
        "active_nav": active_nav,
        "error": None,
        "message": None,
        "db_host": settings.db_host,
        "db_ok": True,
        "db_error": None,
        "datasets_database": settings.datasets_database,
        "datasets_schema": settings.datasets_schema,
    }
    context.update(extra)
    return context


def _console_context(user: str, **extra) -> dict:
    """Base template context: the DB picker list and the reports manifest.

    Both DB discovery and manifest parsing are best-effort — a failure degrades
    the page (banner + empty list) instead of 500ing the whole dashboard.
    """
    databases: list[str] = []
    db_ok = False
    db_error = None
    try:
        databases = db.list_databases()
        db_ok = True
    except Exception:
        logger.exception("Could not list databases")
        db_error = "Could not load the database list — check the server logs."

    try:
        report_list = reports.load_reports()
    except Exception:
        logger.exception("Could not load the reports manifest")
        report_list = []
        db_error = db_error or "Could not load the reports — check the server logs."

    active_tab = extra.pop("active_tab", "query")
    context = _shell_context(
        user,
        "reports" if active_tab == "reports" else "query",
        sql=None,
        database=settings.db_name,
        databases=databases,
        db_ok=db_ok,
        db_error=db_error,
        reports=report_list,
        result=None,
        active_tab=active_tab,
    )
    context.update(extra)
    return context


@app.get("/", response_class=HTMLResponse)
def console(
    request: Request,
    tab: str = "query",
    sql: str = "",
    database: str = "",
    user: str = Depends(require_login),
):
    """The query console and reports.

    `tab`/`sql`/`database` make the console deep-linkable, which is what the
    "Open in Query" buttons on a dataset page use to hand a query over.
    """
    context = _console_context(user, active_tab="reports" if tab == "reports" else "query")
    if sql:
        context["sql"] = sql
    if database:
        context["database"] = database
    return templates.TemplateResponse(request, "console.html", context)


@app.get("/datasets", response_class=HTMLResponse)
def datasets_index(
    request: Request,
    q: str = "",
    kind: str = "",
    user: str = Depends(require_login),
):
    """Everything in the analytics schema, grouped by the manifest's folders."""
    context = _shell_context(user, "datasets", q=q, kind=kind, groups=[], total=0, shown=0)
    try:
        all_datasets = datasets.list_datasets()
    except Exception:
        logger.exception("Could not read the dataset catalog")
        context["db_ok"] = False
        context["db_error"] = "Could not read the dataset catalog — check the server logs."
        return templates.TemplateResponse(request, "datasets.html", context, status_code=502)

    needle = q.strip().lower()
    matched = [
        d
        for d in all_datasets
        if (not kind or d.kind == kind)
        and (
            not needle
            or needle in d.name.lower()
            or needle in d.title.lower()
            or needle in d.description.lower()
        )
    ]
    context.update(total=len(all_datasets), shown=len(matched), groups=datasets.group(matched))
    return templates.TemplateResponse(request, "datasets.html", context)


@app.get("/datasets/{name}", response_class=HTMLResponse)
def dataset_detail(
    request: Request,
    name: str,
    user: str = Depends(require_login),
):
    """Preview, catalog, description and example queries for one dataset."""

    def _catalog_failure(message: str, status: int):
        context = _shell_context(
            user,
            "datasets",
            q="",
            kind="",
            groups=[],
            total=0,
            shown=0,
            db_ok=False,
            db_error=message,
        )
        return templates.TemplateResponse(request, "datasets.html", context, status_code=status)

    try:
        # Resolving through the catalog is also the guard that stops an
        # arbitrary path segment from ever reaching a query.
        dataset = datasets.get_dataset(name)
    except Exception:
        logger.exception("Could not read the dataset catalog for %r", name)
        return _catalog_failure("Could not read the dataset catalog — check the server logs.", 502)

    if dataset is None:
        return _catalog_failure(f"Unknown dataset: {name!r}.", 404)

    context = _shell_context(
        user,
        "datasets",
        dataset=dataset,
        columns=[],
        preview_columns=[],
        preview_rows=[],
        preview_error=None,
        default_query=f"SELECT *\nFROM {dataset.name}\nLIMIT 100",
    )

    try:
        context["columns"] = datasets.get_columns(dataset.name)
    except Exception:
        logger.exception("Could not read columns for %r", dataset.name)

    try:
        preview_columns, preview_rows = datasets.get_preview(dataset.name)
        context["preview_columns"] = preview_columns
        context["preview_rows"] = [
            [None if v is None else str(v) for v in row] for row in preview_rows
        ]
    except Exception:
        # Details to the log only — a preview failure must not leak the
        # connection string, same rule as report export.
        logger.exception("Preview failed for %r", dataset.name)
        context["preview_error"] = "Could not load a preview. Check the server logs."

    return templates.TemplateResponse(request, "dataset_detail.html", context)


@app.post("/query", response_class=HTMLResponse)
def run_query(
    request: Request,
    sql: str = Form(...),
    database: str = Form(...),
    user: str = Depends(require_login),
):
    context = _console_context(user, sql=sql, database=database)

    # Only accept a database the server actually reported (when we have a list).
    if context["databases"] and database not in context["databases"]:
        context["error"] = f"Unknown database: {database!r}."
        return templates.TemplateResponse(request, "console.html", context, status_code=400)

    try:
        result = db.execute(sql, database)
    except Exception as exc:
        # This is a trusted, login-gated internal console, so showing the real
        # DB error is the useful behavior (unlike report export).
        logger.exception("Ad-hoc query failed")
        context["error"] = f"Query failed: {exc}"
        return templates.TemplateResponse(request, "console.html", context, status_code=400)

    if result.returns_rows:
        shown = result.rows[:QUERY_DISPLAY_LIMIT]
        context["result"] = {
            "columns": result.columns,
            "rows": [[None if v is None else str(v) for v in row] for row in shown],
            "total": result.rowcount,
            "shown": len(shown),
            "truncated": result.rowcount > QUERY_DISPLAY_LIMIT,
        }
    else:
        context["message"] = f"OK — {result.rowcount} row(s) affected."
    return templates.TemplateResponse(request, "console.html", context)


@app.post("/query/export")
def export_query(
    request: Request,
    format: str = "csv",
    sql: str = Form(...),
    database: str = Form(...),
    user: str = Depends(require_login),
):
    def _error(msg: str, status: int = 400):
        context = _console_context(user, sql=sql, database=database, error=msg)
        return templates.TemplateResponse(request, "console.html", context, status_code=status)

    try:
        databases = db.list_databases()
    except Exception:
        databases = []
    if databases and database not in databases:
        return _error(f"Unknown database: {database!r}.")

    try:
        result = db.execute(sql, database)
    except Exception as exc:
        logger.exception("Ad-hoc query export failed")
        return _error(f"Query failed: {exc}")

    if not result.returns_rows:
        context = _console_context(
            user,
            sql=sql,
            database=database,
            message=f"OK — {result.rowcount} row(s) affected. Nothing to export.",
        )
        return templates.TemplateResponse(request, "console.html", context)

    return _file_response(result.to_dataframe(), format, "query")


@app.get("/report/{key}/export")
def export_report(
    request: Request,
    key: str,
    format: str = "csv",
    user: str = Depends(require_login),
):
    try:
        df = reports.get_report_df(key)
    except KeyError:
        context = _console_context(user, error=f"Unknown report: {key!r}.", active_tab="reports")
        return templates.TemplateResponse(request, "console.html", context, status_code=404)
    except Exception:
        # Full details go to the server log; the user sees a generic message so
        # we never leak connection strings or credentials into the browser.
        logger.exception("Report generation failed for %r", key)
        context = _console_context(
            user,
            error="Could not generate the report. Check the server logs.",
            active_tab="reports",
        )
        return templates.TemplateResponse(request, "console.html", context, status_code=502)

    return _file_response(df, format, key)


def _charts_unavailable(request: Request, user: str, status: int = 503):
    context = _shell_context(
        user,
        "charts",
        charts=[],
        db_ok=False,
        db_error=(
            "The app database is not configured, so charts can't be saved. "
            "Set APP_DB_PASSWORD (see .env.example)."
        ),
    )
    return templates.TemplateResponse(request, "charts.html", context, status_code=status)


@app.get("/charts", response_class=HTMLResponse)
def charts_index(request: Request, user: str = Depends(require_login)):
    if not store.available():
        return _charts_unavailable(request, user)
    context = _shell_context(user, "charts", charts=[])
    try:
        context["charts"] = store.list_charts()
    except Exception:
        logger.exception("Could not list charts")
        context["db_ok"] = False
        context["db_error"] = "Could not read saved charts — check the server logs."
    return templates.TemplateResponse(request, "charts.html", context)


def _builder_context(user: str, **extra) -> dict:
    """Context for the chart builder, including everything needed to re-render
    the form after a run so nothing the user typed is lost."""
    databases: list[str] = []
    try:
        databases = db.list_databases()
    except Exception:
        logger.exception("Could not list databases for the chart builder")
    context = _shell_context(
        user,
        "charts",
        databases=databases,
        chart_types=charts.CHART_TYPES,
        chart=None,
        sql="",
        source_db=settings.datasets_database,
        title="",
        chart_type="bar",
        x_column="",
        y_columns=[],
        columns=[],
        rows=[],
        numeric_columns=[],
        spec=None,
        ran=False,
        # Handed to the browser so the live preview re-colours from the same
        # validated palette instead of keeping a second copy of the hexes.
        series_colors=charts.SERIES_COLORS,
        max_series=charts.MAX_SERIES,
        max_points=charts.MAX_POINTS,
    )
    context.update(extra)
    return context


@app.get("/charts/new", response_class=HTMLResponse)
def chart_new(request: Request, user: str = Depends(require_login)):
    if not store.available():
        return _charts_unavailable(request, user)
    return templates.TemplateResponse(request, "chart_builder.html", _builder_context(user))


@app.post("/charts/new", response_class=HTMLResponse)
def chart_run(
    request: Request,
    sql: str = Form(...),
    source_db: str = Form(...),
    title: str = Form(""),
    chart_type: str = Form("bar"),
    x_column: str = Form(""),
    y_columns: list[str] = Form(default=[]),
    user: str = Depends(require_login),
):
    """Run the builder's SQL and re-render with the column pickers + preview."""
    if not store.available():
        return _charts_unavailable(request, user)

    context = _builder_context(
        user,
        sql=sql,
        source_db=source_db,
        title=title,
        chart_type=chart_type,
        x_column=x_column,
        y_columns=y_columns,
        ran=True,
    )

    try:
        result = db.execute(sql, source_db)
    except Exception as exc:
        # Same reasoning as the query console: this is a login-gated internal
        # tool, so the real database error is the useful thing to show.
        logger.exception("Chart query failed")
        context["error"] = f"Query failed: {exc}"
        return templates.TemplateResponse(request, "chart_builder.html", context, status_code=400)

    if not result.returns_rows:
        context["error"] = "That statement returned no rows to chart."
        return templates.TemplateResponse(request, "chart_builder.html", context, status_code=400)

    context["columns"] = result.columns
    context["rows"] = result.rows[: charts.MAX_POINTS]
    context["numeric_columns"] = charts.numeric_columns(result.columns, result.rows)

    # Sensible first guess: first column on x, first numeric column as the measure.
    if not x_column and result.columns:
        context["x_column"] = result.columns[0]
    if not y_columns and context["numeric_columns"]:
        first = context["numeric_columns"][0]
        # Don't measure the same column we're labelling by.
        if first == context["x_column"] and len(context["numeric_columns"]) > 1:
            first = context["numeric_columns"][1]
        context["y_columns"] = [first]

    context["spec"] = charts.build_spec(
        result.columns,
        result.rows,
        context["chart_type"],
        context["x_column"],
        context["y_columns"],
    )
    return templates.TemplateResponse(request, "chart_builder.html", context)


@app.post("/charts/save")
def chart_save(
    request: Request,
    sql: str = Form(...),
    source_db: str = Form(...),
    title: str = Form(...),
    chart_type: str = Form(...),
    x_column: str = Form(...),
    y_columns: list[str] = Form(default=[]),
    slug: str = Form(""),
    user: str = Depends(require_login),
):
    if not store.available():
        return _charts_unavailable(request, user)

    title = title.strip() or "Untitled chart"
    chart = store.Chart(
        slug=slug or store.unique_slug(title),
        title=title,
        source_db=source_db,
        sql=sql,
        chart_type=chart_type if chart_type in charts.CHART_TYPE_KEYS else "bar",
        x_column=x_column,
        y_columns=list(y_columns),
        created_by=user,
    )
    saved = store.save_chart(chart)
    logger.info("Chart %r saved by %s", saved.slug, user)
    return RedirectResponse(request.url_for("chart_detail", slug=saved.slug), status_code=303)


@app.get("/charts/{slug}", response_class=HTMLResponse)
def chart_detail(request: Request, slug: str, user: str = Depends(require_login)):
    if not store.available():
        return _charts_unavailable(request, user)

    try:
        chart = store.get_chart(slug)
    except Exception:
        logger.exception("Could not load chart %r", slug)
        return _charts_unavailable(request, user)
    if chart is None:
        context = _shell_context(user, "charts", charts=[], error=f"Unknown chart: {slug!r}.")
        return templates.TemplateResponse(request, "charts.html", context, status_code=404)

    context = _shell_context(
        user, "charts", chart=chart, spec=None, columns=[], rows=[], chart_error=None
    )
    try:
        result = db.execute(chart.sql, chart.source_db)
    except Exception:
        logger.exception("Chart %r failed to refresh", slug)
        context["chart_error"] = "This chart's query failed. Edit it, or check the server logs."
        return templates.TemplateResponse(request, "chart_detail.html", context)

    context["columns"] = result.columns
    context["rows"] = [
        [None if v is None else str(v) for v in row] for row in result.rows[: charts.MAX_POINTS]
    ]
    context["spec"] = charts.build_spec(
        result.columns, result.rows, chart.chart_type, chart.x_column, chart.y_columns
    )
    return templates.TemplateResponse(request, "chart_detail.html", context)


@app.post("/charts/{slug}/delete")
def chart_delete(request: Request, slug: str, user: str = Depends(require_login)):
    if store.available():
        store.delete_chart(slug)
        logger.info("Chart %r deleted by %s", slug, user)
    return RedirectResponse(request.url_for("charts_index"), status_code=303)


# ── dashboards ─────────────────────────────────────────────────────────────


def _dashboards_unavailable(request: Request, user: str, status: int = 503):
    context = _shell_context(
        user,
        "dashboards",
        dashboards=[],
        db_ok=False,
        db_error=(
            "The app database is not configured, so dashboards can't be saved. "
            "Set APP_DB_PASSWORD (see .env.example)."
        ),
    )
    return templates.TemplateResponse(request, "dashboards.html", context, status_code=status)


def _render_tiles(items: list) -> list[dict]:
    """Run each tile's query and build its spec.

    One query per tile, sequentially — fine for the handful of charts a
    dashboard holds, and a failing tile becomes an error card rather than
    taking down the whole page.
    """
    tiles = []
    for item in items:
        tile = {"item": item, "chart": item.chart, "spec": None, "error": None}
        try:
            result = db.execute(item.chart.sql, item.chart.source_db)
            tile["spec"] = charts.build_spec(
                result.columns,
                result.rows,
                item.chart.chart_type,
                item.chart.x_column,
                item.chart.y_columns,
            )
        except Exception:
            logger.exception("Dashboard tile %r failed", item.chart.slug)
            tile["error"] = "This chart's query failed."
        tiles.append(tile)
    return tiles


def _tile_specs(tiles: list[dict]) -> dict:
    """canvas id -> spec, for the page's single embedded payload.

    Built here rather than in the template: Jinja has no zero-based enumerate,
    and the ids must match what _tile.html renders.
    """
    return {f"tile-{i}": (t["spec"].to_dict() if t["spec"] else None) for i, t in enumerate(tiles)}


@app.get("/dashboards", response_class=HTMLResponse)
def dashboards_index(request: Request, user: str = Depends(require_login)):
    if not store.available():
        return _dashboards_unavailable(request, user)
    context = _shell_context(user, "dashboards", dashboards=[])
    try:
        context["dashboards"] = store.list_dashboards()
    except Exception:
        logger.exception("Could not list dashboards")
        context["db_ok"] = False
        context["db_error"] = "Could not read dashboards — check the server logs."
    return templates.TemplateResponse(request, "dashboards.html", context)


@app.post("/dashboards")
def dashboard_create(
    request: Request,
    title: str = Form(...),
    user: str = Depends(require_login),
):
    if not store.available():
        return _dashboards_unavailable(request, user)
    title = title.strip() or "Untitled dashboard"
    dash = store.save_dashboard(
        store.Dashboard(
            slug=store.unique_slug(
                title, exists=lambda s: store.get_dashboard(s, with_items=False)
            ),
            title=title,
            created_by=user,
        )
    )
    logger.info("Dashboard %r created by %s", dash.slug, user)
    # Straight into the editor: a new dashboard is empty and the next thing you
    # want is to add a chart.
    return RedirectResponse(request.url_for("dashboard_edit", slug=dash.slug), status_code=303)


def _load_dashboard(request: Request, user: str, slug: str):
    """Returns (dashboard, None) or (None, response) for the error path."""
    try:
        dash = store.get_dashboard(slug)
    except Exception:
        logger.exception("Could not load dashboard %r", slug)
        return None, _dashboards_unavailable(request, user)
    if dash is None:
        context = _shell_context(
            user, "dashboards", dashboards=[], error=f"Unknown dashboard: {slug!r}."
        )
        return None, templates.TemplateResponse(
            request, "dashboards.html", context, status_code=404
        )
    return dash, None


@app.get("/dashboards/{slug}", response_class=HTMLResponse)
def dashboard_show(request: Request, slug: str, user: str = Depends(require_login)):
    if not store.available():
        return _dashboards_unavailable(request, user)
    dash, failure = _load_dashboard(request, user, slug)
    if failure is not None:
        return failure
    tiles = _render_tiles(dash.items)
    context = _shell_context(
        user, "dashboards", dashboard=dash, tiles=tiles, tile_specs=_tile_specs(tiles)
    )
    return templates.TemplateResponse(request, "dashboard_show.html", context)


@app.get("/dashboards/{slug}/edit", response_class=HTMLResponse)
def dashboard_edit(request: Request, slug: str, user: str = Depends(require_login)):
    if not store.available():
        return _dashboards_unavailable(request, user)
    dash, failure = _load_dashboard(request, user, slug)
    if failure is not None:
        return failure

    available_charts = []
    try:
        available_charts = store.list_charts()
    except Exception:
        logger.exception("Could not list charts for the dashboard editor")

    tiles = _render_tiles(dash.items)
    context = _shell_context(
        user,
        "dashboards",
        dashboard=dash,
        tiles=tiles,
        tile_specs=_tile_specs(tiles),
        available_charts=available_charts,
        widths=store.WIDTHS,
    )
    return templates.TemplateResponse(request, "dashboard_edit.html", context)


@app.post("/dashboards/{slug}/items")
def dashboard_add_item(
    request: Request,
    slug: str,
    chart_slug: str = Form(...),
    width: str = Form(store.DEFAULT_WIDTH),
    user: str = Depends(require_login),
):
    if store.available():
        store.add_item(slug, chart_slug, width)
    return RedirectResponse(request.url_for("dashboard_edit", slug=slug), status_code=303)


@app.post("/dashboards/{slug}/items/{item_id}/remove")
def dashboard_remove_item(
    request: Request, slug: str, item_id: int, user: str = Depends(require_login)
):
    if store.available():
        store.remove_item(slug, item_id)
    return RedirectResponse(request.url_for("dashboard_edit", slug=slug), status_code=303)


@app.post("/dashboards/{slug}/items/{item_id}/width")
def dashboard_set_width(
    request: Request,
    slug: str,
    item_id: int,
    width: str = Form(...),
    user: str = Depends(require_login),
):
    if store.available():
        store.set_item_width(slug, item_id, width)
    return RedirectResponse(request.url_for("dashboard_edit", slug=slug), status_code=303)


@app.post("/dashboards/{slug}/items/{item_id}/move")
def dashboard_move_item(
    request: Request,
    slug: str,
    item_id: int,
    direction: str = Form(...),
    user: str = Depends(require_login),
):
    if store.available():
        store.move_item(slug, item_id, -1 if direction == "up" else 1)
    return RedirectResponse(request.url_for("dashboard_edit", slug=slug), status_code=303)


@app.post("/dashboards/{slug}/delete")
def dashboard_delete(request: Request, slug: str, user: str = Depends(require_login)):
    if store.available():
        store.delete_dashboard(slug)
        logger.info("Dashboard %r deleted by %s", slug, user)
    return RedirectResponse(request.url_for("dashboards_index"), status_code=303)


# ── admin panel ────────────────────────────────────────────────────────────


def require_admin(request: Request, user: str = Depends(require_login)) -> str:
    """Gate the admin screens.

    Checked against the database on every request rather than read from the
    session, so removing someone's admin takes effect at once instead of
    whenever they next log in.
    """
    if not store.available():
        raise HTTPException(status_code=503, detail="The app database is not configured")
    try:
        admin = store.is_admin(user)
    except Exception:
        logger.exception("Could not verify admin rights for %r", user)
        raise HTTPException(status_code=503, detail="Could not verify permissions") from None
    if not admin:
        raise HTTPException(status_code=403, detail="Administrators only")
    return user


@app.get("/admin")
def admin_index(request: Request, user: str = Depends(require_admin)):
    return RedirectResponse(request.url_for("admin_users"), status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: str = Depends(require_admin)):
    context = _shell_context(
        user,
        "admin",
        admin_tab="users",
        users=store.list_users(),
        roles=store.list_roles(),
        is_admin=True,
    )
    return templates.TemplateResponse(request, "admin_users.html", context)


@app.post("/admin/users/{username}/roles")
def admin_set_user_roles(
    request: Request,
    username: str,
    roles: list[str] = Form(default=[]),
    user: str = Depends(require_admin),
):
    store.set_user_roles(username, roles)
    logger.info("Roles for %r set to %r by %s", username, roles, user)
    return RedirectResponse(request.url_for("admin_users"), status_code=303)


@app.post("/admin/users/{username}/admin")
def admin_toggle_admin(
    request: Request,
    username: str,
    value: str = Form(...),
    user: str = Depends(require_admin),
):
    grant = value == "true"
    # Refuse to remove the last administrator — otherwise the panel becomes
    # unreachable and only a shell on the host can fix it.
    if not grant:
        admins = [u.username for u in store.list_users() if u.is_admin]
        if admins == [username]:
            context = _shell_context(
                user,
                "admin",
                admin_tab="users",
                users=store.list_users(),
                roles=store.list_roles(),
                is_admin=True,
                error=(
                    f"{username} is the only administrator — grant admin to "
                    "someone else before removing it."
                ),
            )
            return templates.TemplateResponse(request, "admin_users.html", context, status_code=400)
    store.set_user_admin(username, grant)
    logger.info("Admin for %r set to %s by %s", username, grant, user)
    return RedirectResponse(request.url_for("admin_users"), status_code=303)


@app.post("/admin/users/{username}/active")
def admin_toggle_active(
    request: Request,
    username: str,
    value: str = Form(...),
    user: str = Depends(require_admin),
):
    store.set_user_active(username, value == "true")
    return RedirectResponse(request.url_for("admin_users"), status_code=303)


@app.get("/admin/roles", response_class=HTMLResponse)
def admin_roles(request: Request, user: str = Depends(require_admin)):
    # Every database on the instance, so grants are picked from a list rather
    # than typed. This is the one place the full list is still shown, and it is
    # admin-only by construction.
    live: list[str] = []
    try:
        live = db.list_databases()
    except Exception:
        logger.exception("Could not list databases for the roles screen")

    roles = store.list_roles()
    # Union in every existing grant. A grant naming a database that is no
    # longer on the instance would otherwise have no checkbox — and since the
    # form posts the full set, saving that role would silently drop it.
    granted = {name for r in roles for name in r.databases}
    all_databases = sorted(set(live) | granted)
    context = _shell_context(
        user,
        "admin",
        admin_tab="roles",
        roles=roles,
        all_databases=all_databases,
        # Flagged in the UI so a stale grant is visible rather than mysterious.
        missing_databases=sorted(granted - set(live)) if live else [],
        is_admin=True,
    )
    return templates.TemplateResponse(request, "admin_roles.html", context)


@app.post("/admin/roles")
def admin_create_role(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    user: str = Depends(require_admin),
):
    if store.create_role(name, description) is None:
        logger.info("Role %r already exists (requested by %s)", name, user)
    return RedirectResponse(request.url_for("admin_roles"), status_code=303)


@app.post("/admin/roles/{role_id}/databases")
def admin_set_role_databases(
    request: Request,
    role_id: int,
    databases: list[str] = Form(default=[]),
    user: str = Depends(require_admin),
):
    store.set_role_databases(role_id, databases)
    logger.info("Databases for role %s set to %r by %s", role_id, databases, user)
    return RedirectResponse(request.url_for("admin_roles"), status_code=303)


@app.post("/admin/roles/{role_id}/default")
def admin_set_role_default(
    request: Request,
    role_id: int,
    value: str = Form(...),
    user: str = Depends(require_admin),
):
    store.set_role_default(role_id, value == "true")
    return RedirectResponse(request.url_for("admin_roles"), status_code=303)


@app.post("/admin/roles/{role_id}/delete")
def admin_delete_role(request: Request, role_id: int, user: str = Depends(require_admin)):
    store.delete_role(role_id)
    logger.info("Role %s deleted by %s", role_id, user)
    return RedirectResponse(request.url_for("admin_roles"), status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
