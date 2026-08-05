# CLAUDE.md — project memory

Decisions already taken and *why*, so future sessions extend the project instead
of re-litigating it. If you are about to change something listed here, read the
reason first — most of these were bugs found against real data, not preferences.

## What this is

A scraper, pipeline, database and public API for university admission data from
official national platforms. France (Parcoursup) is live; the other 23 country
packages are scaffolded placeholders.

Run everything: `docker compose up`. Tests: `pytest` (set `TEST_DATABASE_URL`
for the database-backed ones).

## Decisions that cost real debugging

### The programme natural key is `source_key`, not the name

Programme names are **not unique within an institution**. Parcoursup publishes
several distinct PASS programmes per university under one display name,
differing only by their minor — Clermont Auvergne has eight, totalling 560
places, with applicant counts from 1,263 to 4,048. Keying on
`(institution, name, year)` silently merged **1,404 of 14,252 records**.

`Program.external_id` holds the platform's own code (`cod_aff_form`, unique
across all 14,214 records that carry one). `Program.source_key` is what the
unique constraint and upserts key on: `external_id` when present, the local name
otherwise. The redundancy is deliberate — a nullable column cannot carry a useful
unique constraint in PostgreSQL, and a functional index over `coalesce()` breaks
both `ON CONFLICT` inference and Alembic autogeneration.

Any new scraper must set `external_id` if its platform publishes an identifier.

### `taux_acces_ens` is not the acceptance rate

The Parcoursup column measures the share of candidates who received *an offer*,
as a whole-number percentage (`7` = 7%). `acceptance_rate` in our schema is
`admitted / applicants`, which for the same record is 1.88%. Conflating them puts
a value above 1 into a column constrained to 0–1 and misreports selectivity. The
source figure stays in `raw_data`.

### We do not use `urllib.robotparser`

It ends a record at the first blank line. `parcoursup.gouv.fr` is written as
`User-agent: *` / `Crawl-delay: 10` / blank line / `Disallow: /admin/`, so the
stdlib parser drops every rule after the blank and reports `/admin/` as
**allowed** — it silently under-restricts on a site we scrape.
`src/utils/robots_checker.py` implements RFC 9309 grouping instead.

### The paginated ODS endpoint truncates

`/records` caps `offset + limit` at 10,000, against a 14,252-record dataset. It
returns the first 10,000 and looks successful. Use `/exports/json`, which has no
ceiling.

### Translation is rule-based, not model-backed

Source names are templated — of 3,150 distinct Parcoursup names, 1,677 begin with
"Licence", 313 with "DN MADE". A glossary handles them deterministically,
instantly and for free. A model backend is pluggable but **off by default**:
thousands of calls per run would be slow, costly and non-deterministic, which
would make runs irreproducible and the tests meaningless. Same reasoning applies
to the static exchange-rate table in `normalizer.py`.

### robots.txt policy is declared per scraper and audited

`BaseScraper.robots_policy` defaults to `ENFORCE`. `API_CLIENT` is permitted only
with a written `robots_policy_justification`; without one `check_robots` refuses
the fetch, and there is a test for that. The policy is logged at the start of
every run. This exists so an exemption is a visible, reviewable decision rather
than a config flag someone flips quietly.

Parcoursup uses it: the dataset carries the Etalab Open Licence while the portal
serves Opendatasoft's stock robots.txt, whose `Disallow: /api/` targets
search-engine indexing of API URLs. **The user approved this posture explicitly.**

### Alembic downgrades must drop enum types

Autogenerate drops tables but leaves native enum types behind, so re-applying a
migration fails with "type ... already exists" and a rollback cannot be undone.
The initial migration drops the seven types explicitly. Do the same in any
migration that removes an enum-typed column.

### Constraint naming convention on `Base`

Set in `src/core/database.py`. Without deterministic names, Alembic autogenerate
cannot match a constraint to the model that produced it and starts dropping and
recreating constraints between runs. Do not remove it.

### `expire_on_commit=False`

Mandatory with the asyncio engine: touching an attribute of an expired instance
after commit triggers a lazy refresh outside an await point and raises
`MissingGreenlet`.

### Native enums need `values_callable`

Otherwise PostgreSQL stores member *names* (`BACHELOR`) instead of *values*
(`bachelor`), and every API filter comparing against the lower-case value
silently matches nothing.

## Conventions

- Type hints on all public functions; Google-style docstrings.
- Custom exceptions from `src/core/exceptions.py`; never `raise Exception()`.
- All configuration through `src/core/config.py`; no secret in code.
- Conventional commits in English (`feat:`, `fix:`, `docs:`, `refactor:`).
- Money is `Numeric`, never `Float`.
- Tests run against real PostgreSQL, never SQLite — the schema needs native
  enums, `ARRAY`, `JSONB` and `ON CONFLICT`.

## Verification that matters

Beyond the suite, the property the refresh cycle depends on: **run the same
scrape twice**, and confirm nothing duplicates while `records_updated` takes over
from `records_inserted`. That is the only real proof the natural keys work.

## Not built yet

- Admin dashboard (only the JSON admin routes exist).
- 23 country scrapers.
- `oumno_users` / `oumno_applications` are tables only; no auth, no flows.
- Redis response caching is designed for but not wired into the API routes.
- Nobody has run `docker compose up` end to end — it was written and validated
  with `docker compose config`, but this environment had no Docker daemon.
