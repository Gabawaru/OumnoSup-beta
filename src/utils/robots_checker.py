"""robots.txt fetching, parsing and caching.

Requirement 1 of the BaseScraper contract: nothing is fetched before robots.txt
has been consulted.

This module deliberately does **not** use :mod:`urllib.robotparser`. That parser
treats a blank line as the end of a record, so a file written like::

    User-agent: *
    Crawl-delay: 10

    Disallow: /admin/

loses every rule after the blank line and reports ``/admin/`` as allowed.
parcoursup.gouv.fr is written exactly that way, so the stdlib parser silently
under-restricts on a site this project scrapes. Group matching here follows
RFC 9309 instead: a group runs until the next ``User-agent`` line, blank lines
are insignificant, and the longest matching path rule wins with ``Allow``
breaking ties.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from src.core.config import get_settings
from src.utils.logger import logger

__all__ = ["RobotsChecker", "RobotsPolicy", "RuleGroup", "parse_robots_txt"]


@dataclass(slots=True)
class RuleGroup:
    """One ``User-agent`` group and the rules that apply to it.

    Attributes:
        agents: Lower-cased user-agent tokens the group applies to.
        rules: ``(allow, path)`` pairs in file order.
        crawl_delay: Seconds requested between requests, if declared.
    """

    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)
    crawl_delay: float | None = None


def _path_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a robots.txt path pattern into a regular expression.

    Supports the two wildcards RFC 9309 defines: ``*`` for any run of characters
    and a trailing ``$`` anchoring the end of the path.

    Args:
        pattern: The raw path from an ``Allow``/``Disallow`` line.

    Returns:
        A compiled pattern matched against the start of a URL path.
    """
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    escaped = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.compile(f"^{escaped}{'$' if anchored else ''}")


def parse_robots_txt(text: str) -> list[RuleGroup]:
    """Parse a robots.txt document into its groups.

    Args:
        text: The raw file contents.

    Returns:
        The groups in file order.
    """
    groups: list[RuleGroup] = []
    current: RuleGroup | None = None
    # A `User-agent` line directly after a rule line opens a new group; one after
    # another `User-agent` line extends the current group's agent list.
    last_was_agent = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        key = field_name.strip().lower()
        value = value.strip()

        if key == "user-agent":
            if current is None or not last_was_agent:
                current = RuleGroup()
                groups.append(current)
            current.agents.append(value.lower())
            last_was_agent = True
            continue

        if current is None:
            continue
        last_was_agent = False

        if key in {"allow", "disallow"}:
            # `Disallow:` with an empty value means "nothing is disallowed".
            if key == "disallow" and not value:
                continue
            current.rules.append((key == "allow", value))
        elif key == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                logger.debug("ignoring unparsable Crawl-delay value {!r}", value)

    return groups


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """The rules in force for one host.

    Attributes:
        groups: Every parsed group.
        allow_all: True when the host published no usable restrictions.
        fetched_at: Monotonic timestamp of the fetch, for cache expiry.
    """

    groups: tuple[RuleGroup, ...]
    allow_all: bool
    fetched_at: float

    def group_for(self, user_agent: str) -> RuleGroup | None:
        """Return the most specific group matching a user agent.

        Args:
            user_agent: The full user-agent string.

        Returns:
            The best-matching group, the ``*`` group, or ``None``.
        """
        ua = user_agent.lower()
        best: RuleGroup | None = None
        best_len = -1
        wildcard: RuleGroup | None = None
        for group in self.groups:
            for agent in group.agents:
                if agent == "*":
                    if wildcard is None:
                        wildcard = group
                elif agent in ua and len(agent) > best_len:
                    best, best_len = group, len(agent)
        return best or wildcard

    def can_fetch(self, user_agent: str, path: str) -> bool:
        """Whether a path may be fetched.

        The longest matching rule wins; ``Allow`` breaks a tie, per RFC 9309.

        Args:
            user_agent: The full user-agent string.
            path: URL path, including any query string.

        Returns:
            ``True`` if the fetch is permitted.
        """
        if self.allow_all:
            return True
        group = self.group_for(user_agent)
        if group is None:
            return True
        decision: bool | None = None
        best_len = -1
        for allow, pattern in group.rules:
            # Wildcards make the literal length a poor proxy for specificity, so
            # compare on the pattern length as the RFC prescribes.
            if _path_to_regex(pattern).match(path) and (
                len(pattern) > best_len or (len(pattern) == best_len and allow)
            ):
                    decision, best_len = allow, len(pattern)
        return True if decision is None else decision

    def crawl_delay_for(self, user_agent: str) -> float | None:
        """The ``Crawl-delay`` declared for a user agent, if any.

        Args:
            user_agent: The full user-agent string.

        Returns:
            The delay in seconds, or ``None``.
        """
        group = self.group_for(user_agent)
        return group.crawl_delay if group else None


class RobotsChecker:
    """Fetches and caches robots.txt, and answers questions about it.

    Args:
        user_agent: The agent string rules are matched against.
        cache_ttl: Seconds a fetched policy stays valid.
        timeout: Per-request timeout when fetching robots.txt.
    """

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        cache_ttl: float = 3600.0,
        timeout: float = 10.0,
    ) -> None:
        settings = get_settings()
        self.user_agent = user_agent or settings.scrape_user_agent
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self._cache: dict[str, RobotsPolicy] = {}

    @staticmethod
    def _origin(url: str) -> str:
        """Return the scheme+host origin of a URL.

        Args:
            url: Any absolute URL.

        Returns:
            The origin, e.g. ``https://example.org``.
        """
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    @staticmethod
    def _path_of(url: str) -> str:
        """Return the path and query of a URL, as robots.txt rules match it."""
        parts = urlsplit(url)
        path = unquote(parts.path) or "/"
        return f"{path}?{parts.query}" if parts.query else path

    async def _load(self, origin: str) -> RobotsPolicy:
        """Fetch and parse robots.txt for an origin, using the cache when warm.

        Failure handling follows RFC 9309: a 4xx means there are no rules and
        everything is allowed, while a 5xx or a network error means the rules are
        unknown and a complete disallow must be assumed. Failing closed is
        deliberate — a transient error must not become licence to crawl a site
        that may have forbidden it.

        Args:
            origin: Scheme+host to load rules for.

        Returns:
            The policy in force for that origin.
        """
        cached = self._cache.get(origin)
        if cached is not None and (time.monotonic() - cached.fetched_at) < self.cache_ttl:
            return cached

        robots_url = urljoin(origin, "/robots.txt")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            ) as client:
                response = await client.get(robots_url)
        except httpx.HTTPError as exc:
            logger.warning(
                "robots.txt unreachable at {} ({}); treating as disallow-all", robots_url, exc
            )
            policy = RobotsPolicy((), allow_all=False, fetched_at=time.monotonic())
            self._cache[origin] = policy
            return policy

        if response.status_code >= 500:
            logger.warning(
                "robots.txt returned HTTP {} at {}; treating as disallow-all",
                response.status_code, robots_url,
            )
            policy = RobotsPolicy((), allow_all=False, fetched_at=time.monotonic())
        elif response.status_code >= 400:
            logger.debug(
                "no robots.txt at {} (HTTP {}); everything allowed",
                robots_url, response.status_code,
            )
            policy = RobotsPolicy((), allow_all=True, fetched_at=time.monotonic())
        else:
            groups = tuple(parse_robots_txt(response.text))
            policy = RobotsPolicy(groups, allow_all=not groups, fetched_at=time.monotonic())
            logger.debug(
                "robots.txt loaded from {} ({} groups, crawl-delay={})",
                robots_url, len(groups), policy.crawl_delay_for(self.user_agent),
            )

        self._cache[origin] = policy
        return policy

    async def can_fetch(self, url: str) -> bool:
        """Whether robots.txt permits fetching a URL.

        Args:
            url: The absolute URL to test.

        Returns:
            ``True`` if the fetch is permitted.
        """
        policy = await self._load(self._origin(url))
        return policy.can_fetch(self.user_agent, self._path_of(url))

    async def crawl_delay(self, url: str) -> float | None:
        """The ``Crawl-delay`` declared for this agent, if any.

        Args:
            url: Any URL on the host in question.

        Returns:
            The delay in seconds, or ``None`` when none is declared.
        """
        policy = await self._load(self._origin(url))
        return policy.crawl_delay_for(self.user_agent)

    async def effective_delay(self, url: str, default_delay: float) -> float:
        """The delay to actually use between requests to a host.

        Takes the slower of the configured default and the host's declared
        ``Crawl-delay``. Being asked to slow down is binding; being asked to
        speed up is not.

        Args:
            url: Any URL on the host in question.
            default_delay: The project-configured delay in seconds.

        Returns:
            The delay to apply, in seconds.
        """
        if not get_settings().scrape_respect_robots_crawl_delay:
            return default_delay
        declared = await self.crawl_delay(url)
        if declared is None:
            return default_delay
        if declared > default_delay:
            logger.info(
                "honouring robots.txt Crawl-delay of {}s for {} (default was {}s)",
                declared, self._origin(url), default_delay,
            )
        return max(default_delay, declared)
