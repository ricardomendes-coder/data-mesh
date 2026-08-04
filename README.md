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

## Charts

Write SQL, run it, map columns to axes, save. The chart re-runs its query each
time you open it.

Charts live in the app's **own database** (`report_hub`), not in `analytics`.
That separation is deliberate: the query console can write to `analytics`, and
the Datasets tab enumerates it — app state belongs in neither blast radius. The
database has its own owner role, matching the convention already used on that
server (`gitlab_user`, `keycloak_user`, `robots_api_user`).

Schema changes are append-only steps in `MIGRATIONS` (see `app/store.py`), each
applied once and recorded in `schema_migrations`. Never edit a shipped step —
add another.

Without `APP_DB_PASSWORD` the Charts tab shows a "not configured" notice; the
rest of the app is unaffected.

### About the colours

The eight series colours in `app/charts.py` are not a taste call. They were run
through the data-viz validator against the app's white card surface and clear
every gate — lightness band, chroma floor, adjacent colour-blind separation
(ΔE 9.5) and normal-vision separation (ΔE 16.8). Three consequences are load
bearing:

- Hues are assigned **by slot, in fixed order, never cycled**. A ninth series
  would reuse a hue and two series would look identical, so series are capped
  at eight and the chart says so.
- Aqua, yellow and magenta fall below 3:1 contrast on white, which obliges
  "relief" — an alternative to colour. That's why every chart page also renders
  its rows as a table.
- **No dual axes.** Two measures at wildly different scales belong in two
  charts, not on two y-scales.

Reordering those hexes invalidates the validation. Re-run it before changing them.

## Datasets

The **Datasets** tab is a catalog over the analytics database. Every table,
view and materialized view in `analytics.public` is discovered live from the
Postgres catalog — a new table shows up on its own, with nothing to register.
Each dataset gets a page with four things:

| | |
| --- | --- |
| **Preview** | the first 50 rows, under a statement timeout |
| **Data catalog** | every column: name, type, nullability, default |
| **Description** | prose from `datasets.toml` (backticked names render as code) |
| **Example queries** | curated SQL, each with an "Open in Query" button |

`datasets.toml` is a pure curation layer — everything works without it:

```toml
[settings]
hide = ["temp_*", "test_*", "*_20????????????"]   # globs on the object name

[[folder]]
key      = "captura"
title    = "Captura"
match    = ["captura*"]          # by pattern...
datasets = ["clients_bi_usage"]  # ...and/or by name

[[dataset]]
name        = "companies"
description = "One row per company. `c_id` is the tenant id."

  [[dataset.example]]
  title = "Created this month"
  sql   = "SELECT * FROM companies WHERE created_at >= date_trunc('month', now())"
```

Folders ship empty: with none defined the tab renders one flat **All datasets**
list. Add a `[[folder]]` and its members group up; anything unclaimed collects
under **Ungrouped**. The first folder in the file that claims a dataset wins, so
put narrow folders above broad ones.

Like `reports.toml`, the manifest is read fresh on each request — edits take
effect on the next page load, no restart.

A few things worth knowing:

- **Row counts are estimates** (`pg_class.reltuples`), shown as `~1,420`.
  54 of the 267 objects have never been analyzed, and those show `—` rather
  than a misleading zero. Counting 191 tables per page load isn't worth it.
- **A dataset name only reaches SQL after the catalog vouches for it.** Routes
  resolve the name through `get_dataset()` first, and the identifier is quoted
  by the dialect, so a hidden or invented name 404s instead of running.
- **Previews carry a statement timeout** (`DATASET_TIMEOUT_MS`, default 10s) so
  a scan of a large table can't hang the page.

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

## Development & CI

```bash
pip install -r requirements-dev.txt
pytest                  # 14 tests, ~4s, no database or network needed
ruff check . && ruff format --check .
```

`pyproject.toml` holds the pytest and ruff config. It also pins the pytest
rootdir — without a config file here, pytest walks up the filesystem and adopts
the first `pyproject.toml` it finds, which can be a completely unrelated
project's.

**The suite needs no services.** The dataset catalog is stubbed, Keycloak is
faked, and the app database is exercised through its disabled branch. That's a
property worth protecting: the real database lives in a private VPC, so a test
that needed it could never run on a hosted runner.

Two tests (`test_superset_delegated_mode`, `test_sso_only_mode`) `importlib.reload`
`app.main` to exercise the auth modes, so the suite shares process state and
must run in file order. Don't add a random-ordering plugin.

### CI

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

| Job | What it checks |
| --- | --- |
| `python` | ruff lint, ruff format, pytest |
| `assets` | `node --check` on the chart scripts and the vendored Chart.js |
| `templates` | parses all 10 Jinja templates — they're only compiled when a route renders them, so a typo on a page no test covers would otherwise reach production |

### Deploying

```bash
scripts/deploy.sh                # origin/main to the BI host
scripts/deploy.sh --ref v1.2.0
```

The host-side steps are in `scripts/host-update.sh`, piped over SSH so it works
even when the host's checkout is older than that script. It refuses to deploy
when `.env` is missing required keys — including the `AUTH_MODE=sso` with no
Keycloak client combination that once left the app crash-looping — waits for
`/healthz`, and prints the rollback command on failure.

Always `docker compose up -d --build`, never `docker restart`: Docker only
re-reads `env_file` when the container is **recreated**, so a restart silently
keeps the old environment.

`.github/workflows/deploy.yml` does the same thing from Actions via AWS SSM, but
it's manual-dispatch only and inert until two repo variables exist
(`AWS_ROLE_ARN`, `SSM_INSTANCE_ID`) — the host has no public SSH port, so a
hosted runner needs an IAM role to reach it. It fails with those instructions if
they're absent. Both paths run the same `host-update.sh`, so they can't drift.

## Project layout

```
app/
  main.py       FastAPI app: routes, sessions, query console + report export
  auth.py       login dependency + session start/end
  superset_session.py  delegated auth: identity from Superset's /api/v1/me/
  oidc.py       Keycloak OIDC client and claim mapping (AUTH_MODE=sso)
  datasets.py   live catalog + datasets.toml curation layer
  users.py      JSON-backed user store for the break-glass login
  db.py         direct SQLAlchemy connection: run_query / execute / list_databases
  reports.py    manifest-driven reports + CSV/Excel serialization
  config.py     settings from environment
  templates/    login + dashboard pages
reports.toml    report definitions (database + SQL per report)
datasets.toml   dataset folders, descriptions, example queries, hide list
sql/            report SQL files (report.sql is the connectivity check)
manage.py       CLI to add users
smoke_test.py   the test suite (run with `pytest`)
scripts/        deploy.sh (SSH) + host-update.sh (runs on the host)
.github/        CI on every PR; manual-dispatch deploy
```

## Where to go next

- Add more reports (parameterized queries, date ranges).
- Gate access on a Keycloak role (`realm_access.roles`) if the hub ever needs to
  be narrower than "any realm user".
- Add JSON API endpoints for the "data functionalities" you mentioned — FastAPI
  gives you automatic docs at `/docs`.
- Switch to a persistent SSH tunnel with a health check if traffic grows.
```
