"""Parcoursup scraper (France).

Reads the ministry's official open data rather than the Parcoursup web front
end. The dataset carries admission capacity, application volumes and admission
outcomes for every programme in the national platform, already structured::

    https://data.education.gouv.fr/api/explore/v2.1
        /catalog/datasets/fr-esr-parcoursup/exports/json

Run it directly::

    python -m src.scrapers.france.parcoursup --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from typing import Any, ClassVar, Final

from src.core.exceptions import ScraperError
from src.core.schemas import ScrapedProgram
from src.scrapers.base import BaseScraper, RobotsPolicyMode, ScrapeResult
from src.scrapers.france.parsers.parcoursup_parser import (
    PARCOURSUP_REQUIRED_FIELDS,
    parse_record,
)

__all__ = ["ParcoursupScraper"]

_HOST: Final[str] = "https://data.education.gouv.fr"
_DATASET: Final[str] = "fr-esr-parcoursup"


class ParcoursupScraper(BaseScraper):
    """Scrapes French admission data from the Parcoursup open dataset."""

    platform: ClassVar[str] = "parcoursup"
    country_code: ClassVar[str] = "FR"
    base_url: ClassVar[str] = _HOST

    # The dataset is published by the Ministry of Education under the Etalab
    # Open Licence, which grants the right to reuse and redistribute it,
    # including by automated means. The portal's robots.txt is Opendatasoft's
    # stock file: it carries `Disallow: /api/` for every agent but Googlebot,
    # which keeps search engines from indexing API URLs. This scraper consumes a
    # documented export endpoint as an API client rather than crawling the site,
    # identifies itself, and rate-limits itself. Reviewed and accepted as a
    # deliberate, recorded exception — not a silent bypass.
    robots_policy: ClassVar[RobotsPolicyMode] = RobotsPolicyMode.API_CLIENT
    robots_policy_justification: ClassVar[str] = (
        "documented open data export, Etalab Open Licence; portal robots.txt is "
        "Opendatasoft boilerplate targeting search-engine indexing of /api/ URLs"
    )

    expected_fields: ClassVar[frozenset[str]] = PARCOURSUP_REQUIRED_FIELDS

    #: Full-dataset export. Unlike the paginated `/records` endpoint, this one is
    #: not subject to the portal's `offset + limit <= 10000` ceiling, which would
    #: otherwise silently truncate a 14k-record dataset to the first 10k.
    export_url: ClassVar[str] = (
        f"{_HOST}/api/explore/v2.1/catalog/datasets/{_DATASET}/exports/json"
    )

    async def fetch(self) -> Sequence[dict[str, Any]]:
        """Download the full dataset in a single request.

        Returns:
            Every published programme record.

        Raises:
            ScraperError: If the response is not the expected JSON array.
        """
        response = await self.request(self.export_url)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ScraperError(
                f"Parcoursup export returned non-JSON content: {exc}",
                platform=self.platform, url=self.export_url,
            ) from exc

        if not isinstance(payload, list):
            raise ScraperError(
                f"expected a JSON array, got {type(payload).__name__}",
                platform=self.platform, url=self.export_url,
            )
        return payload

    def parse(self, record: dict[str, Any]) -> ScrapedProgram:
        """Turn one dataset record into a validated item.

        Args:
            record: A single record from :meth:`fetch`.

        Returns:
            The parsed programme.
        """
        return parse_record(record)


async def _amain() -> ScrapeResult:
    """Parse arguments and run the scraper.

    Returns:
        The run outcome.
    """
    parser = argparse.ArgumentParser(description="Scrape Parcoursup open data.")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many records (smoke runs)")
    parser.add_argument("--no-persist", action="store_true",
                        help="skip writing the scrape_runs audit row")
    args = parser.parse_args()

    result = await ParcoursupScraper.main(limit=args.limit, persist_run=not args.no_persist)
    print(
        f"{result.status.value}: {result.scraped} fetched, "
        f"{len(result.items)} parsed, {result.failed} failed"
    )
    return result


if __name__ == "__main__":
    asyncio.run(_amain())
