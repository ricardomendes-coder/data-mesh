"""Small management CLI.

Usage:
    python manage.py create-user <username>      # local break-glass account
    python manage.py list-users                  # who has signed in, and their roles
    python manage.py grant-admin <username>      # make someone an administrator
    python manage.py revoke-admin <username>

Inside Docker:
    docker compose exec hub python manage.py grant-admin marcelo.ferreira

grant-admin exists so the admin panel can never lock everyone out: a shell on
the host is always a way back in.
"""

import getpass
import sys

from app import users


def create_user(username: str) -> None:
    if users.user_exists(username):
        print(f"User {username!r} already exists.")
        sys.exit(1)
    pw = getpass.getpass(f"Password for {username}: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("Passwords do not match.")
        sys.exit(1)
    if not pw:
        print("Password cannot be empty.")
        sys.exit(1)
    users.add_user(username, pw)
    print(f"User {username!r} saved.")


def _require_store():
    from app import store

    if not store.available():
        print("The app database is not configured (set APP_DB_PASSWORD).")
        sys.exit(1)
    return store


def list_users() -> None:
    store = _require_store()
    rows = store.list_users()
    if not rows:
        print("No users have signed in yet.")
        return
    print(f"{'username':28} {'admin':6} {'active':7} {'via':10} roles")
    for u in rows:
        print(
            f"{u.username:28} {'yes' if u.is_admin else '-':6} "
            f"{'yes' if u.is_active else 'no':7} {u.auth_via or '-':10} "
            f"{', '.join(u.roles) or '-'}"
        )


def set_admin(username: str, value: bool) -> None:
    store = _require_store()
    if store.get_user(username) is None:
        print(f"No user {username!r} — they must sign in once before being granted admin.")
        sys.exit(1)
    if not value:
        admins = [u.username for u in store.list_users() if u.is_admin]
        if admins == [username]:
            print(f"{username!r} is the only administrator; grant admin elsewhere first.")
            sys.exit(1)
    store.set_user_admin(username, value)
    print(f"{username!r} is {'now an administrator' if value else 'no longer an administrator'}.")


def main() -> None:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "create-user":
        create_user(args[1])
    elif len(args) == 1 and args[0] == "list-users":
        list_users()
    elif len(args) == 2 and args[0] == "grant-admin":
        set_admin(args[1], True)
    elif len(args) == 2 and args[0] == "revoke-admin":
        set_admin(args[1], False)
    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
