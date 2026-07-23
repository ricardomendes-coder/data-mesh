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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "title": settings.app_title, "error": None},
    )


@app.post("/query", response_class=HTMLResponse)
def run_query(
    request: Request,
    sql: str = Form(...),
    user: str = Depends(require_login),
):
    context = {
        "user": user,
        "title": settings.app_title,
        "error": None,
        "message": None,
        "sql": sql,
    }
    try:
        result = db.execute(sql)
    except Exception as exc:
        # This is a trusted, login-gated internal console, so showing the real
        # DB error is the useful behavior (unlike the fixed report export).
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
    user: str = Depends(require_login),
):
    try:
        result = db.execute(sql)
    except Exception as exc:
        logger.exception("Ad-hoc query export failed")
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "user": user,
                "title": settings.app_title,
                "error": f"Query failed: {exc}",
                "message": None,
                "sql": sql,
            },
            status_code=400,
        )

    if not result.returns_rows:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "user": user,
                "title": settings.app_title,
                "error": None,
                "message": f"OK — {result.rowcount} row(s) affected. Nothing to export.",
                "sql": sql,
            },
        )

    return _file_response(result.to_dataframe(), format, "query")


@app.get("/report/export")
def export_report(
    request: Request,
    format: str = "csv",
    user: str = Depends(require_login),
):
    try:
        df = reports.get_report()
    except Exception:
        # Full details go to the server log; the user sees a generic message so
        # we never leak connection strings or credentials into the browser.
        logger.exception("Report generation failed")
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "user": user,
                "title": settings.app_title,
                "error": "Could not generate the report. Check the server logs.",
            },
            status_code=502,
        )

    return _file_response(df, format, "report")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
