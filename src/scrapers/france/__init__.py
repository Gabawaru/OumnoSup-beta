"""France admission scrapers.

Target platform: Parcoursup
Platform URL: https://www.parcoursup.gouv.fr
Deployment phase: 1

Data source: the official open data API published by the Ministry of Higher
Education under the Etalab Open Licence, not the Parcoursup web front end::

    https://data.enseignementsup-recherche.gouv.fr/api/explore/v2.1
        /catalog/datasets/fr-esr-parcoursup/records

The dataset exposes one record per programme (14k+ records) with admission
capacity, application volumes and admission outcomes already structured, which
covers the ``programs`` and ``admission_statistics`` tables directly.

Note: ``parcoursup.gouv.fr/robots.txt`` declares ``Crawl-delay: 10`` for generic
user agents. Any fallback that does hit the web front end must honour that delay
rather than the project-wide 1 req/s default.

Status: package scaffolded, scraper not implemented yet.
"""
