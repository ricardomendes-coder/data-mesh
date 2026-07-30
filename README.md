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
   - `AUTH_MODE` + `SSO_*` — single sign-on against Keycloak. See
     [Authentication](#authentication) below.
   - `INITIAL_ADMIN_USER` / `INITIAL_ADMIN_PASSWORD` — the first local user,
     created on first startup. Only used when `AUTH_MODE` permits password login.
   - `DB_*` — the server endpoint + login reachable from this host (e.g. the RDS
     endpoint from your EC2 instance). `DB_NAME` is the database preselected in
     the query dropdown; `DB_CATALOG` (default `postgres`) is the database used
     only to enumerate the others.

2. **Run**

   ```bash
   docker compose up --build
   ```

   Open http://localhost:8000 and log in with the admin credentials.

`sql/report.sql` holds a simple `now()`/`current_user`/`current_database()`
probe you can point a report at to confirm the whole pipeline works. (Add it
back to `reports.toml` as a `connectivity` report if you want a quick check.)

## Authentication

The hub signs users in through the **same Keycloak realm as Superset**
(`sso.v360.io/realms/v360`), reusing the Superset OIDC client. Anyone who can
log in to BI 360 can log in here, and because the realm session is shared they
usually arrive already authenticated — no second prompt.

`AUTH_MODE` picks the behaviour:

| Value | `/login` | `/login/local` |
| --- | --- | --- |
| `sso` (default) | redirects to Keycloak | `404` |
| `both` | redirects to Keycloak | bcrypt form — break-glass |
| `password` | redirects to `/login/local` | bcrypt form |

Usernames come from the `preferred_username` claim, the same mapping Superset's
`CustomSsoSecurityManager` uses, so a person has one identity across both tools.
Any authenticated realm user is allowed in — authentication *is* authorization
here, matching how BI 360 itself behaves.

### Keycloak setup

One admin change is needed on the existing Superset client in realm `v360`:

```
Valid redirect URIs   +  https://bi.v360.io/report/auth/callback
```

Then copy `SSO_CLIENT_ID` / `SSO_CLIENT_SECRET` from `bi360/web.env` on the BI
host into this app's `.env`, and set:

```
SSO_REDIRECT_URI=https://bi.v360.io/report/auth/callback
```

Set `SSO_REDIRECT_URI` explicitly rather than letting the app infer it — the
inferred value depends on `X-Forwarded-Proto` surviving the ALB → nginx →
uvicorn chain, and it must match Keycloak's registered URI byte-for-byte.

### If SSO breaks

`AUTH_MODE=both` + restart re-exposes the password form at
`/report/login/local` without disturbing the SSO path. Keep one local account
around for exactly this.

### Logout

Logout is **local only** by default: it clears this app's cookie and leaves the
Keycloak session alone, so signing out of the hub does not sign you out of
BI 360. Set `SSO_SINGLE_LOGOUT=true` for a full RP-initiated logout — that also
needs the post-logout URI registered on the Keycloak client.

### Cookie naming

The session cookie is `report_hub_session`, **not** `session`. Superset is
Flask-based and its cookie is called `session`; since both apps live on
`bi.v360.io`, sharing the name would make each one silently overwrite the
other's login. Don't set `SESSION_COOKIE_NAME=session`.

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

Under SSO there is nothing to do — access is granted in Keycloak, and any user
in the `v360` realm can sign in.

Local accounts only matter for the break-glass path (`AUTH_MODE=both`):

```bash
docker compose exec hub python manage.py create-user alice
```

Those are stored (username + bcrypt hash) in a Docker named volume.

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
- **Everyone in the `v360` realm gets the SQL console.** There are no roles
  here: a successful SSO login is full access. That's deliberate — it matches
  BI 360, and the target is the analytical database, not a production one. If
  that ever stops being true, gate `require_login` on a Keycloak role claim.
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
  auth.py       login dependency + session start/end
  oidc.py       Keycloak OIDC client and claim mapping
  users.py      JSON-backed user store for the break-glass login
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
- Gate access on a Keycloak role (`realm_access.roles`) if the hub ever needs to
  be narrower than "any realm user".
- Add JSON API endpoints for the "data functionalities" you mentioned — FastAPI
  gives you automatic docs at `/docs`.
- Switch to a persistent SSH tunnel with a health check if traffic grows.
```
