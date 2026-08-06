"""OumnoSup — global university admission data platform.

OumnoSup aggregates admission data (programmes, criteria, capacities, deadlines,
fees, languages of instruction, statistics) from official national admission
platforms into a single normalised PostgreSQL database exposed through a public
REST API.

Package layout:
    core        Configuration, database access, ORM models, schemas, exceptions.
    scrapers    One package per country, each built on ``scrapers.base.BaseScraper``.
    pipeline    scrape -> parse -> normalize -> validate -> deduplicate -> store.
    api         Public FastAPI application and admin routes.
    scheduler   APScheduler jobs driving per-platform refresh cycles.
    utils       Cross-cutting helpers (logging, i18n, rate limiting, robots.txt).
"""

__version__ = "0.1.0"
