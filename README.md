# Report Hub

A minimal internal hub that runs in Docker on a machine with network access to
your database (e.g. an EC2 instance in the same VPC). Users log in, then either
run ad-hoc SQL or export the predefined report (CSV or Excel). It's structured
so you can grow it from here.

## How it works

```
Browser ──HTTP──▶  Docker container (this app)  ──▶  Database
                   login + query / report export
```

The app connects directly to `DB_HOST:DB_PORT` using the `DB_*` settings. For
the predefined report it runs `sql/report.sql`; the "Run a query" console runs
whatever SQL you type. Connections are opened and disposed per request, so
there's no long-lived connection to go stale.

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
   - `DB_*` — the database endpoint reachable from this host (e.g. the RDS
     endpoint or private IP from your EC2 instance).

2. **Run**

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

- **Use a read-only database user.** The "Run a query" console executes *any*
  SQL a logged-in user submits — including writes and DDL — so the DB user's
  own permissions are your only guardrail. Grant only what you're comfortable
  with every logged-in user having.
- **Everyone who can log in gets the SQL console.** There are no roles yet, so
  only hand out accounts to people you'd trust with that DB user.
- **HTTPS is required as configured.** `app/main.py` sets `https_only=True` on
  the session cookie, so login only works over `https://` (put it behind a
  reverse proxy like Caddy/Nginx/Traefik). For plain-HTTP local testing, flip
  it to `False` temporarily.
- Don't commit `.env` (already in `.gitignore`).
- Restrict network access to the hub (VPN / internal network only).

## Project layout

```
app/
  main.py       FastAPI app: routes, sessions, query console + report export
  auth.py       login dependency
  users.py      JSON-backed user store (swap for a table later)
  db.py         direct SQLAlchemy connection (run_query + execute)
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
