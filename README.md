# Report Hub

A minimal internal hub that runs in Docker on a machine with network access to
your database server (e.g. an EC2 instance in the same VPC). Users log in, then
either run ad-hoc SQL against **any database on the server** or export one of
several **predefined reports** (CSV or Excel). It's structured so you can grow
it from here.

## How it works

```
Browser ──HTTP──▶  Docker container (this app)  ──▶  Database server
                   login + query / report export        (pick a database)
```

The app connects directly to `DB_HOST:DB_PORT` using the `DB_*` settings — one
server, one login. The **database** is chosen per request:

- **Run a query**: pick a database from the dropdown (auto-discovered from the
  server via `pg_database`) and run whatever SQL you type.
- **Reports**: each report in `reports.toml` names its own database and SQL.

Connections are opened and disposed per request, so there's no long-lived
connection to go stale.

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
   - `DB_*` — the server endpoint + login reachable from this host (e.g. the RDS
     endpoint from your EC2 instance). `DB_NAME` is the database preselected in
     the query dropdown; `DB_CATALOG` (default `postgres`) is the database used
     only to enumerate the others.

2. **Run**

   ```bash
   docker compose up --build
   ```

   Open http://localhost:8000 and log in with the admin credentials.

The default `connectivity` report (`sql/report.sql`) returns `now()`, the DB
user, and the DB name — so you can confirm the whole pipeline works before
writing your real reports.

## Defining reports

Reports live in `reports.toml`. Each `[[report]]` names a `database` and its SQL
(a `sql_file` path or inline `sql`):

```toml
[[report]]
key      = "rocketlane_projects"
title    = "Rocketlane projects"
database = "rocketlane"
sql_file = "sql/rocketlane_projects.sql"
```

Both `reports.toml` and the `sql/` directory are mounted into the container (see
`docker-compose.yml`) and read fresh on each export, so edits take effect
immediately — no rebuild or restart needed.

## Adding more users

```bash
docker compose exec hub python manage.py create-user alice
```

Users are stored (username + bcrypt hash) in a Docker named volume.

## Targeting a different database engine

The app uses SQLAlchemy, so switching the database *type* is a config change
(the `pg_database` discovery query is PostgreSQL-specific, though — other
engines would need that adjusted in `app/db.py`):

| Database   | `DB_DRIVER`          | Extra dependency                  |
|------------|----------------------|-----------------------------------|
| PostgreSQL | `postgresql+psycopg2`| (already included)                |
| MySQL      | `mysql+pymysql`      | add `pymysql` to requirements.txt |
| SQL Server | `mssql+pyodbc`       | add `pyodbc` + an ODBC driver     |

## Security notes (please read before exposing this)

- **Use a read-only database user.** The "Run a query" console executes *any*
  SQL a logged-in user submits — including writes and DDL — against **any
  database on the server** the login can reach. The DB user's own permissions
  are your only guardrail; grant only what you're comfortable with every
  logged-in user having.
- **The dropdown lists every database on the server** (from `pg_database`), so
  all database names — client names included — are visible to anyone who can
  log in.
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
  db.py         direct SQLAlchemy connection: run_query / execute / list_databases
  reports.py    manifest-driven reports + CSV/Excel serialization
  config.py     settings from environment
  templates/    login + dashboard pages
reports.toml    report definitions (database + SQL per report)
sql/            report SQL files (report.sql is the connectivity check)
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
