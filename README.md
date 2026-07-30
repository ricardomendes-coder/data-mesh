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

Everyone reaches the hub through the **BI 360 login** — no separate account, no
second password. `AUTH_MODE` picks how:

| Value | How identity is established | `/login/local` |
| --- | --- | --- |
| `superset` (in use) | asks Superset who the caller is | `404` unless `ENABLE_LOCAL_LOGIN` |
| `sso` | talks to Keycloak directly | `404` unless `ENABLE_LOCAL_LOGIN` |
| `password` | local bcrypt accounts only | bcrypt form |

`ENABLE_LOCAL_LOGIN=true` re-exposes the bcrypt form at `/report/login/local`
alongside whichever mode is active — the break-glass path for when the IdP is
unreachable. Keep one local account around for exactly that.

Either way, **any user who can log in to BI 360 can use the hub**.
Authentication *is* authorization here, matching how BI 360 itself behaves. See
the security notes below.

### `AUTH_MODE=superset` — borrowing the BI 360 session

This is what's deployed, and it needs **no Keycloak change at all**.

`bi.v360.io/` is Superset and `bi.v360.io/report` is this app, so the browser
already sends Superset's `session` cookie on our requests. At login we hand that
cookie back to Superset's own `/api/v1/me/` and use whatever identity it
reports. Superset stays the only authority on who is signed in — we never verify
the cookie ourselves, so this app holds no copy of Superset's `SECRET_KEY`.

If there's no valid Superset session, the user is redirected to
`https://bi.v360.io/login/?next=…/report/`. Superset runs the normal Keycloak
flow (using *its* callback, which is registered) and drops them back here.

Why this rather than direct OIDC: the shared Keycloak client `V360-BI` permits
**exact-match redirect URIs only** — no wildcards — and adding
`https://bi.v360.io/report/auth/callback` needs realm-admin rights. Superset's
callback is already registered, so the hub borrows its login instead.

Two consequences worth knowing:

- **Logout ends the BI 360 session too.** Clearing only our cookie would be
  pointless — the next request would find Superset's cookie and sign the user
  straight back in. So `/logout` redirects to Superset's logout.
- **The Superset session is checked once, at login.** Afterwards the hub's own
  cookie carries the session, so logging out of BI 360 elsewhere doesn't
  immediately revoke hub access. `SESSION_MAX_AGE` (default 12h) bounds that
  window.

### `AUTH_MODE=sso` — direct Keycloak (preferred, needs one admin change)

The code for this is in `app/oidc.py` and fully working; it only needs the
redirect URI registered. When someone with realm access is available:

```
sso.v360.io → realm v360 → Clients → V360-BI → Valid redirect URIs
  +  https://bi.v360.io/report/auth/callback
```

Add it as a **new entry** — don't replace Superset's existing
`oauth-authorized/...` line or BI 360 login breaks. Then copy `SSO_CLIENT_ID` /
`SSO_CLIENT_SECRET` from `bi360/web.env` into `.env`, set
`SSO_REDIRECT_URI=https://bi.v360.io/report/auth/callback`, and switch
`AUTH_MODE=sso`.

Set `SSO_REDIRECT_URI` explicitly rather than letting the app infer it: the
inferred value depends on `X-Forwarded-Proto` surviving the ALB → nginx →
uvicorn chain, and it must match Keycloak's registered value byte-for-byte.

In this mode usernames come from the `preferred_username` claim — the same
mapping Superset's `CustomSsoSecurityManager` uses — so a person keeps one
identity across both tools. Logout is local-only by default, since the Keycloak
client is shared; `SSO_SINGLE_LOGOUT=true` opts into a full RP-initiated logout.

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

There is nothing to do — if someone can log in to BI 360, they can use the hub.

Local accounts only matter for the break-glass path (`ENABLE_LOCAL_LOGIN=true`):

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
  superset_session.py  delegated auth: identity from Superset's /api/v1/me/
  oidc.py       Keycloak OIDC client and claim mapping (AUTH_MODE=sso)
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
