"""Canonicalisation of heterogeneous scraped values.

Every platform publishes the same facts differently: currencies in local units,
dates in local formats and timezones, institution names with articles and
punctuation that differ between runs. The normaliser converts all of it to one
canonical form so that later stages — matching, deduplication, comparison across
countries — operate on values that are actually comparable.

Nothing here reaches the network. Exchange rates are a static, dated table: a
live rate would make every run produce different numbers and make tests
irreproducible. Refreshing the table is a deliberate, reviewable commit.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from src.core.exceptions import NormalizationError

__all__ = [
    "EXCHANGE_RATES_TO_EUR",
    "RATES_AS_OF",
    "convert_to_eur",
    "match_key",
    "normalize_academic_year",
    "normalize_country_code",
    "normalize_institution_name",
    "normalize_score",
    "to_utc",
]

#: Units of foreign currency per 1 EUR, as of :data:`RATES_AS_OF`.
#:
#: Static on purpose. Live rates would make identical inputs produce different
#: stored values on every run, so a scrape could never be reproduced or tested.
EXCHANGE_RATES_TO_EUR: Final[dict[str, Decimal]] = {
    "EUR": Decimal("1"),
    "USD": Decimal("1.09"),
    "GBP": Decimal("0.84"),
    "CHF": Decimal("0.94"),
    "SEK": Decimal("11.35"),
    "DKK": Decimal("7.46"),
    "NOK": Decimal("11.70"),
    "CAD": Decimal("1.48"),
    "BRL": Decimal("6.10"),
    "MXN": Decimal("19.80"),
    "CLP": Decimal("1020.00"),
    "ARS": Decimal("1090.00"),
    "CNY": Decimal("7.85"),
    "JPY": Decimal("165.00"),
    "KRW": Decimal("1480.00"),
    "TWD": Decimal("35.20"),
    "SGD": Decimal("1.45"),
    "MYR": Decimal("4.85"),
    "INR": Decimal("91.50"),
    "THB": Decimal("37.50"),
    "VND": Decimal("27500.00"),
    "IDR": Decimal("17200.00"),
    "MAD": Decimal("10.75"),
    "TND": Decimal("3.42"),
    "XOF": Decimal("655.957"),
    "DZD": Decimal("146.00"),
    "EGP": Decimal("53.50"),
    "AED": Decimal("4.00"),
    "SAR": Decimal("4.09"),
    "AUD": Decimal("1.65"),
    "NZD": Decimal("1.80"),
    "ZAR": Decimal("19.90"),
}

RATES_AS_OF: Final[date] = date(2025, 1, 1)

#: Leading articles dropped when building a match key, by language.
_LEADING_ARTICLES: Final[tuple[str, ...]] = (
    "the", "la", "le", "les", "l", "de", "het", "der", "die", "das", "el", "los",
    "las", "il", "lo", "gli", "o", "a", "os", "as", "universite", "university",
)

_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_ACADEMIC_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})\s*[-/–]\s*(\d{2,4})$")


def fold_accents(text: str) -> str:
    """Strip diacritics so accented and unaccented spellings compare equal.

    Args:
        text: Any string.

    Returns:
        The string with combining marks removed.
    """
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_country_code(value: str) -> str:
    """Return an upper-cased ISO 3166-1 alpha-2 code.

    Args:
        value: A two-letter country code in any case.

    Returns:
        The code in upper case.

    Raises:
        NormalizationError: If the value is not two letters.
    """
    code = (value or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise NormalizationError(
            "country code must be two letters (ISO 3166-1 alpha-2)",
            field="country_code", value=value,
        )
    return code


def normalize_academic_year(value: str) -> str:
    """Return an academic year in canonical ``YYYY-YYYY`` form.

    Accepts the shapes platforms actually publish: ``2025-2026``, ``2025/26``,
    ``2025–26``.

    Args:
        value: The raw academic year.

    Returns:
        The canonical form.

    Raises:
        NormalizationError: If the value cannot be interpreted, or the two years
            are not consecutive.
    """
    text = (value or "").strip()
    match = _ACADEMIC_YEAR_RE.match(text)
    if not match:
        raise NormalizationError(
            "academic year must look like 2025-2026", field="academic_year", value=value
        )
    start = int(match.group(1))
    tail = match.group(2)
    end = int(tail) if len(tail) == 4 else (start // 100) * 100 + int(tail)
    if end != start + 1:
        raise NormalizationError(
            "academic year must span two consecutive years",
            field="academic_year", value=value,
        )
    return f"{start}-{end}"


def convert_to_eur(amount: Decimal | None, currency: str | None) -> Decimal | None:
    """Convert a monetary amount to euros.

    Args:
        amount: The amount in ``currency``.
        currency: ISO 4217 code.

    Returns:
        The amount in EUR rounded to cents, or ``None`` when ``amount`` is
        ``None``.

    Raises:
        NormalizationError: If the currency is unknown or missing.
    """
    if amount is None:
        return None
    code = (currency or "").strip().upper()
    if not code:
        raise NormalizationError(
            "cannot convert an amount without a currency", field="currency", value=currency
        )
    rate = EXCHANGE_RATES_TO_EUR.get(code)
    if rate is None:
        raise NormalizationError(
            f"no exchange rate on file for {code}; add it to EXCHANGE_RATES_TO_EUR",
            field="currency", value=currency,
        )
    return (amount / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_utc(value: datetime) -> datetime:
    """Return a timezone-aware datetime in UTC.

    A naive datetime is assumed to be UTC already: guessing a local timezone from
    a scraped value would silently shift dates by up to a day.

    Args:
        value: Any datetime.

    Returns:
        The equivalent moment in UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_score(raw: str | None) -> tuple[Decimal, Decimal] | None:
    """Parse a score expressed as ``value/maximum`` into a comparable pair.

    Grading scales are not comparable across countries, so scores are stored
    verbatim and this helper is used only where a comparison is needed. It
    returns both the value and its scale rather than a bare ratio, so callers
    can decide how to compare.

    Args:
        raw: A score such as ``"14/20"`` or ``"100/120"``.

    Returns:
        The ``(value, maximum)`` pair, or ``None`` when the input has no
        recognisable scale.
    """
    if not raw:
        return None
    text = raw.strip().replace(",", ".")
    if "/" not in text:
        return None
    left, _, right = text.partition("/")
    try:
        value, maximum = Decimal(left.strip()), Decimal(right.strip())
    except Exception:
        return None
    if maximum <= 0 or value < 0:
        return None
    return value, maximum


def normalize_institution_name(name: str) -> str:
    """Return a tidy display form of an institution name.

    Collapses whitespace and strips stray punctuation, but keeps the original
    casing and wording — this is what gets shown to users. Use :func:`match_key`
    for comparison instead.

    Args:
        name: The raw institution name.

    Returns:
        The cleaned name.
    """
    return _SPACE_RE.sub(" ", (name or "").strip()).strip(" -–—,;")


def match_key(name: str) -> str:
    """Return a canonical key for comparing two institution or programme names.

    Lower-cases, folds accents, drops punctuation and leading articles, and
    collapses whitespace, so that "Université de Rennes", "UNIVERSITE DE RENNES"
    and "Universite Rennes" all collapse to the same key.

    Args:
        name: The raw name.

    Returns:
        The comparison key, empty when the name carries no usable content.
    """
    text = fold_accents((name or "").lower())
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in _SPACE_RE.split(text) if t]
    while tokens and tokens[0] in _LEADING_ARTICLES:
        tokens.pop(0)
    return " ".join(tokens)
