from fastapi import Request


class NotAuthenticated(Exception):
    """Raised by `require_login` when there is no logged-in user. teste"""


def get_current_user(request: Request) -> str | None:
    return request.session.get("user")


def require_login(request: Request) -> str:
    """FastAPI dependency: returns the username or triggers a redirect to /login."""
    user = get_current_user(request)
    if not user:
        raise NotAuthenticated()
    return user
