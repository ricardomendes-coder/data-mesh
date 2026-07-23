import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import reports, users
from .auth import NotAuthenticated, require_login
from .config import get_settings

logger = logging.getLogger("report_hub")
settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if format == "xlsx":
        data = reports.to_xlsx_bytes(df)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"report_{timestamp}.xlsx"
    else:
        data = reports.to_csv_bytes(df)
        media = "text/csv"
        filename = f"report_{timestamp}.csv"

    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
