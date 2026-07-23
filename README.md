# Report Hub

A minimal internal hub that runs in Docker on a machine with SSH access to your
database. Version 1 does exactly one thing: users log in and export a predefined
report (CSV or Excel). It's structured so you can grow it from here.

## How it works

```
Browser ──HTTP──▶  Docker container (this app)  ──SSH tunnel──▶  DB host  ──▶  Database
                   login + report export
```

For each report, the app opens an SSH tunnel to `SSH_HOST`, forwards a local
port to `DB_HOST:DB_PORT` (as seen *from the SSH host*), runs `sql/report.sql`,
and streams the results back as a file. The tunnel is opened and closed per
request, so there's no long-lived connection to go stale.

## Setup

1. **Configure environment**

   ```bash
   cp .env.example .env
   # then edit .env
   ```

   Generate a session secret:

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   Key values in `.env`:
   - `INITIAL_ADMIN_USER` / `INITIAL_ADMIN_PASSWORD` — the first user, created
     automatically on first startup.
   - `SSH_*` — how to reach the DB host over SSH.
   - `DB_*` — the database, addressed **from the SSH host** (often `127.0.0.1`
     if the DB runs on that host).

2. **Add the SSH key**

   Put the private key the app should use to open the tunnel at
   `secrets/ssh_key`. Use a **dedicated deploy key**, and make sure the file is
   readable by the container user (uid 10001) — e.g. `chmod 644 secrets/ssh_key`.
   (paramiko, unlike the `ssh` CLI, does not require `600`.)

3. **Run**

   ```bash
   docker compose up --build
   ```

   Open http://localhost:8000 and log in with the admin credentials.

The default `sql/report.sql` returns `now()`, the DB user, and the DB name — so
you can confirm the whole pipeline works before writing your real query.

## Editing the report

Edit `sql/report.sql`. With the default `docker-compose.yml` it's mounted into
the container, so changes take effect on the next export — no rebuild needed.

## Adding more users

```bash
docker compose exec hub python manage.py create-user alice
```

Users are stored (username + bcrypt hash) in a Docker named volume.

## Targeting a different database

The app uses SQLAlchemy, so switching databases is a config change:

| Database   | `DB_DRIVER`          | Extra dependency                  |
|------------|----------------------|-----------------------------------|
| PostgreSQL | `postgresql+psycopg2`| (already included)                |
| MySQL      | `mysql+pymysql`      | add `pymysql` to requirements.txt |
| SQL Server | `mssql+pyodbc`       | add `pyodbc` + an ODBC driver     |

## Security notes (please read before exposing this)

- **Use a read-only database user.** V1 only reads.
- **Put it behind HTTPS** (a reverse proxy like Caddy/Nginx/Traefik) and then
  set `https_only=True` in `app/main.py`'s `SessionMiddleware`.
- Don't commit `.env` or `secrets/` (already in `.gitignore`).
- Consider restricting network access to the hub (VPN / internal network only).

## Project layout

```
app/
  main.py       FastAPI app: routes, sessions, report export
  auth.py       login dependency
  users.py      JSON-backed user store (swap for a table later)
  db.py         SSH tunnel + SQLAlchemy query
  reports.py    predefined report + CSV/Excel serialization
  config.py     settings from environment
  templates/    login + dashboard pages
sql/report.sql  the predefined query
manage.py       CLI to add users
smoke_test.py   quick self-test (needs `pip install httpx`)
```

## Where to go next

- Add more reports (parameterized queries, date ranges).
- Add roles/permissions to the user store.
- Add JSON API endpoints for the "data functionalities" you mentioned — FastAPI
  gives you automatic docs at `/docs`.
- Switch to a persistent SSH tunnel with a health check if traffic grows.
```
