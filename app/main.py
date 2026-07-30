import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuthError
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db, oidc, reports, superset_session, users
from .auth import NotAuthenticated, end_session, get_current_user, require_login, start_session
from .config import get_settings

# uvicorn only configures its own loggers and leaves root at WARNING, so our
# logger.info() calls were being dropped. Give root a handler at INFO.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("report_hub")
settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

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
        settings.superset_auth_enabled
        or settings.sso_enabled
        or settings.password_login_enabled
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
        return RedirectResponse(request.url_for("dashboard"), status_code=303)

    if settings.superset_auth_enabled:
        # Same origin as Superset, so its cookie is already on this request.
        cookie = request.cookies.get(settings.superset_cookie_name)
        if cookie:
            result = await superset_session.identify(cookie)
            if result:
                username = result["username"]
                start_session(
                    request,
                    username,
                    via="superset",
                    claims=superset_session.claims_from_result(result),
                )
                logger.info("Superset-delegated login: %s", username)
                return RedirectResponse(
                    request.url_for("dashboard"), status_code=303
                )
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
    logger.info("SSO login: %s", username)
    return RedirectResponse(request.url_for("dashboard"), status_code=303)


@app.get("/login/local", response_class=HTMLResponse)
def login_local_form(request: Request):
    """Break-glass password form. Only mounted when AUTH_MODE allows it."""
    if not settings.password_login_enabled:
        raise HTTPException(status_code=404, detail="Password login is disabled")
    if get_current_user(request):
        return RedirectResponse(request.url_for("dashboard"), status_code=303)
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
        return RedirectResponse(request.url_for("dashboard"), status_code=303)
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
            post_logout = settings.sso_logout_redirect_uri or str(
                request.url_for("login_form")
            )
            params = {
                "client_id": settings.sso_client_id,
                "post_logout_redirect_uri": post_logout,
            }
            return RedirectResponse(
                f"{end_session_endpoint}?{urlencode(params)}", status_code=303
            )

    return RedirectResponse(request.url_for("login_form"), status_code=303)


def _dashboard_context(user: str, **extra) -> dict:
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

    context = {
        "user": user,
        "title": settings.app_title,
        "error": None,
        "message": None,
        "sql": None,
        "database": settings.db_name,
        "databases": databases,
        "db_host": settings.db_host,
        "db_ok": db_ok,
        "db_error": db_error,
        "reports": report_list,
        "result": None,
        "active_tab": "query",
    }
    context.update(extra)
    return context


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context(user))


@app.post("/query", response_class=HTMLResponse)
def run_query(
    request: Request,
    sql: str = Form(...),
    database: str = Form(...),
    user: str = Depends(require_login),
):
    context = _dashboard_context(user, sql=sql, database=database)

    # Only accept a database the server actually reported (when we have a list).
    if context["databases"] and database not in context["databases"]:
        context["error"] = f"Unknown database: {database!r}."
        return templates.TemplateResponse(
            request, "dashboard.html", context, status_code=400
        )

    try:
        result = db.execute(sql, database)
    except Exception as exc:
        # This is a trusted, login-gated internal console, so showing the real
        # DB error is the useful behavior (unlike report export).
        logger.exception("Ad-hoc query failed")
        context["error"] = f"Query failed: {exc}"
        return templates.TemplateResponse(
            request, "dashboard.html", context, status_code=400
        )

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
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.post("/query/export")
def export_query(
    request: Request,
    format: str = "csv",
    sql: str = Form(...),
    database: str = Form(...),
    user: str = Depends(require_login),
):
    def _error(msg: str, status: int = 400):
        context = _dashboard_context(user, sql=sql, database=database, error=msg)
        return templates.TemplateResponse(
            request, "dashboard.html", context, status_code=status
        )

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
        context = _dashboard_context(
            user,
            sql=sql,
            database=database,
            message=f"OK — {result.rowcount} row(s) affected. Nothing to export.",
        )
        return templates.TemplateResponse(request, "dashboard.html", context)

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
        context = _dashboard_context(
            user, error=f"Unknown report: {key!r}.", active_tab="reports"
        )
        return templates.TemplateResponse(
            request, "dashboard.html", context, status_code=404
        )
    except Exception:
        # Full details go to the server log; the user sees a generic message so
        # we never leak connection strings or credentials into the browser.
        logger.exception("Report generation failed for %r", key)
        context = _dashboard_context(
            user,
            error="Could not generate the report. Check the server logs.",
            active_tab="reports",
        )
        return templates.TemplateResponse(
            request, "dashboard.html", context, status_code=502
        )

    return _file_response(df, format, key)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
