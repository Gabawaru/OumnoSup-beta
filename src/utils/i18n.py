"""Minimal translation catalogue for user-facing strings.

Covers the languages named in the project rules: French, English, Chinese,
Japanese, Korean, Spanish and German. Deliberately dependency-free — the API
serves data, not prose, so the catalogue only needs to carry the handful of
labels and messages the interfaces share.

Program and university names are *not* translated here: those come from source
data and go through :mod:`src.pipeline.translator`, which keeps the original in
``name_local``.
"""

from __future__ import annotations

from typing import Final

__all__ = ["DEFAULT_LANGUAGE", "SUPPORTED_LANGUAGES", "available_languages", "translate"]

DEFAULT_LANGUAGE: Final[str] = "fr"

SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = ("fr", "en", "zh", "ja", "ko", "es", "de")

#: key -> language -> text.
_CATALOGUE: Final[dict[str, dict[str, str]]] = {
    "platform.name": {
        "fr": "OumnoSup", "en": "OumnoSup", "zh": "OumnoSup",
        "ja": "OumnoSup", "ko": "OumnoSup", "es": "OumnoSup", "de": "OumnoSup",
    },
    "platform.tagline": {
        "fr": "L'admission universitaire, sans frontières.",
        "en": "University admission, without borders.",
        "zh": "大学录取，无国界。",
        "ja": "国境なき大学入学。",
        "ko": "국경 없는 대학 입학.",
        "es": "La admisión universitaria, sin fronteras.",
        "de": "Hochschulzulassung ohne Grenzen.",
    },
    "degree.bachelor": {
        "fr": "Licence", "en": "Bachelor", "zh": "学士", "ja": "学士",
        "ko": "학사", "es": "Grado", "de": "Bachelor",
    },
    "degree.master": {
        "fr": "Master", "en": "Master", "zh": "硕士", "ja": "修士",
        "ko": "석사", "es": "Máster", "de": "Master",
    },
    "degree.doctorate": {
        "fr": "Doctorat", "en": "Doctorate", "zh": "博士", "ja": "博士",
        "ko": "박사", "es": "Doctorado", "de": "Promotion",
    },
    "degree.other": {
        "fr": "Autre", "en": "Other", "zh": "其他", "ja": "その他",
        "ko": "기타", "es": "Otro", "de": "Sonstige",
    },
    "field.capacity": {
        "fr": "Places", "en": "Places", "zh": "招生名额", "ja": "定員",
        "ko": "정원", "es": "Plazas", "de": "Plätze",
    },
    "field.acceptance_rate": {
        "fr": "Taux d'admission", "en": "Acceptance rate", "zh": "录取率",
        "ja": "合格率", "ko": "합격률", "es": "Tasa de admisión",
        "de": "Zulassungsquote",
    },
    "field.deadline": {
        "fr": "Date limite", "en": "Deadline", "zh": "截止日期", "ja": "締切",
        "ko": "마감일", "es": "Fecha límite", "de": "Frist",
    },
    "error.not_found": {
        "fr": "Ressource introuvable.", "en": "Resource not found.",
        "zh": "未找到资源。", "ja": "リソースが見つかりません。",
        "ko": "리소스를 찾을 수 없습니다.", "es": "Recurso no encontrado.",
        "de": "Ressource nicht gefunden.",
    },
}


def available_languages() -> tuple[str, ...]:
    """Return the language codes the catalogue supports.

    Returns:
        A tuple of ISO 639-1 codes.
    """
    return SUPPORTED_LANGUAGES


def translate(key: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Look up a translated string.

    Falls back to the default language, then to the key itself. Returning the
    key rather than raising keeps a missing translation from taking down a page.

    Args:
        key: Catalogue key, e.g. ``degree.bachelor``.
        language: Target ISO 639-1 code.

    Returns:
        The translated text, or the best available fallback.
    """
    entry = _CATALOGUE.get(key)
    if entry is None:
        return key
    return entry.get(language) or entry.get(DEFAULT_LANGUAGE) or key
