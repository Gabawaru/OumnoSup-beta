# OumnoSup

**L'admission universitaire, sans frontières.**

OumnoSup collects university admission data — programmes, capacities, deadlines,
fees, languages of instruction and admission statistics — from official national
platforms, normalises it into a single schema, and serves it through a public
REST API.

France is live: **14,252 Parcoursup programmes across 4,058 institutions,
769,351 places**, from the Ministry of Education's open data under the Etalab
Open Licence.

---

## Quick start

```bash
git clone https://github.com/Gabawaru/OumnoSup-beta.git
cd OumnoSup-beta
cp .env.example .env
docker compose up
```

The API is then on <http://localhost:8000>, documented at
<http://localhost:8000/docs>.

The database starts empty. Load France:

```bash
docker compose exec api python -m src.scrapers.france.parcoursup
```

That takes about 20 seconds: one request for the dataset, then the pipeline.

### Without Docker

Requires Python 3.11+ and a PostgreSQL 16 server.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # adjust DATABASE_URL

alembic upgrade head
python -m src.scrapers.france.parcoursup
uvicorn src.api.main:app --reload
```

Redis is optional. Without it the rate limiter falls back to per-process
counters and the response cache switches off; nothing breaks.

### Response caching

`GET` requests under `/api/v1` are cached in Redis for `API_CACHE_TTL_SECONDS`
(300 by default). Responses carry `X-Cache: HIT` or `MISS`, and the header is
omitted entirely when Redis is unreachable — so it never claims a cache that
does not exist. Admin routes are never cached: they are authenticated and report
live run state. The cache is dropped automatically after every scrape, since the
data it describes has just changed.

---

## Using the API

```bash
# Everything stored
curl localhost:8000/api/v1/stats/global

# Bachelor's programmes in Brittany with at least 100 places
curl "localhost:8000/api/v1/programs?country=FR&level=bachelor&region=Bretagne&min_capacity=100"

# Programmes admitting more than half their applicants
curl "localhost:8000/api/v1/programs?min_acceptance_rate=0.5"

# Free-text search
curl "localhost:8000/api/v1/search?q=informatique"

# One programme, with its criteria and statistics
curl localhost:8000/api/v1/programs/{id}
```

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/countries` | Countries and their platforms |
| `GET /api/v1/universities` | Institutions, filterable by country, city, region, type |
| `GET /api/v1/programs` | Programmes, filterable by level, field, region, places, acceptance rate |
| `GET /api/v1/programs/{id}` | One programme with institution, criteria and statistics |
| `GET /api/v1/programs/{id}/criteria` | Admission requirements |
| `GET /api/v1/programs/{id}/stats` | Admission outcomes by year |
| `GET /api/v1/search?q=` | Search programmes, institutions and cities |
| `GET /api/v1/stats/global` | Platform-wide totals and per-country freshness |
| `GET /api/v1/stats/selectivity` | Least and most selective programmes |
| `GET /api/v1/admin/scrape-runs` | Run history — needs `X-Admin-Token` |
| `POST /api/v1/admin/scrape/{platform}` | Trigger a refresh — needs `X-Admin-Token` |

Lists are paginated (20 per page, 100 maximum) and wrapped in
`{items, total, page, page_size, pages, has_next}`. A `page_size` above the
maximum is clamped, not rejected.

---

## How it works

```
scrape → parse → normalize → validate → deduplicate → store
```

Scrapers produce validated items; only the pipeline writes to the database, so
no raw payload ever reaches it. Every stage is a module under `src/pipeline/`.

```
src/
├── core/          settings, async engine, ORM models, schemas, exceptions
├── scrapers/      BaseScraper + mixins, then one package per country
├── pipeline/      normalize, validate, deduplicate, translate, store, export
├── api/           FastAPI app and routes
├── scheduler/     APScheduler jobs, one per enabled country
└── utils/         logging, rate limiting, robots.txt, i18n
```

### Writing a scraper

Subclass `BaseScraper`, set three attributes, implement two methods:

```python
class MyScraper(BaseScraper):
    platform = "myplatform"
    country_code = "XX"
    base_url = "https://example.gov"
    expected_fields = frozenset({"id", "name", "capacity"})

    async def fetch(self) -> Sequence[dict]:
        return (await self.request(f"{self.base_url}/data.json")).json()

    def parse(self, record: dict) -> ScrapedProgram:
        ...
```

`BaseScraper` handles robots.txt, rate limiting, retry with backoff, request
logging, the `scrape_runs` audit row, error isolation and structure-drift
detection. Mixins add JavaScript rendering, proxy rotation and response caching
when a platform needs them.

Every scraper runs standalone:

```bash
python -m src.scrapers.france.parcoursup --limit 100
```

### Scheduling

The scheduler reads `countries.scraping_enabled` and
`countries.scraping_frequency`, so adding a country to the rotation is a
database change, not a deployment. A country that fails never affects the
others.

---

## Scraping policy

Two rules, both enforced in code rather than documented and hoped for.

**robots.txt is a hard stop.** `BaseScraper.robots_policy` defaults to
`ENFORCE`; a disallowed URL aborts the run. When a platform declares a
`Crawl-delay`, the slower of that and the configured delay wins — being asked to
slow down is binding, being asked to speed up is not.

**Exceptions must be declared and justified.** A scraper reading a documented
open-data endpoint published for reuse may declare
`robots_policy = API_CLIENT`, but only alongside a written justification; without
one the fetch is refused. The declaration is logged at the start of every run.
`ParcoursupScraper` is the current example: the dataset carries the Etalab Open
Licence while the portal serves Opendatasoft's stock robots.txt, whose
`Disallow: /api/` keeps search engines out of API URLs.

Scrapers identify themselves through `SCRAPE_USER_AGENT` so an operator can
contact, throttle or exclude us rather than silently blocking.

---

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
createdb oumnosup_test
TEST_DATABASE_URL=postgresql+asyncpg://oumnosup:oumnosup@localhost:5432/oumnosup_test pytest
```

104 tests. The 76 offline ones run anywhere; the 28 database-backed ones skip
unless `TEST_DATABASE_URL` is set.

Tests run against real PostgreSQL, never SQLite: the schema relies on native
enums, `ARRAY`, `JSONB` and `ON CONFLICT`, so a substitute database would pass
tests that production fails.

```bash
alembic revision --autogenerate -m "describe the change"   # after editing models
alembic upgrade head
ruff check src tests && mypy src
```

---

## Deployment

```bash
export POSTGRES_PASSWORD=... SECRET_KEY=... ADMIN_TOKEN=...
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The production overlay keeps PostgreSQL and Redis off the host network, runs
four API workers, and requires every secret from the environment. Settings
refuses to start with `ENVIRONMENT=production` while `SECRET_KEY` or
`ADMIN_TOKEN` still hold their example values, so a misconfigured deploy fails at
boot rather than serving with known-public secrets.

---

## Coverage

| Phase | Countries | Status |
|---|---|---|
| 1 — Europe | France | **live** |
| | UK, Germany, Netherlands, Italy, Spain, Sweden, Finland, Denmark | scaffolded |
| 2 — Americas | USA, Canada, Brazil | scaffolded |
| 3 — Asia | China, Japan, Korea, Taiwan, Singapore, Malaysia, India | scaffolded |
| 4 — Africa & Middle East | Morocco, Tunisia, Senegal | scaffolded |
| 5 — Oceania | Australia, New Zealand | scaffolded |

Scaffolded means the package exists and records its target platform; the scraper
is not written yet.

---

## Data sources and licence

French data comes from the `fr-esr-parcoursup` dataset published by the Ministry
of Education on data.education.gouv.fr under the **Licence Ouverte / Open Licence
(Etalab)**, which permits reuse and redistribution including by automated means.

OumnoSup is operated by the Oumno group. The platform is open to students
everywhere; the organisation running it is private.
