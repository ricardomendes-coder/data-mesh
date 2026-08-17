import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuthError
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.middleware.sessions import SessionMiddleware

from . import charts, datasets, db, filters, i18n, oidc, reports, store, superset_session, users
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


def _markdown_lite(value: str) -> Markup:
    """The small slice of Markdown that dashboard text blocks actually use.

    Superset's text tiles are Markdown, and importing them raw left `###` and
    `**bold**` on screen as literal characters. This handles headings, bold,
    italics, links and paragraphs — not a Markdown implementation, just the
    parts that appear in these dashboards.

    Escaped first, so imported text can never inject markup: only the tags
    added afterwards are live, and links are restricted to http(s) so no
    `javascript:` URL can come through.
    """
    text_value = str(escape(value or "")).strip()
    if not text_value:
        return Markup("")

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if not re.match(r"^https?://", url):
            return label
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'

    out = []
    for block in re.split(r"\n\s*\n", text_value):
        block = block.strip()
        if not block:
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", block, flags=re.S)
        if heading:
            level = min(4, len(heading.group(1)) + 1)  # h1 is the page title
            body = heading.group(2).replace("\n", " ")
            out.append(f"<h{level}>{body}</h{level}>")
            continue
        if re.match(r"^-{3,}$", block):
            out.append("<hr>")
            continue
        out.append("<p>" + block.replace("\n", "<br>") + "</p>")

    html = "".join(out)
    html = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", html)
    html = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", html)
    html = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, html)
    return Markup(html)


# `_` rather than `t`: _tile.html's macro takes a parameter called `t`,
# and a global of the same name is shadowed inside it.
templates.env.globals["_"] = i18n.t
templates.env.globals["locales"] = i18n.LOCALE_NAMES
templates.env.globals["current_locale"] = i18n.get_locale

# Charts store their SQL with the {{ filters }} token in it. Anywhere the SQL
# is *shown* — the SQL panel, the "open in Query" link — it should be the SQL
# that actually runs, so the token comes out.
templates.env.filters["strip_filters"] = filters.strip_token
templates.env.filters["inline_code"] = _inline_code
templates.env.filters["markdown_lite"] = _markdown_lite


def _row_limit(requested: str | int | None) -> int:
    """Clamp a requested row count into [1, QUERY_MAX_ROWS].

    Anything unparseable falls back to the default rather than erroring — the
    box is a convenience, and a typo shouldn't lose the query you just wrote.
    """
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        return settings.query_default_rows
    return max(1, min(wanted, settings.query_max_rows))


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


@app.middleware("http")
async def set_request_locale(request: Request, call_next):
    """Pin the interface language for the duration of one request.

    Order matters: an explicit ?lang= wins (so a link can be shared in a given
    language), then the session, then the configured default. The value lands in
    a ContextVar, which is what lets a message produced deep in charts.py come
    back translated without every function taking a locale argument.
    """
    chosen = request.query_params.get("lang")
    if not chosen:
        # SessionMiddleware is added after this one, so it wraps it and the
        # session is populated by the time we get here. Guarded anyway: a route
        # mounted outside the session stack would otherwise 500 on language.
        try:
            chosen = request.session.get("lang")
        except (AssertionError, KeyError):
            chosen = None
    i18n.set_locale(chosen or settings.default_locale)
    return await call_next(request)


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
        # Their language choice follows them to a new browser: the session is
        # fresh here, so seed it from what they picked last time.
        stored = store.get_user_locale(username)
        if stored:
            request.session["lang"] = i18n.set_locale(stored)
    except Exception:
        logger.exception("Could not record the login for %r", username)


def signed_in_user(request: Request, user: str = Depends(require_login)) -> str:
    """require_login, plus a one-time admin-flag lookup for the sidebar.

    The flag is normally set at login, but sessions created before this feature
    existed have no flag at all — and an admin granted mid-session would
    otherwise have to sign out and back in before the Admin link appeared. The
    lookup runs once per session, then the cached value is used.

    This only controls whether the link is *drawn*. Every admin route checks the
    database itself, so a stale or forged flag grants nothing.
    """
    if "is_admin" not in request.session and store.available():
        try:
            request.session["is_admin"] = store.is_admin(user)
        except Exception:
            logger.exception("Could not resolve admin status for %r", user)
            request.session["is_admin"] = False
    return user


def access_for(request: Request, user: str = Depends(signed_in_user)) -> store.Access:
    """The caller's effective permissions, resolved once for this request.

    When the app database isn't configured there are no roles to consult, so
    everything is permitted — otherwise losing `report_hub` would take the whole
    app down rather than just its own features. That trade is deliberate: the
    app database is where permissions live, and a permission system that fails
    *open* on its own outage is the lesser evil for an internal tool. Revisit if
    this is ever exposed beyond the VPN.
    """
    if not store.available():
        return store.Access(username=user, everything=True)
    try:
        return store.access_for(user)
    except Exception:
        logger.exception("Could not resolve permissions for %r", user)
        return store.Access(username=user, everything=True)


def _may_browse_datasets(access: store.Access) -> bool:
    """The catalog needs both the feature and a grant on the database it reads.

    Two checks because they answer different questions: may you use the catalog
    at all, and may you see *this* warehouse. Granting the feature without the
    database would otherwise list every table in `analytics`.
    """
    return access.allows(store.FEATURE, "dataset_catalog") and access.allows(
        store.DATABASE, settings.datasets_database
    )


def _resolve(table: str, ident: str) -> str:
    """Path parameter -> slug, for routes addressed by id.

    Declared as a dependency so every route gets it without repeating the
    lookup: FastAPI binds the path parameter into this, and the route receives
    the slug that the rest of the code — and every permission grant — speaks in.

    An unknown id 404s here rather than falling through as a slug that happens
    to be numeric and matches nothing.
    """
    if not store.available():
        return ident  # the route's own db_ok handling reports this properly
    try:
        slug = store.slug_for(table, ident)
    except Exception:
        logger.exception("Could not resolve %s %r", table, ident)
        return ident
    if slug is None:
        raise HTTPException(status_code=404, detail="Not found")
    return slug


def dashboard_ref(slug: str) -> str:
    return _resolve("dashboards", slug)


def chart_ref(slug: str) -> str:
    return _resolve("charts", slug)


def _back_to(request: Request, route: str, table: str, slug: str) -> RedirectResponse:
    """Redirect to a page's canonical id URL after a POST.

    The handlers work in slugs; the URLs are ids. Without this the address bar
    would flip back to a slug on every save.
    """
    ident = slug
    if store.available():
        try:
            ident = store.id_for(table, slug) or slug
        except Exception:
            logger.exception("Could not resolve the id for %s %r", table, slug)
    return RedirectResponse(request.url_for(route, slug=ident), status_code=303)


def _forbidden(
    request: Request,
    user: str,
    message: str,
    status: int = 403,
    access: store.Access | None = None,
):
    """A refusal that looks like the rest of the app rather than a bare 403.

    Pass `access` wherever it's in scope: without it the sidebar falls back to
    showing everything, which on this page of all pages reads as a list of what
    you were just told you can't have.
    """
    context = _shell_context(user, "", access, error=message)
    return templates.TemplateResponse(request, "forbidden.html", context, status_code=status)


# ── folder helpers — parked, not dead ──────────────────────────────────────
#
# Nothing calls these two right now: the folder UI was built, judged confusing,
# and unwired on purpose. The schema, the store API and these helpers are kept
# so bringing it back is a template change rather than a rewrite. Deleting them
# as "unused" is the one thing that would make that expensive, hence this note.
# See the folders section further down for the full picture.


def _folders() -> list[store.Folder]:
    """Every folder, or none if they can't be read. Never fatal: folders are
    decoration, so losing them costs the headings and nothing else."""
    if not store.available():
        return []
    try:
        return store.list_folders()
    except Exception:
        logger.exception("Could not list folders")
        return []


def _grouped(items: list, folders: list[store.Folder]) -> list[tuple]:
    """Group items under their folders, ungrouped last, empty folders dropped.

    Takes the items the viewer may *already* see, never the full set. Grouping
    runs strictly after permission filtering, so the most a folder can do is
    decide which heading something appears under.

    Empty folders are dropped rather than rendered as empty headings. That is
    not tidiness: a folder name is itself information ("Whirlpool renegotiation"),
    and showing one to someone who may see nothing inside it would leak exactly
    what the permission filter just removed.
    """
    known = {f.id for f in folders}
    by_folder: dict[int, list] = {}
    loose: list = []
    for item in items:
        fid = getattr(item, "folder_id", None)
        # An unknown folder_id falls through to ungrouped rather than being
        # dropped. It matters because _folders() fails open to an empty list:
        # if the folder read breaks while the chart read succeeds, every filed
        # chart would otherwise vanish from the page. Losing folders may cost
        # the headings; it must never cost the content.
        (by_folder.setdefault(fid, []) if fid in known else loose).append(item)

    groups = [(f, by_folder[f.id]) for f in folders if by_folder.get(f.id)]
    if loose:
        groups.append((None, loose))
    return groups


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


def _shell_context(user: str, active_nav: str, access: store.Access | None = None, **extra) -> dict:
    """Context every signed-in page needs — the bits _shell.html renders.

    `access` drives which nav entries exist at all. Pages that don't resolve it
    (the admin screens, which are admin-only anyway) pass None and get the full
    nav — the routes behind each entry still enforce for themselves, so a shown
    link is never itself a grant.
    """
    nav = access or store.Access(username=user, everything=True)
    context = {
        # Nav visibility. Asking "has any grant of this type" rather than for a
        # specific key, since the nav points at a list page.
        "nav_query": nav.allows(store.FEATURE, "sql_console"),
        "nav_reports": nav.has_any(store.REPORT),
        "nav_charts": nav.has_any(store.CHART) or nav.allows(store.FEATURE, "chart_builder"),
        "nav_dashboards": (
            nav.has_any(store.DASHBOARD) or nav.allows(store.FEATURE, "dashboard_builder")
        ),
        "nav_datasets": nav.allows(store.FEATURE, "dataset_catalog"),
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


def _console_context(user: str, access: store.Access | None = None, **extra) -> dict:
    """Base template context: the DB picker list and the reports manifest.

    Both DB discovery and manifest parsing are best-effort — a failure degrades
    the page (banner + empty list) instead of 500ing the whole dashboard.

    Everything the picker and the report list show is filtered through `access`,
    so a database or report the caller has no grant for is never even named.
    """
    access = access or store.NO_ACCESS
    databases: list[str] = []
    db_ok = False
    db_error = None
    try:
        # Filtered, not merely un-selectable: the full list of ~50 databases is
        # itself information (client names), so an ungranted one is not shown.
        databases = access.filter(store.DATABASE, db.list_databases())
        db_ok = True
    except Exception:
        logger.exception("Could not list databases")
        db_error = "Could not load the database list — check the server logs."

    try:
        report_list = [r for r in reports.load_reports() if access.allows(store.REPORT, r.key)]
    except Exception:
        logger.exception("Could not load the reports manifest")
        report_list = []
        db_error = db_error or "Could not load the reports — check the server logs."

    active_tab = extra.pop("active_tab", "query")
    # Preselect something the user can actually use.
    default_db = (
        settings.db_name if settings.db_name in databases else (databases[0] if databases else "")
    )
    context = _shell_context(
        user,
        "reports" if active_tab == "reports" else "query",
        access,
        sql=None,
        database=default_db,
        databases=databases,
        db_ok=db_ok,
        db_error=db_error,
        reports=report_list,
        result=None,
        active_tab=active_tab,
        can_query=access.allows(store.FEATURE, "sql_console"),
        limit=settings.query_default_rows,
        max_rows=settings.query_max_rows,
    )
    context.update(extra)
    return context


@app.get("/", response_class=HTMLResponse)
def console(
    request: Request,
    tab: str = "query",
    sql: str = "",
    database: str = "",
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """The query console and reports.

    `tab`/`sql`/`database` make the console deep-linkable, which is what the
    "Open in Query" buttons on a dataset page use to hand a query over.
    """
    context = _console_context(user, access, active_tab="reports" if tab == "reports" else "query")
    if sql:
        context["sql"] = sql
    # A deep link can name any database; only honour it if it's actually granted.
    if database and access.allows(store.DATABASE, database):
        context["database"] = database
    return templates.TemplateResponse(request, "console.html", context)


@app.get("/datasets", response_class=HTMLResponse)
def datasets_index(
    request: Request,
    q: str = "",
    kind: str = "",
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Everything in the analytics schema, grouped by the manifest's folders."""
    if not _may_browse_datasets(access):
        return _forbidden(
            request, user, i18n.t("You don't have access to the dataset catalog."), access=access
        )
    context = _shell_context(user, "datasets", access, q=q, kind=kind, groups=[], total=0, shown=0)
    try:
        all_datasets = datasets.list_datasets()
    except Exception:
        logger.exception("Could not read the dataset catalog")
        context["db_ok"] = False
        context["db_error"] = "Could not read the dataset catalog — check the server logs."
        return templates.TemplateResponse(request, "datasets.html", context, status_code=502)

    # Per-dataset grants. Filtered rather than merely un-clickable: a table
    # name is itself information, so an ungranted dataset is never listed.
    all_datasets = [d for d in all_datasets if access.allows(store.DATASET, d.name)]

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
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Preview, catalog, description and example queries for one dataset."""
    if not _may_browse_datasets(access):
        return _forbidden(
            request, user, i18n.t("You don't have access to the dataset catalog."), access=access
        )
    # 403 before the catalog lookup, so an ungranted name can't be probed for
    # existence by telling 403 apart from 404.
    if not access.allows(store.DATASET, name):
        return _forbidden(
            request, user, i18n.t("You don't have access to that dataset."), access=access
        )

    def _catalog_failure(message: str, status: int):
        context = _shell_context(
            user,
            "datasets",
            access,
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
        access,
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
    limit: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    # Checked server-side, not just hidden in the UI: the dropdown being
    # filtered means nothing when the target is a posted form field.
    if not access.allows(store.FEATURE, "sql_console"):
        return _forbidden(
            request, user, i18n.t("You don't have access to the query console."), access=access
        )
    if not access.allows(store.DATABASE, database):
        logger.warning("%s attempted a query against ungranted database %r", user, database)
        return _forbidden(request, user, f"You don't have access to {database!r}.", access=access)

    max_rows = _row_limit(limit)
    context = _console_context(user, access, sql=sql, database=database, limit=max_rows)

    # Only accept a database the server actually reported (when we have a list).
    if context["databases"] and database not in context["databases"]:
        context["error"] = f"Unknown database: {database!r}."
        return templates.TemplateResponse(request, "console.html", context, status_code=400)

    try:
        result = db.execute(sql, database, max_rows=max_rows)
    except Exception as exc:
        # This is a trusted, login-gated internal console, so showing the real
        # DB error is the useful behavior (unlike report export).
        logger.exception("Ad-hoc query failed")
        context["error"] = f"Query failed: {exc}"
        return templates.TemplateResponse(request, "console.html", context, status_code=400)

    if result.returns_rows:
        # Two separate caps. `max_rows` bounded what left the database and is
        # what Export gets; `query_display_rows` bounds what we send to the
        # browser, because 100k rows of HTML locks the tab.
        shown = result.rows[: settings.query_display_rows]
        context["result"] = {
            "columns": result.columns,
            "rows": [[None if v is None else str(v) for v in row] for row in shown],
            "total": result.rowcount,
            "shown": len(shown),
            "display_capped": result.rowcount > settings.query_display_rows,
            "hit_limit": result.truncated,
            "limit": max_rows,
            "page_size": settings.query_page_size,
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
    limit: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    # Export is a second way to run the same SQL, so it needs the same gate —
    # enforcing only on /query would leave the door open here.
    if not access.allows(store.FEATURE, "sql_console"):
        return _forbidden(
            request, user, i18n.t("You don't have access to the query console."), access=access
        )
    if not access.allows(store.DATABASE, database):
        logger.warning("%s attempted an export from ungranted database %r", user, database)
        return _forbidden(request, user, f"You don't have access to {database!r}.", access=access)

    def _error(msg: str, status: int = 400):
        context = _console_context(user, access, sql=sql, database=database, error=msg)
        return templates.TemplateResponse(request, "console.html", context, status_code=status)

    try:
        databases = db.list_databases()
    except Exception:
        databases = []
    if databases and database not in databases:
        return _error(f"Unknown database: {database!r}.")

    try:
        result = db.execute(sql, database, max_rows=_row_limit(limit))
    except Exception as exc:
        logger.exception("Ad-hoc query export failed")
        return _error(f"Query failed: {exc}")

    if not result.returns_rows:
        context = _console_context(
            user,
            access,
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
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    # A report is a stored query against a named database, so it needs its own
    # grant — being able to run it is not implied by console access.
    if not access.allows(store.REPORT, key):
        logger.warning("%s attempted ungranted report %r", user, key)
        return _forbidden(
            request, user, i18n.t("You don't have access to that report."), access=access
        )
    try:
        df = reports.get_report_df(key, max_rows=settings.query_max_rows)
    except KeyError:
        context = _console_context(
            user, access, error=f"Unknown report: {key!r}.", active_tab="reports"
        )
        return templates.TemplateResponse(request, "console.html", context, status_code=404)
    except Exception:
        # Full details go to the server log; the user sees a generic message so
        # we never leak connection strings or credentials into the browser.
        logger.exception("Report generation failed for %r", key)
        context = _console_context(
            user,
            access,
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
def charts_index(
    request: Request,
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _charts_unavailable(request, user)
    context = _shell_context(
        user,
        "charts",
        access,
        charts=[],
        can_build=access.allows(store.FEATURE, "chart_builder"),
        **_listing_controls(request, store.CHART),
    )
    try:
        # Filtered by slug: a chart you can't open shouldn't be listed either.
        visible = [c for c in store.list_charts() if access.allows(store.CHART, c.slug)]
        context["charts"] = _apply_tag_filter(visible, store.CHART, context["tag"])
        context["tags_map"] = _tags_of(store.CHART, [c.slug for c in context["charts"]])
    except Exception:
        logger.exception("Could not list charts")
        context["db_ok"] = False
        context["db_error"] = "Could not read saved charts — check the server logs."
    return templates.TemplateResponse(request, "charts.html", context)


def _builder_context(user: str, access: store.Access | None = None, **extra) -> dict:
    """Context for the chart builder, including everything needed to re-render
    the form after a run so nothing the user typed is lost."""
    access = access or store.NO_ACCESS
    databases: list[str] = []
    try:
        databases = access.filter(store.DATABASE, db.list_databases())
    except Exception:
        logger.exception("Could not list databases for the chart builder")
    context = _shell_context(
        user,
        "charts",
        access,
        databases=databases,
        chart_types=charts.CHART_TYPES,
        chart=None,
        sql="",
        source_db=(
            settings.datasets_database
            if settings.datasets_database in databases
            else (databases[0] if databases else "")
        ),
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
def chart_new(
    request: Request,
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _charts_unavailable(request, user)
    if not access.allows(store.FEATURE, "chart_builder"):
        return _forbidden(
            request, user, i18n.t("You don't have access to the chart builder."), access=access
        )
    return templates.TemplateResponse(request, "chart_builder.html", _builder_context(user, access))


@app.post("/charts/new", response_class=HTMLResponse)
def chart_run(
    request: Request,
    sql: str = Form(...),
    source_db: str = Form(...),
    title: str = Form(""),
    chart_type: str = Form("bar"),
    x_column: str = Form(""),
    y_columns: list[str] = Form(default=[]),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Run the builder's SQL and re-render with the column pickers + preview."""
    if not store.available():
        return _charts_unavailable(request, user)
    if not access.allows(store.FEATURE, "chart_builder"):
        return _forbidden(
            request, user, i18n.t("You don't have access to the chart builder."), access=access
        )
    # The builder runs arbitrary SQL, so it needs the same database gate as the
    # console — otherwise it is a way around it.
    if not access.allows(store.DATABASE, source_db):
        logger.warning("%s attempted a chart query against ungranted %r", user, source_db)
        return _forbidden(request, user, f"You don't have access to {source_db!r}.", access=access)

    context = _builder_context(
        user,
        access,
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
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _charts_unavailable(request, user)
    if not access.allows(store.FEATURE, "chart_builder"):
        return _forbidden(
            request, user, i18n.t("You don't have access to the chart builder."), access=access
        )
    if not access.allows(store.DATABASE, source_db):
        return _forbidden(request, user, f"You don't have access to {source_db!r}.", access=access)

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
    # The query changed, so the cached preview is of the old one.
    try:
        if saved.id:
            store.drop_chart_preview(saved.id)
    except Exception:
        logger.exception("Could not drop the cached preview for %r", saved.slug)
    logger.info("Chart %r saved by %s", saved.slug, user)
    return RedirectResponse(request.url_for("chart_detail", slug=saved.id), status_code=303)


@app.get("/charts/{slug}/data")
def chart_data(
    request: Request,
    slug: str = Depends(chart_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """One chart's rendered spec, for the listing's preview mode.

    Same shape as the dashboard tile endpoint and drawn by the same JS. It's a
    separate fetch per card so a listing of forty charts paints immediately and
    fills in, rather than waiting on forty queries.
    """
    if not store.available():
        return JSONResponse(
            {"error": i18n.t("The app database is not configured.")}, status_code=503
        )
    if not access.allows(store.CHART, slug):
        return JSONResponse(
            {"error": i18n.t("You don't have access to that chart.")}, status_code=403
        )
    try:
        chart = store.get_chart(slug)
    except Exception:
        logger.exception("Could not load chart %r", slug)
        return JSONResponse({"error": i18n.t("This chart's query failed.")}, status_code=502)
    if chart is None:
        return JSONResponse({"error": i18n.t("Not found")}, status_code=404)

    # Served from cache when it's fresh. A preview exists to tell one chart from
    # another; re-running 580 queries so each thumbnail is to-the-second is a
    # cost nobody asked for. `?refresh=1` forces a rebuild.
    ttl = timedelta(minutes=max(0, settings.preview_cache_minutes))
    force = request.query_params.get("refresh") == "1"
    if not force and ttl:
        try:
            cached = store.get_chart_preview(chart.id)
        except Exception:
            logger.exception("Could not read the cached preview for %r", slug)
            cached = None
        if cached:
            payload, built_at = cached
            fresh = datetime.now(built_at.tzinfo) - built_at < ttl
            # A chart edited since the snapshot invalidates it: the query moved.
            unchanged = not chart.updated_at or chart.updated_at <= built_at
            if fresh and unchanged:
                payload["age"] = _age_label(built_at)
                payload["cached"] = True
                return JSONResponse(payload)

    try:
        sql = filters.strip_token(chart.sql)
        result = db.execute(sql, chart.source_db, max_rows=charts.MAX_POINTS + 1)
        spec = charts.build_spec(
            result.columns, result.rows, chart.chart_type, chart.x_column, chart.y_columns
        )
    except Exception:
        logger.exception("Chart %r failed to render for the listing", slug)
        return JSONResponse({"error": i18n.t("This chart's query failed.")}, status_code=200)

    payload = {"renders_as": spec.renders_as, "warnings": spec.warnings, "unfiltered": False}
    if spec.renders_as == "canvas":
        payload["spec"] = spec.to_dict()
    elif spec.renders_as == "table":
        payload["columns"] = spec.columns
        payload["rows"] = [["" if v is None else str(v) for v in r] for r in spec.rows[:12]]
    else:
        payload["value"] = spec.value
        payload["caption"] = spec.caption

    # An empty result is far more often a moment than a fact — an ETL that
    # truncates and reloads leaves its tables briefly empty, and caching that
    # snapshot pins "no data" on the card for the whole TTL. Serve it, don't
    # keep it; the next look asks the warehouse again.
    empty = (spec.renders_as == "table" and not payload["rows"]) or (
        spec.renders_as == "canvas" and not (payload["spec"].get("labels") or [])
    )
    if empty:
        try:
            store.drop_chart_preview(chart.id)
        except Exception:
            logger.exception("Could not drop the stale preview for %r", slug)
    else:
        try:
            store.put_chart_preview(chart.id, payload)
        except Exception:
            logger.exception("Could not cache the preview for %r", slug)
    payload["age"] = i18n.t("just now")
    payload["cached"] = False
    return JSONResponse(payload)


@app.get("/charts/{slug}", response_class=HTMLResponse)
def chart_detail(
    request: Request,
    slug: str = Depends(chart_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _charts_unavailable(request, user)
    # 403 before the lookup, so an ungranted slug can't be probed for existence.
    if not access.allows(store.CHART, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to that chart."), access=access
        )

    try:
        chart = store.get_chart(slug)
    except Exception:
        logger.exception("Could not load chart %r", slug)
        return _charts_unavailable(request, user)
    if chart is None:
        context = _shell_context(
            user, "charts", access, charts=[], error=f"Unknown chart: {slug!r}."
        )
        return templates.TemplateResponse(request, "charts.html", context, status_code=404)

    context = _shell_context(
        user, "charts", access, chart=chart, spec=None, columns=[], rows=[], chart_error=None
    )
    try:
        # The stored SQL carries the {{ filters }} token, which is Report Hub's
        # and not SQL. The dashboard path substitutes it with the active
        # filters; here there are none, so it comes out.
        result = db.execute(filters.strip_token(chart.sql), chart.source_db)
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
def chart_delete(
    request: Request,
    slug: str = Depends(chart_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not (access.allows(store.CHART, slug) and access.allows(store.FEATURE, "chart_builder")):
        return _forbidden(
            request, user, i18n.t("You don't have access to that chart."), access=access
        )
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


def _render_tiles(items: list, active: list | None = None) -> list[dict]:
    """Run each tile's query and build its spec.

    One query per tile, sequentially — fine for the handful of charts a
    dashboard holds, and a failing tile becomes an error card rather than
    taking down the whole page.

    `active` are the dashboard's filters with the viewer's current choices. A
    chart whose SQL has no {{ filters }} token runs unfiltered, and the tile
    says so rather than pretending the filter applied.
    """
    active = active or []
    tiles = []
    for index, item in enumerate(items):
        # The canvas id is fixed here rather than derived from the loop in the
        # template: with tabs the tiles are rendered in groups, so a per-group
        # loop index would no longer match the keys in the embedded payload.
        tile = {
            "item": item,
            "chart": item.chart,
            "spec": None,
            "error": None,
            "canvas_id": f"tile-{index}",
        }
        # A text tile carries its own content and runs nothing.
        if getattr(item, "is_text", False) or item.chart is None:
            tiles.append(tile)
            continue
        # Charts that can't take a filter are flagged, so a filtered dashboard
        # never quietly mixes filtered and unfiltered numbers.
        tile["unfiltered"] = bool(
            active
            and any(f.is_set and f.scopes(item.chart.slug) for f in active)
            and not filters.accepts_filters(item.chart.sql)
        )
        try:
            sql, params = filters.apply(item.chart.sql, active, item.chart.slug)
            result = db.execute(sql, item.chart.source_db, params=params)
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


# How a listing can be shown. `box` is the card that already existed; `preview`
# renders the chart itself; `list` is a dense row per item.
VIEWS = ("preview", "box", "list")
DEFAULT_VIEW = "box"


def _age_label(built_at) -> str:
    """How stale a cached preview is, in words.

    Always shown next to the preview: a chart's numbers move when the data
    moves, so a cached spec is a likeness, not a reading. Saying how old it is
    costs one line and stops it being mistaken for live.
    """
    if built_at is None:
        return ""
    now = datetime.now(built_at.tzinfo) if built_at.tzinfo else datetime.now()
    minutes = max(0, int((now - built_at).total_seconds() // 60))
    if minutes < 1:
        return i18n.t("just now")
    if minutes < 60:
        return i18n.t("{n} min ago", n=minutes)
    return i18n.t("{n} h ago", n=minutes // 60)


def _listing_controls(request: Request, resource_type: str) -> dict:
    """View mode, tag filter and the tag bar for a listing page.

    The view is remembered in the session: it's a preference about how you like
    to browse, and having it reset on every navigation is the kind of small
    friction that makes a tool feel unfinished.
    """
    view = request.query_params.get("view")
    if view in VIEWS:
        try:
            request.session[f"view_{resource_type}"] = view
        except (AssertionError, KeyError):
            pass
    else:
        try:
            view = request.session.get(f"view_{resource_type}")
        except (AssertionError, KeyError):
            view = None
    if view not in VIEWS:
        view = DEFAULT_VIEW

    tags: list = []
    vocab: list = []
    if store.available():
        try:
            # Two lists, two jobs. The filter bar shows what this kind of thing
            # is actually tagged with; the editor's picker offers the whole
            # vocabulary, including tags defined on /admin/tags that nothing
            # carries yet — otherwise a tag created there could never be
            # applied except by typing it from memory.
            tags = store.list_tags(resource_type)
            vocab = store.list_tags()
        except Exception:
            logger.exception("Could not read tags for %s", resource_type)
    return {
        "view": view,
        "views": VIEWS,
        "tag": (request.query_params.get("tag") or "").strip(),
        "all_tags": tags,
        "vocab": vocab,
    }


def _apply_tag_filter(items: list, resource_type: str, tag_slug: str) -> list:
    """Narrow a listing to one tag. Runs *after* the permission filter."""
    if not tag_slug or not store.available():
        return items
    try:
        keys = set(store.keys_with_tag(resource_type, tag_slug))
    except Exception:
        logger.exception("Could not filter by tag %r", tag_slug)
        return items
    return [i for i in items if i.slug in keys]


def _tags_of(resource_type: str, keys: list[str]) -> dict:
    """slug -> tags, in one query for the whole page."""
    if not keys or not store.available():
        return {}
    try:
        return store.tags_for(resource_type, keys)
    except Exception:
        logger.exception("Could not read tags for the listing")
        return {}


def _dashboard_filters(slug: str) -> list:
    """A dashboard's filter definitions, or none if they can't be read.

    Non-fatal on purpose: losing the filter bar should degrade a dashboard to
    its unfiltered self, not to an error page.
    """
    if not store.available():
        return []
    try:
        return store.list_filters(slug)
    except Exception:
        logger.exception("Could not read filters for dashboard %r", slug)
        return []


def _filter_options(definitions: list, access: store.Access) -> dict[str, list[str]]:
    """key -> the values a `select` filter offers.

    Each is a query the dashboard's editor wrote, so it runs under the same
    database grant as anything else: no grant for that database, no options.
    A failing options query costs one dropdown, never the page.

    **Not called while rendering a dashboard.** These are `SELECT DISTINCT`
    over the warehouse and they are slow — on Automatismo, eleven of them
    totalling 292 seconds, run in series before the page sent a byte, for a
    drawer most visits never open. They are served from
    dashboard_filter_options instead, on demand, and refreshed here.
    """
    wanted = [
        d
        for d in definitions
        if d.filter_type == filters.SELECT
        and d.values_sql.strip()
        and (not d.source_db or access.allows(store.DATABASE, d.source_db))
    ]
    if not wanted:
        return {}

    def one(d) -> tuple[str, list[str]]:
        try:
            result = db.execute(d.values_sql, d.source_db or None, max_rows=1000)
            return d.key, [
                "" if r[0] is None else str(r[0]) for r in result.rows if r and r[0] is not None
            ]
        except Exception:
            logger.exception("Filter %r options query failed", d.key)
            return d.key, []

    # In parallel: they are independent, and in series the slowest dashboard
    # waited on the sum rather than the maximum.
    workers = min(len(wanted), 6)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, wanted))


def _tile_shells(items: list) -> list[dict]:
    """Tiles with no data in them, for the page's first paint.

    The dashboard page runs **no** queries: it sends the layout, and each tile
    fetches its own data from dashboard_tile_data. That is the whole reason a
    dashboard now appears instantly instead of after the sum of every query on
    it. Text tiles carry their content and never fetch.
    """
    return [
        {
            "item": item,
            "chart": item.chart,
            "spec": None,
            "error": None,
            "unfiltered": False,
            "canvas_id": f"tile-{index}",
        }
        for index, item in enumerate(items)
    ]


@app.get("/dashboards", response_class=HTMLResponse)
def dashboards_index(
    request: Request,
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _dashboards_unavailable(request, user)
    context = _shell_context(
        user,
        "dashboards",
        access,
        dashboards=[],
        can_build=access.allows(store.FEATURE, "dashboard_builder"),
        first_chart={},
        **_listing_controls(request, store.DASHBOARD),
    )
    try:
        visible = [d for d in store.list_dashboards() if access.allows(store.DASHBOARD, d.slug)]
        context["dashboards"] = _apply_tag_filter(visible, store.DASHBOARD, context["tag"])
        context["tags_map"] = _tags_of(store.DASHBOARD, [d.slug for d in context["dashboards"]])
        # Preview shows a dashboard's first chart — one real chart rather than a
        # sketch, and one query per card instead of one per tile.
        if context["view"] == "preview":
            context["first_chart"] = store.first_chart_of(
                [d.slug for d in context["dashboards"]]
            )
    except Exception:
        logger.exception("Could not list dashboards")
        context["db_ok"] = False
        context["db_error"] = "Could not read dashboards — check the server logs."
    return templates.TemplateResponse(request, "dashboards.html", context)


@app.post("/dashboards")
def dashboard_create(
    request: Request,
    title: str = Form(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _dashboards_unavailable(request, user)
    if not access.allows(store.FEATURE, "dashboard_builder"):
        return _forbidden(
            request, user, i18n.t("You don't have access to the dashboard builder."), access=access
        )
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
    return RedirectResponse(request.url_for("dashboard_edit", slug=dash.id), status_code=303)


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
def dashboard_show(
    request: Request,
    slug: str = Depends(dashboard_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _dashboards_unavailable(request, user)
    if not access.allows(store.DASHBOARD, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to that dashboard."), access=access
        )
    dash, failure = _load_dashboard(request, user, slug)
    if failure is not None:
        return failure
    # Tiles are filtered by chart grant: holding a dashboard must not become a
    # way to see a chart — or the database behind it — you were never granted.
    # Text tiles have no chart and are always kept.
    dash.items = [
        i for i in dash.items if i.chart is None or access.allows(store.CHART, i.chart.slug)
    ]

    # Filter state lives in the query string, so a filtered dashboard is a link
    # you can send to somebody.
    definitions = _dashboard_filters(slug)
    chosen = {key: request.query_params.getlist(key) for key in request.query_params}
    active = filters.resolve(definitions, chosen)

    tiles = _tile_shells(dash.items)
    context = _shell_context(
        user,
        "dashboards",
        access,
        dashboard=dash,
        tiles=tiles,
        filters=active,
        # Deliberately empty: the drawer fetches its own options when opened.
        # Building them here is what made this page take minutes.
        filter_options={},
        can_build=access.allows(store.FEATURE, "dashboard_builder"),
        grid_columns=store.GRID_COLUMNS,
        row_height=store.ROW_HEIGHT_PX,
    )
    return templates.TemplateResponse(request, "dashboard_show.html", context)


@app.get("/dashboards/{slug}/filter-options")
def dashboard_filter_options(
    request: Request,
    slug: str = Depends(dashboard_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """The values every select filter offers, fetched when the drawer opens.

    Cached, because these are `SELECT DISTINCT` over the warehouse and a
    column's distinct values move on the data's schedule rather than the
    viewer's. `?refresh=1` forces a rebuild.
    """
    if not store.available():
        return JSONResponse({"error": i18n.t("The app database is not configured.")}, 503)
    if not access.allows(store.DASHBOARD, slug):
        return JSONResponse(
            {"error": i18n.t("You don't have access to that dashboard.")}, status_code=403
        )

    definitions = _dashboard_filters(slug)
    ttl = timedelta(minutes=max(0, settings.filter_options_cache_minutes))
    force = request.query_params.get("refresh") == "1"
    cached: dict = {}
    if not force and ttl:
        try:
            cached = store.get_filter_options(slug)
        except Exception:
            logger.exception("Could not read cached filter options for %r", slug)

    now = datetime.now(UTC)
    fresh: dict[str, list] = {}
    stale = []
    for d in definitions:
        if d.filter_type != filters.SELECT or not d.values_sql.strip():
            continue
        hit = cached.get(d.key)
        if hit and now - hit[1] < ttl:
            fresh[d.key] = hit[0]
        else:
            stale.append(d)

    if stale:
        built = _filter_options(stale, access)
        for key, values in built.items():
            fresh[key] = values
            try:
                store.put_filter_options(slug, key, values)
            except Exception:
                logger.exception("Could not cache filter options for %r/%r", slug, key)

    return JSONResponse({"options": fresh})


@app.get("/dashboards/{slug}/tiles/{item_id}")
def dashboard_tile_data(
    request: Request,
    slug: str = Depends(dashboard_ref),
    item_id: int = PathParam(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """One tile's data, fetched by the browser after the page has painted.

    Dashboards used to run every query before sending a single byte, so a page
    with twenty tiles waited on the slowest of twenty sequential queries. Now
    the page arrives immediately and each tile fills itself in, in parallel.

    Permission is re-checked here in full: this is a real endpoint, and "the
    page already checked" is not a check.
    """
    if not store.available():
        return JSONResponse({"error": "The app database is not configured."}, status_code=503)
    if not access.allows(store.DASHBOARD, slug):
        return JSONResponse({"error": "No access to that dashboard."}, status_code=403)
    try:
        dash = store.get_dashboard(slug)
    except Exception:
        logger.exception("Could not load dashboard %r for tile %s", slug, item_id)
        return JSONResponse({"error": "Could not load the dashboard."}, status_code=502)
    if dash is None:
        return JSONResponse({"error": "Unknown dashboard."}, status_code=404)

    item = next((i for i in dash.items if i.id == item_id), None)
    if item is None or item.chart is None:
        return JSONResponse({"error": "Unknown tile."}, status_code=404)
    if not access.allows(store.CHART, item.chart.slug):
        return JSONResponse({"error": "No access to that chart."}, status_code=403)

    definitions = _dashboard_filters(slug)
    chosen = {key: request.query_params.getlist(key) for key in request.query_params}
    active = filters.resolve(definitions, chosen)

    tile = _render_tiles([item], active)[0]
    if tile["error"]:
        return JSONResponse({"error": tile["error"]}, status_code=200)
    spec = tile["spec"]
    payload = {
        "renders_as": spec.renders_as,
        "warnings": spec.warnings,
        "unfiltered": tile["unfiltered"],
    }
    if spec.renders_as == "canvas":
        payload["spec"] = spec.to_dict()
    elif spec.renders_as == "table":
        payload["columns"] = spec.columns
        payload["rows"] = [["" if v is None else str(v) for v in r] for r in spec.rows]
    else:
        payload["value"] = spec.value
        payload["caption"] = spec.caption
    return JSONResponse(payload)


@app.get("/dashboards/{slug}/edit", response_class=HTMLResponse)
def dashboard_edit(
    request: Request,
    slug: str = Depends(dashboard_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _dashboards_unavailable(request, user)
    if not (
        access.allows(store.DASHBOARD, slug) and access.allows(store.FEATURE, "dashboard_builder")
    ):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    dash, failure = _load_dashboard(request, user, slug)
    if failure is not None:
        return failure
    dash.items = [
        i for i in dash.items if i.chart is None or access.allows(store.CHART, i.chart.slug)
    ]

    available_charts = []
    try:
        available_charts = [c for c in store.list_charts() if access.allows(store.CHART, c.slug)]
    except Exception:
        logger.exception("Could not list charts for the dashboard editor")

    databases: list[str] = []
    try:
        databases = [d for d in db.list_databases() if access.allows(store.DATABASE, d)]
    except Exception:
        logger.exception("Could not list databases for the dashboard editor")

    tiles = _tile_shells(dash.items)
    context = _shell_context(
        user,
        "dashboards",
        access,
        dashboard=dash,
        tiles=tiles,
        available_charts=available_charts,
        widths=store.WIDTHS,
        dash_filters=_dashboard_filters(slug),
        filter_types=filters.FILTER_TYPES,
        databases=databases,
        grid_columns=store.GRID_COLUMNS,
        row_height=store.ROW_HEIGHT_PX,
    )
    return templates.TemplateResponse(request, "dashboard_edit.html", context)


@app.post("/dashboards/{slug}/layout")
async def dashboard_save_layout(
    request: Request,
    slug: str = Depends(dashboard_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Persist the grid after a drag or resize. Sent as JSON by the editor."""
    if not _may_edit_dashboard(access, slug):
        return JSONResponse({"error": "No access to edit that dashboard."}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Expected JSON."}, status_code=400)
    placements = body.get("tiles") if isinstance(body, dict) else None
    if not isinstance(placements, list):
        return JSONResponse({"error": "Expected {tiles: [...]}"}, status_code=400)
    if not store.available():
        return JSONResponse({"error": "The app database is not configured."}, status_code=503)
    written = store.save_layout(slug, placements)
    logger.info("Dashboard %r layout saved by %s (%s tiles)", slug, user, written)
    return JSONResponse({"saved": written})


@app.post("/dashboards/{slug}/filters/{filter_id}")
def dashboard_update_filter(
    request: Request,
    slug: str = Depends(dashboard_ref),
    filter_id: int = PathParam(...),
    label: str = Form(...),
    column_expr: str = Form(...),
    filter_type: str = Form("select"),
    values_sql: str = Form(""),
    source_db: str = Form(""),
    default_value: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Edit a filter in place. The key never changes — it's in shared URLs."""
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if filter_type not in filters.FILTER_TYPE_KEYS:
        filter_type = filters.SELECT
    if source_db and not access.allows(store.DATABASE, source_db):
        return _forbidden(request, user, f"You don't have access to {source_db!r}.", access=access)
    if store.available():
        store.update_filter(
            slug,
            filter_id,
            label=label.strip(),
            column_expr=column_expr.strip(),
            filter_type=filter_type,
            values_sql=values_sql.strip(),
            source_db=source_db,
            default_value=default_value.strip(),
        )
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/filters")
def dashboard_add_filter(
    request: Request,
    slug: str = Depends(dashboard_ref),
    key: str = Form(...),
    label: str = Form(...),
    column_expr: str = Form(...),
    filter_type: str = Form("select"),
    values_sql: str = Form(""),
    source_db: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Define a filter. The options query runs under the caller's own grants."""
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    # The key names a bind parameter and a query-string field, so it has to be a
    # plain identifier — not because of injection (values are bound) but because
    # anything else produces a filter nobody can address.
    if not filters.valid_key(key):
        return _forbidden(
            request, user, i18n.t("A filter key must look like lower_snake_case."), access=access
        )
    if filter_type not in filters.FILTER_TYPE_KEYS:
        filter_type = filters.SELECT
    # You can only point an options query at a database you may read anyway.
    if source_db and not access.allows(store.DATABASE, source_db):
        return _forbidden(request, user, f"You don't have access to {source_db!r}.", access=access)
    if store.available():
        store.add_filter(
            slug,
            store.DashboardFilter(
                key=key,
                label=label.strip() or key,
                column_expr=column_expr.strip(),
                filter_type=filter_type,
                values_sql=values_sql.strip(),
                source_db=source_db,
            ),
        )
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/filters/{filter_id}/delete")
def dashboard_delete_filter(
    request: Request,
    slug: str = Depends(dashboard_ref),
    filter_id: int = PathParam(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.delete_filter(slug, filter_id)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/items")
def dashboard_add_item(
    request: Request,
    slug: str = Depends(dashboard_ref),
    chart_slug: str = Form(...),
    width: str = Form(store.DEFAULT_WIDTH),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not (
        access.allows(store.DASHBOARD, slug) and access.allows(store.FEATURE, "dashboard_builder")
    ):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.add_item(slug, chart_slug, width)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


def _may_edit_dashboard(access: store.Access, slug: str) -> bool:
    return access.allows(store.DASHBOARD, slug) and access.allows(
        store.FEATURE, "dashboard_builder"
    )


@app.post("/dashboards/{slug}/sections")
def dashboard_add_section(
    request: Request,
    slug: str = Depends(dashboard_ref),
    title: str = Form(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Add a tab. Tiles are moved onto it one at a time from their own control."""
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.add_section(slug, title)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/sections/{section_id}/delete")
def dashboard_delete_section(
    request: Request,
    slug: str = Depends(dashboard_ref),
    section_id: int = PathParam(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.delete_section(slug, section_id)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/items/{item_id}/section")
def dashboard_set_item_section(
    request: Request,
    slug: str = Depends(dashboard_ref),
    item_id: int = PathParam(...),
    section_id: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    target = int(section_id) if section_id.strip().isdigit() else None
    if store.available():
        store.set_item_section(slug, item_id, target)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/text")
def dashboard_add_text(
    request: Request,
    slug: str = Depends(dashboard_ref),
    content: str = Form(...),
    section_id: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """A heading or note between charts."""
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    target = int(section_id) if section_id.strip().isdigit() else None
    if store.available():
        store.add_text_item(slug, content.strip()[:2000], section_id=target)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/items/{item_id}/remove")
def dashboard_remove_item(
    request: Request,
    slug: str = Depends(dashboard_ref),
    item_id: int = PathParam(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not (
        access.allows(store.DASHBOARD, slug) and access.allows(store.FEATURE, "dashboard_builder")
    ):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.remove_item(slug, item_id)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/items/{item_id}/width")
def dashboard_set_width(
    request: Request,
    slug: str = Depends(dashboard_ref),
    item_id: int = PathParam(...),
    width: str = Form(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not (
        access.allows(store.DASHBOARD, slug) and access.allows(store.FEATURE, "dashboard_builder")
    ):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.set_item_width(slug, item_id, width)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/items/{item_id}/move")
def dashboard_move_item(
    request: Request,
    slug: str = Depends(dashboard_ref),
    item_id: int = PathParam(...),
    direction: str = Form(...),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not (
        access.allows(store.DASHBOARD, slug) and access.allows(store.FEATURE, "dashboard_builder")
    ):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.move_item(slug, item_id, -1 if direction == "up" else 1)
    return _back_to(request, "dashboard_edit", "dashboards", slug)


@app.post("/dashboards/{slug}/delete")
def dashboard_delete(
    request: Request,
    slug: str = Depends(dashboard_ref),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not (
        access.allows(store.DASHBOARD, slug) and access.allows(store.FEATURE, "dashboard_builder")
    ):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
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


@app.get("/admin/users/{username}", response_class=HTMLResponse)
def admin_user_detail(request: Request, username: str, user: str = Depends(require_admin)):
    """One user: their roles, flags, and what those roles actually resolve to.

    The effective-access summary is the point of the page — it answers "why
    can't they see X?" by cause rather than symptom, which is what you'd
    otherwise reach for impersonation to find out.
    """
    target = store.get_user(username)
    if target is None:
        context = _shell_context(
            user,
            "admin",
            admin_tab="users",
            users=store.list_users(),
            roles=store.list_roles(),
            is_admin=True,
            error=f"Unknown user: {username!r}.",
        )
        return templates.TemplateResponse(request, "admin_users.html", context, status_code=404)

    roles = store.list_roles()
    access = store.access_for(username)

    # Which role granted what, so a surprising grant can be traced to its source.
    by_role = {r.name: r for r in roles}
    sources: dict[str, dict[str, list[str]]] = {}
    for rname in target.roles:
        role = by_role.get(rname)
        if not role:
            continue
        for rtype, keys in role.permissions.items():
            for key in keys:
                sources.setdefault(rtype, {}).setdefault(key, []).append(rname)

    context = _shell_context(
        user,
        "admin",
        admin_tab="users",
        target=target,
        roles=roles,
        # Deliberately NOT `access`: that parameter is the *viewer's*, and
        # drives their nav. This is what the user being edited can reach.
        target_access=access,
        sources=sources,
        resource_types=store.RESOURCE_TYPES,
        resource_labels=store.RESOURCE_LABELS,
        ANY=store.ANY,
        is_admin=True,
        is_self=target.username == user,
    )
    return templates.TemplateResponse(request, "admin_user_detail.html", context)


@app.post("/admin/users/{username}/roles")
def admin_set_user_roles(
    request: Request,
    username: str,
    roles: list[str] = Form(default=[]),
    user: str = Depends(require_admin),
):
    store.set_user_roles(username, roles)
    logger.info("Roles for %r set to %r by %s", username, roles, user)
    return RedirectResponse(
        request.url_for("admin_user_detail", username=username), status_code=303
    )


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
    return RedirectResponse(
        request.url_for("admin_user_detail", username=username), status_code=303
    )


@app.post("/admin/users/{username}/active")
def admin_toggle_active(
    request: Request,
    username: str,
    value: str = Form(...),
    user: str = Depends(require_admin),
):
    store.set_user_active(username, value == "true")
    return RedirectResponse(
        request.url_for("admin_user_detail", username=username), status_code=303
    )


def _grantable() -> dict[str, list[str]]:
    """Every existing key, per resource type — the checkboxes on the roles screen.

    Must return an entry for every RESOURCE_TYPE: a type missing here renders
    a section with no checkboxes, so the permission cannot be granted and any
    existing grant is dropped the moment that section is saved. Each source is
    caught separately so one dead source costs one section, not the screen.
    """
    # Every database on the instance, so grants are picked from a list rather
    # than typed. This is the one place the full list is still shown, and it is
    # admin-only by construction.
    live_databases: list[str] = []
    try:
        live_databases = db.list_databases()
    except Exception:
        logger.exception("Could not list databases for the roles screen")

    try:
        # all_reports(), not load_reports(): a report created in the UI is just
        # as grantable as one defined in reports.toml.
        report_keys = [r.key for r in reports.all_reports()]
    except Exception:
        logger.exception("Could not load reports for the roles screen")
        report_keys = []

    try:
        chart_slugs = [c.slug for c in store.list_charts()]
        dashboard_slugs = [d.slug for d in store.list_dashboards()]
    except Exception:
        logger.exception("Could not load charts/dashboards for the roles screen")
        chart_slugs, dashboard_slugs = [], []

    # Separate from the block above: the catalog is a live query against the
    # warehouse, so it fails independently of anything in the app database.
    try:
        dataset_names = [d.name for d in datasets.list_datasets()]
    except Exception:
        logger.exception("Could not list datasets for the roles screen")
        dataset_names = []

    return {
        store.DATABASE: live_databases,
        store.DATASET: dataset_names,
        store.REPORT: report_keys,
        store.CHART: chart_slugs,
        store.DASHBOARD: dashboard_slugs,
        store.FEATURE: list(store.FEATURE_KEYS),
    }


@app.get("/admin/roles", response_class=HTMLResponse)
def admin_roles(request: Request, user: str = Depends(require_admin)):
    roles = store.list_roles()
    live = _grantable()

    # Union in whatever is already granted. A grant naming something that no
    # longer exists would otherwise have no checkbox, and since each section
    # posts its full set, saving would silently drop it.
    options: dict[str, list[str]] = {}
    stale: dict[str, list[str]] = {}
    for rtype, names in live.items():
        granted = {k for r in roles for k in r.keys(rtype) if k != store.ANY}
        options[rtype] = sorted(set(names) | granted)
        stale[rtype] = sorted(granted - set(names)) if names else []

    context = _shell_context(
        user,
        "admin",
        admin_tab="roles",
        roles=roles,
        options=options,
        stale=stale,
        resource_types=store.RESOURCE_TYPES,
        resource_labels=store.RESOURCE_LABELS,
        feature_labels=dict(store.FEATURES),
        ANY=store.ANY,
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


@app.post("/admin/roles/{role_id}/permissions")
def admin_set_role_permissions(
    request: Request,
    role_id: int,
    resource_type: str = Form(...),
    keys: list[str] = Form(default=[]),
    user: str = Depends(require_admin),
):
    """Replace one resource type's grants for a role.

    One type per submit, so saving the Databases section can't clear the Charts
    section — each `<form>` on the page owns exactly one type.
    """
    if not store.set_role_permissions(role_id, resource_type, keys):
        logger.warning(
            "Rejected permission update for role %s type %r by %s", role_id, resource_type, user
        )
    else:
        logger.info("Role %s %s grants set to %r by %s", role_id, resource_type, keys, user)
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


# ── tags ───────────────────────────────────────────────────────────────────
#
# Curating the vocabulary. Anyone who can build still creates a tag just by
# typing it onto a chart — that stays, because making people declare a word
# before using it is friction on something whose only job is findability.
# What lives here is the other half: defining the words up front, and the
# delete, which is admin-only because it reaches across everybody's screens.


@app.get("/admin/tags", response_class=HTMLResponse)
def admin_tags(request: Request, user: str = Depends(require_admin)):
    context = _shell_context(user, "admin", admin_tab="tags", tags=store.list_tags(), is_admin=True)
    return templates.TemplateResponse(request, "admin_tags.html", context)


@app.post("/admin/tags")
def admin_create_tag(request: Request, name: str = Form(...), user: str = Depends(require_admin)):
    if store.create_tag(name, created_by=user) is None:
        # Already there, or nothing left after trimming. Neither is an error
        # worth a red box: the wanted end state — a tag by that name — holds.
        logger.info("Tag %r not created for %s: empty or already exists", name, user)
    else:
        logger.info("Tag %r created by %s", name, user)
    return RedirectResponse(request.url_for("admin_tags"), status_code=303)


@app.post("/admin/tags/{slug}/delete")
def admin_delete_tag(request: Request, slug: str, user: str = Depends(require_admin)):
    if store.delete_tag(slug):
        logger.info("Tag %r deleted by %s", slug, user)
    return RedirectResponse(request.url_for("admin_tags"), status_code=303)


# ── reports ────────────────────────────────────────────────────────────────


@app.get("/reports", response_class=HTMLResponse)
def reports_index(
    request: Request,
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Reports on their own page.

    They used to be a second pane inside the query console, which stopped
    making sense once they became things you create and manage rather than a
    fixed list bolted onto the SQL editor.
    """
    visible = []
    try:
        visible = [r for r in reports.all_reports() if access.allows(store.REPORT, r.key)]
    except Exception:
        logger.exception("Could not load reports")

    context = _shell_context(
        user,
        "reports",
        access,
        reports=visible,
        can_build=access.allows(store.FEATURE, "report_builder") and store.available(),
    )
    return templates.TemplateResponse(request, "reports.html", context)


def _report_form_context(user: str, access: store.Access, **extra) -> dict:
    databases: list[str] = []
    try:
        databases = access.filter(store.DATABASE, db.list_databases())
    except Exception:
        logger.exception("Could not list databases for the report builder")
    context = _shell_context(
        user,
        "reports",
        access,
        databases=databases,
        report=None,
        slug="",
        title="",
        description="",
        source_db=databases[0] if databases else "",
        sql="",
        preview=None,
        max_rows=settings.query_max_rows,
    )
    context.update(extra)
    return context


@app.get("/reports/new", response_class=HTMLResponse)
def report_new(
    request: Request,
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _forbidden(
            request, user, i18n.t("The app database is not configured."), 503, access=access
        )
    if not access.allows(store.FEATURE, "report_builder"):
        return _forbidden(
            request, user, i18n.t("You don't have access to create reports."), access=access
        )
    return templates.TemplateResponse(
        request, "report_builder.html", _report_form_context(user, access)
    )


@app.post("/reports/preview", response_class=HTMLResponse)
def report_preview(
    request: Request,
    sql: str = Form(...),
    source_db: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    slug: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Run the report's SQL so you can see it before saving."""
    if not access.allows(store.FEATURE, "report_builder"):
        return _forbidden(
            request, user, i18n.t("You don't have access to create reports."), access=access
        )
    if not access.allows(store.DATABASE, source_db):
        return _forbidden(request, user, f"You don't have access to {source_db!r}.", access=access)

    context = _report_form_context(
        user,
        access,
        sql=sql,
        source_db=source_db,
        title=title,
        description=description,
        slug=slug,
    )
    try:
        # Small ceiling: this is a look before saving, not the export.
        result = db.execute(sql, source_db, max_rows=settings.query_page_size)
    except Exception as exc:
        logger.exception("Report preview failed")
        context["error"] = f"Query failed: {exc}"
        return templates.TemplateResponse(request, "report_builder.html", context, status_code=400)
    if not result.returns_rows:
        context["error"] = "That statement returns no rows, so it can't be a report."
        return templates.TemplateResponse(request, "report_builder.html", context, status_code=400)
    context["preview"] = {
        "columns": result.columns,
        "rows": [[None if v is None else str(v) for v in row] for row in result.rows],
        "more": result.truncated,
    }
    return templates.TemplateResponse(request, "report_builder.html", context)


@app.post("/reports/save")
def report_save(
    request: Request,
    sql: str = Form(...),
    source_db: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    slug: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _forbidden(
            request, user, i18n.t("The app database is not configured."), 503, access=access
        )
    if not access.allows(store.FEATURE, "report_builder"):
        return _forbidden(
            request, user, i18n.t("You don't have access to create reports."), access=access
        )
    if not access.allows(store.DATABASE, source_db):
        return _forbidden(request, user, f"You don't have access to {source_db!r}.", access=access)

    title = title.strip() or "Untitled report"
    # Editing keeps the slug; a new report gets one that isn't taken. File
    # reports are never editable here, so their keys can't be clobbered.
    if not slug:
        taken = {r.key for r in reports.load_reports()}
        slug = store.unique_slug(
            title, exists=lambda s: True if s in taken else store.get_report(s)
        )
    saved = store.save_report(
        store.Report(
            slug=slug,
            title=title,
            description=description,
            source_db=source_db,
            sql=sql,
            created_by=user,
        )
    )
    logger.info("Report %r saved by %s", saved.slug, user)
    return RedirectResponse(request.url_for("reports_index"), status_code=303)


@app.get("/reports/{slug}/edit", response_class=HTMLResponse)
def report_edit(
    request: Request,
    slug: str,
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not store.available():
        return _forbidden(
            request, user, i18n.t("The app database is not configured."), 503, access=access
        )
    if not (access.allows(store.FEATURE, "report_builder") and access.allows(store.REPORT, slug)):
        return _forbidden(
            request, user, i18n.t("You don't have access to that report."), access=access
        )
    saved = store.get_report(slug)
    if saved is None:
        # Either unknown, or a reports.toml report — those are edited in git.
        return _forbidden(
            request,
            user,
            i18n.t("That report is defined in reports.toml and is edited in git, not here."),
            404,
        )
    return templates.TemplateResponse(
        request,
        "report_builder.html",
        _report_form_context(
            user,
            access,
            slug=saved.slug,
            title=saved.title,
            description=saved.description,
            source_db=saved.source_db,
            sql=saved.sql,
        ),
    )


@app.post("/reports/{slug}/delete")
def report_delete(
    request: Request,
    slug: str,
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not (access.allows(store.FEATURE, "report_builder") and access.allows(store.REPORT, slug)):
        return _forbidden(
            request, user, i18n.t("You don't have access to that report."), access=access
        )
    if store.available():
        store.delete_report(slug)
        logger.info("Report %r deleted by %s", slug, user)
    return RedirectResponse(request.url_for("reports_index"), status_code=303)


# ── folders (UNWIRED — kept for a future attempt) ──────────────────────────
#
# Folders group the list pages and nothing else. They carry no permission, so
# these routes are gated on the ordinary builder features rather than on admin:
# filing a chart is editing a chart. See store.py's folders section.
#
# The UI is deliberately switched off: the grouping read as cluttered and the
# per-card "move to folder" dropdown wasn't obvious enough to earn its place, so
# the app now behaves exactly as if folders had never been built. What that
# means concretely:
#
#   * no Folders tab in the admin sub-nav (_admin_tabs.html)
#   * the charts / dashboards / reports pages render one flat list of cards
#   * nothing reads or writes folder_id in the normal course of using the app
#
# Everything underneath survives — migration 0007, the folders table, the
# folder_id columns, the store API, these routes, admin_folders.html and the
# _folders.html macros. The routes below still work if you hit the URLs; they
# are simply unreachable by clicking. Turning it back on means re-adding the tab
# and the macro calls in the three list templates, not rebuilding any of this.
#
# The one rule to keep if it does come back: a folder must never grant anything.
# That property is pinned by test_folders_are_organisation_only, which still
# runs — the guarantee is about the schema, not the screens.

# Which feature lets you file each kind of thing — the same one that lets you
# create it. There is no separate "organise" permission because organising is
# not a separate power.
_FILE_FEATURE = {
    store.CHART: "chart_builder",
    store.DASHBOARD: "dashboard_builder",
    store.REPORT: "report_builder",
}

_FILE_REDIRECT = {
    store.CHART: "charts_index",
    store.DASHBOARD: "dashboards_index",
    store.REPORT: "reports_index",
}


@app.post("/folders/file")
def folder_file_item(
    request: Request,
    resource_type: str = Form(...),
    resource_key: str = Form(...),
    folder_id: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Move one item into a folder, or out of all of them with a blank value."""
    feature = _FILE_FEATURE.get(resource_type)
    if feature is None:
        return _forbidden(
            request, user, i18n.t("That kind of thing can't be filed in a folder."), access=access
        )
    # Both checks matter: the feature says you may organise this kind of thing,
    # the key says you may see this particular one. Neither implies the other.
    if not (access.allows(store.FEATURE, feature) and access.allows(resource_type, resource_key)):
        return _forbidden(request, user, i18n.t("You don't have access to that."), access=access)

    target = int(folder_id) if folder_id.strip().isdigit() else None
    if store.available():
        store.set_item_folder(resource_type, resource_key, target)
        logger.info("%s %r filed under folder %s by %s", resource_type, resource_key, target, user)
    return RedirectResponse(request.url_for(_FILE_REDIRECT[resource_type]), status_code=303)


@app.get("/admin/folders", response_class=HTMLResponse)
def admin_folders(request: Request, user: str = Depends(require_admin)):
    """Create, rename, reorder and delete folders.

    Deliberately *not* where membership is edited: you file something from the
    page where you can see it, which is the only place the choice makes sense.
    """
    folders, counts = [], {}
    if store.available():
        try:
            folders = store.list_folders()
            counts = store.folder_counts()
        except Exception:
            logger.exception("Could not read folders for the admin screen")

    context = _shell_context(
        user,
        "admin",
        admin_tab="folders",
        folders=folders,
        counts=counts,
        is_admin=True,
    )
    return templates.TemplateResponse(request, "admin_folders.html", context)


@app.post("/admin/folders")
def admin_create_folder(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    user: str = Depends(require_admin),
):
    if store.create_folder(name, description, created_by=user) is None:
        logger.info("Folder %r not created (empty name), by %s", name, user)
    return RedirectResponse(request.url_for("admin_folders"), status_code=303)


@app.post("/admin/folders/{folder_id}")
def admin_update_folder(
    request: Request,
    folder_id: int,
    name: str = Form(...),
    description: str = Form(""),
    user: str = Depends(require_admin),
):
    store.update_folder(folder_id, name, description)
    return RedirectResponse(request.url_for("admin_folders"), status_code=303)


@app.post("/admin/folders/{folder_id}/move")
def admin_move_folder(
    request: Request,
    folder_id: int,
    direction: str = Form(...),
    user: str = Depends(require_admin),
):
    store.move_folder(folder_id, direction)
    return RedirectResponse(request.url_for("admin_folders"), status_code=303)


@app.post("/admin/folders/{folder_id}/delete")
def admin_delete_folder(request: Request, folder_id: int, user: str = Depends(require_admin)):
    store.delete_folder(folder_id)
    logger.info("Folder %s deleted by %s (contents kept, now ungrouped)", folder_id, user)
    return RedirectResponse(request.url_for("admin_folders"), status_code=303)


@app.post("/lang/{code}")
def set_language(request: Request, code: str, user: str = Depends(signed_in_user)):
    """Switch the interface language.

    Stored on the session so it applies immediately, and on the user row so it
    follows them to another browser. Never fatal: if the app database is down
    the session alone still carries the choice for this visit.
    """
    chosen = i18n.set_locale(code)
    request.session["lang"] = chosen
    if store.available():
        try:
            store.set_user_locale(user, chosen)
        except Exception:
            logger.exception("Could not store the language for %s", user)
    # Back where they were, so switching language doesn't lose the page.
    back = request.headers.get("referer") or str(request.url_for("console"))
    return RedirectResponse(back, status_code=303)


@app.post("/charts/{slug}/tags")
def chart_set_tags(
    request: Request,
    slug: str = Depends(chart_ref),
    tags: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    """Retag a chart. Comma-separated free text — people think in words, and
    making them create a tag first is friction for something whose only job is
    to make things findable."""
    if not (access.allows(store.CHART, slug) and access.allows(store.FEATURE, "chart_builder")):
        return _forbidden(
            request, user, i18n.t("You don't have access to that chart."), access=access
        )
    if store.available():
        store.set_tags(store.CHART, slug, tags.split(","))
    # Back to the listing they came from, so the view mode and any tag filter
    # they had applied survive the round trip.
    back = request.headers.get("referer") or str(request.url_for("charts_index"))
    return RedirectResponse(back, status_code=303)


@app.post("/dashboards/{slug}/tags")
def dashboard_set_tags(
    request: Request,
    slug: str = Depends(dashboard_ref),
    tags: str = Form(""),
    user: str = Depends(signed_in_user),
    access: store.Access = Depends(access_for),
):
    if not _may_edit_dashboard(access, slug):
        return _forbidden(
            request, user, i18n.t("You don't have access to edit that dashboard."), access=access
        )
    if store.available():
        store.set_tags(store.DASHBOARD, slug, tags.split(","))
    back = request.headers.get("referer") or str(request.url_for("dashboards_index"))
    return RedirectResponse(back, status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
