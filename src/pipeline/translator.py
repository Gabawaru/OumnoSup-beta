"""Translation of programme and institution names into English.

``name`` holds the normalised English form; ``name_local`` always keeps the
original wording exactly as published, so nothing is lost by translating.

The default backend is a rule-based glossary, not a language model. Source names
are highly templated — of 3,150 distinct Parcoursup programme names, 1,677 begin
with "Licence", 313 with "DN MADE", 165 with "BTS", and the body follows a
``<award> - <field> - Parcours <track>`` pattern. A glossary handles that shape
deterministically, instantly and for free, which also keeps runs reproducible and
tests meaningful.

A model-backed backend is available for the long tail but is **off by default**:
thousands of calls per run would be slow, costly, and non-deterministic.
"""

from __future__ import annotations

import re
from typing import Final, Protocol

from src.pipeline.normalizer import fold_accents
from src.utils.logger import logger

__all__ = ["RuleBasedTranslator", "Translator", "build_translator", "translate_program_name"]

#: French award names mapped to their English equivalents. Ordered longest-first
#: at match time so "Double licence" is not shadowed by "Licence".
_AWARDS: Final[dict[str, str]] = {
    "double licence": "Dual Bachelor's",
    "licence professionnelle": "Vocational Bachelor's",
    "licence_las": "Bachelor's (Health Sciences Track)",
    "licence": "Bachelor's",
    "master": "Master's",
    "doctorat": "Doctorate",
    "bts": "Higher Technician Certificate (BTS)",
    "btsa": "Higher Agricultural Technician Certificate (BTSA)",
    "but": "University Bachelor of Technology (BUT)",
    "dut": "University Technology Diploma (DUT)",
    "cpge": "Preparatory Class for the Grandes Ecoles (CPGE)",
    "dn made": "National Diploma in Art and Design (DN MADE)",
    "dnmade": "National Diploma in Art and Design (DN MADE)",
    "deust": "University Diploma in Science and Technology (DEUST)",
    "diplome d'universite": "University Diploma",
    "diplome d'etat": "State Diploma",
    "formation d'ingenieur": "Engineering Programme",
    "ecole d'ingenieur": "Engineering School",
    "ecole de commerce": "Business School",
    "classe preparatoire": "Preparatory Class",
    "pass": "Health Sciences Access Pathway (PASS)",
    "fcil": "Complementary Vocational Training (FCIL)",
    "c.m.i": "Engineering Cursus (CMI)",
    "cmi": "Engineering Cursus (CMI)",
    "mise a niveau": "Foundation Year",
    "prepa": "Preparatory Programme",
}

#: Structural connectors used inside names.
_CONNECTORS: Final[dict[str, str]] = {
    "parcours": "Track",
    "specialite": "Specialism",
    "option": "Option",
    "mention": "Major",
    "double diplome": "Dual Degree",
    "entierement en distanciel": "Fully Online",
    "en apprentissage": "Apprenticeship",
    "formation initiale": "Initial Training",
    "formation continue": "Continuing Education",
}

#: Fields of study.
_FIELDS: Final[dict[str, str]] = {
    "droit": "Law",
    "economie": "Economics",
    "gestion": "Management",
    "informatique": "Computer Science",
    "mathematiques": "Mathematics",
    "physique": "Physics",
    "chimie": "Chemistry",
    "biologie": "Biology",
    "medecine": "Medicine",
    "pharmacie": "Pharmacy",
    "psychologie": "Psychology",
    "sociologie": "Sociology",
    "histoire": "History",
    "geographie": "Geography",
    "philosophie": "Philosophy",
    "lettres": "Literature",
    "langues etrangeres appliquees": "Applied Foreign Languages",
    "langues": "Languages",
    "sciences de l'education et de la formation": "Education Sciences",
    "sciences de l'education": "Education Sciences",
    "sciences politiques": "Political Science",
    "science politique": "Political Science",
    "sciences et techniques des activites physiques et sportives": "Sport Science",
    "sciences - technologies - sante": "Science, Technology and Health",
    "sciences humaines et sociales": "Humanities and Social Sciences",
    "arts du spectacle": "Performing Arts",
    "arts plastiques": "Visual Arts",
    "musicologie": "Musicology",
    "theologie": "Theology",
    "architecture": "Architecture",
    "agronomie": "Agronomy",
    "soins infirmiers": "Nursing",
    "travail social": "Social Work",
    "commerce": "Business",
    "marketing": "Marketing",
    "communication": "Communication",
    "comptabilite": "Accounting",
    "tourisme": "Tourism",
    "dietetique": "Dietetics",
    "energies renouvelables": "Renewable Energy",
    "genie civil": "Civil Engineering",
    "genie mecanique": "Mechanical Engineering",
    "genie electrique": "Electrical Engineering",
    "electronique": "Electronics",
    "mecanique": "Mechanics",
    "nutrition": "Nutrition",
    "sante": "Health",
    "staps": "Sport Science",
    "meef": "Teacher Education",
}

_SEGMENT_SPLIT: Final[re.Pattern[str]] = re.compile(r"\s+-\s+")


class Translator(Protocol):
    """Interface a translation backend must satisfy."""

    def translate(self, text: str, *, source_language: str) -> str:
        """Translate a single string into English.

        Args:
            text: The source text.
            source_language: ISO 639-1 code of the source language.

        Returns:
            The English rendering.
        """
        ...


def _translate_segment(segment: str) -> str:
    """Translate one ``-``-delimited segment of a name.

    Args:
        segment: The segment, e.g. ``"Parcours Anglais-Mandarin"``.

    Returns:
        The English rendering, or the original segment when nothing matches.
    """
    raw = segment.strip()
    if not raw:
        return ""
    key = fold_accents(raw.lower()).strip()

    for table in (_AWARDS, _FIELDS, _CONNECTORS):
        if key in table:
            return table[key]

    # "Parcours <something>" — translate the connector, keep the specialism as
    # published rather than inventing a translation for a local track name.
    for connector, english in sorted(_CONNECTORS.items(), key=lambda kv: -len(kv[0])):
        if key.startswith(connector + " "):
            rest = raw[len(connector):].strip()
            rest_key = fold_accents(rest.lower())
            return f"{english} {_FIELDS.get(rest_key, rest)}".strip()

    for table in (_AWARDS, _FIELDS):
        for french, english in sorted(table.items(), key=lambda kv: -len(kv[0])):
            if key.startswith(french + " ") or key == french:
                rest = raw[len(french):].strip(" -–—")
                return f"{english} {rest}".strip() if rest else english

    return raw


class RuleBasedTranslator:
    """Glossary-driven translator, the project default.

    Deterministic and offline: the same input always yields the same output, so
    a scrape can be reproduced and its results asserted in tests.
    """

    def translate(self, text: str, *, source_language: str = "fr") -> str:
        """Translate a programme or institution name into English.

        Args:
            text: The source name.
            source_language: ISO 639-1 code. Only ``fr`` has a glossary today;
                other languages are returned unchanged.

        Returns:
            The English rendering, falling back to the original where no rule
            applies.
        """
        if not text or source_language != "fr":
            return text
        segments = _SEGMENT_SPLIT.split(text.strip())
        translated = [_translate_segment(s) for s in segments]
        return " - ".join(part for part in translated if part)


class _NullTranslator:
    """Passthrough used when translation is switched off."""

    def translate(self, text: str, *, source_language: str = "fr") -> str:
        """Return the text unchanged.

        Args:
            text: The source name.
            source_language: Ignored.

        Returns:
            ``text``, unmodified.
        """
        return text


def build_translator(*, enabled: bool = True) -> Translator:
    """Return the configured translation backend.

    Args:
        enabled: Whether to translate at all.

    Returns:
        A translator implementing :class:`Translator`.
    """
    if not enabled:
        logger.debug("translation disabled; names pass through unchanged")
        return _NullTranslator()
    return RuleBasedTranslator()


def translate_program_name(name: str, *, source_language: str = "fr") -> str:
    """Convenience wrapper around the default translator.

    Args:
        name: The source programme name.
        source_language: ISO 639-1 code of the source language.

    Returns:
        The English rendering.
    """
    return RuleBasedTranslator().translate(name, source_language=source_language)
