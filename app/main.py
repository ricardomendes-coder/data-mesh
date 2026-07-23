import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db, reports, users
from .auth import NotAuthenticated, require_login
from .config import get_settings

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
    if (
        not users.any_users()
        and settings.initial_admin_user
        and settings.initial_admin_password
    ):
        users.add_user(settings.initial_admin_user, settings.initial_admin_password)
        logger.info("Created bootstrap admin user %r", settings.initial_admin_user)
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=True,  # set True when served over HTTPS (recommended)
)


@app.exception_handler(NotAuthenticated)
async def _redirect_to_login(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url=request.url_for("login_form"), status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse(request.url_for("dashboard"), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "title": settings.app_title}
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if users.verify_user(username, password):
        request.session["user"] = username
        return RedirectResponse(request.url_for("dashboard"), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid username or password.", "title": settings.app_title},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(request.url_for("login_form"), status_code=303)


def _dashboard_context(user: str, **extra) -> dict:
    """Base template context: the DB picker list and the reports manifest.

    Both DB discovery and manifest parsing are best-effort — a failure degrades
    the page (banner + empty list) instead of 500ing the whole dashboard.
    """
    databases: list[str] = []
    db_error = None
    try:
        databases = db.list_databases()
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
        "db_error": db_error,
        "reports": report_list,
        "result": None,
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
            "rows": [["" if v is None else str(v) for v in row] for row in shown],
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
        context = _dashboard_context(user, error=f"Unknown report: {key!r}.")
        return templates.TemplateResponse(
            request, "dashboard.html", context, status_code=404
        )
    except Exception:
        # Full details go to the server log; the user sees a generic message so
        # we never leak connection strings or credentials into the browser.
        logger.exception("Report generation failed for %r", key)
        context = _dashboard_context(
            user, error="Could not generate the report. Check the server logs."
        )
        return templates.TemplateResponse(
            request, "dashboard.html", context, status_code=502
        )

    return _file_response(df, format, key)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
