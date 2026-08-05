"""Country scrapers.

Every scraper subclasses ``src.scrapers.base.BaseScraper``, which enforces
robots.txt compliance, rate limiting, retry with exponential backoff, structured
request logging and persistence of each run into the ``scrape_runs`` table.

Scrapers are grouped by country and each one is independently runnable::

    python -m src.scrapers.france.parcoursup

A failure in one country never aborts the others.
"""
