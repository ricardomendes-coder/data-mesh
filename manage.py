"""Small management CLI.

Usage:
    python manage.py create-user <username>

Inside Docker:
    docker compose exec hub python manage.py create-user alice
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


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "create-user":
        create_user(sys.argv[2])
    else:
        print("Usage: python manage.py create-user <username>")
        sys.exit(1)


if __name__ == "__main__":
    main()
