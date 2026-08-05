"""Normalizer, translator, validator and deduplicator tests. No network access."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.core.exceptions import NormalizationError
from src.core.schemas import ScrapedProgram
from src.pipeline.deduplicator import deduplicate, similarity
from src.pipeline.normalizer import (
    convert_to_eur,
    match_key,
    normalize_academic_year,
    normalize_country_code,
    normalize_score,
    to_utc,
)
from src.pipeline.runner import process_items
from src.pipeline.translator import translate_program_name
from src.pipeline.validator import validate_program


class TestAcademicYear:
    @pytest.mark.parametrize(
        "raw", ["2025-2026", "2025/2026", "2025/26", "2025–26", " 2025 - 2026 "]
    )
    def test_accepts_the_shapes_platforms_publish(self, raw) -> None:
        assert normalize_academic_year(raw) == "2025-2026"

    @pytest.mark.parametrize("raw", ["2025", "2025-2027", "abc", ""])
    def test_rejects_anything_else(self, raw) -> None:
        with pytest.raises(NormalizationError):
            normalize_academic_year(raw)


class TestCurrency:
    def test_converts_to_euros(self) -> None:
        assert convert_to_eur(Decimal("109"), "USD") == Decimal("100.00")

    def test_currency_code_is_case_insensitive(self) -> None:
        assert convert_to_eur(Decimal("109"), "usd") == convert_to_eur(Decimal("109"), "USD")

    def test_euro_is_a_no_op(self) -> None:
        assert convert_to_eur(Decimal("170.00"), "EUR") == Decimal("170.00")

    def test_unknown_currency_is_refused_not_guessed(self) -> None:
        with pytest.raises(NormalizationError):
            convert_to_eur(Decimal("1"), "XYZ")

    def test_amount_without_currency_is_refused(self) -> None:
        with pytest.raises(NormalizationError):
            convert_to_eur(Decimal("1"), None)

    def test_rates_are_static_so_runs_are_reproducible(self) -> None:
        assert convert_to_eur(Decimal("100"), "GBP") == convert_to_eur(Decimal("100"), "GBP")


class TestMatchKey:
    def test_folds_accents_case_and_articles(self) -> None:
        assert match_key("Université de Rennes") == match_key("UNIVERSITE RENNES")

    def test_ignores_punctuation(self) -> None:
        assert match_key("Lycée Saint-Louis") == match_key("Lycee Saint Louis")

    def test_distinguishes_genuinely_different_names(self) -> None:
        assert match_key("Université de Rennes") != match_key("Université de Nantes")


class TestMisc:
    def test_naive_datetime_is_assumed_utc(self) -> None:
        """Guessing a local zone would shift scraped dates by up to a day."""
        assert to_utc(datetime(2025, 1, 1, 12)).tzinfo is timezone.utc

    def test_aware_datetime_is_converted(self) -> None:
        assert to_utc(datetime(2025, 1, 1, 12, tzinfo=timezone.utc)).hour == 12

    def test_score_keeps_its_scale(self) -> None:
        assert normalize_score("14/20") == (Decimal("14"), Decimal("20"))

    def test_score_without_a_scale_is_not_invented(self) -> None:
        assert normalize_score("excellent") is None

    def test_country_code_upper_cased(self) -> None:
        assert normalize_country_code("fr") == "FR"

    def test_bad_country_code_refused(self) -> None:
        with pytest.raises(NormalizationError):
            normalize_country_code("FRA")


class TestTranslator:
    @pytest.mark.parametrize(
        ("source", "expected_fragment"),
        [
            ("Licence - Droit", "Bachelor's"),
            ("Licence - Informatique", "Computer Science"),
            ("Double licence - Droit", "Dual Bachelor's"),
            ("BTS - Services", "BTS"),
            ("CPGE - MPSI", "Preparatory"),
            ("DN MADE - Graphisme", "DN MADE"),
        ],
    )
    def test_translates_templated_names(self, source, expected_fragment) -> None:
        assert expected_fragment in translate_program_name(source)

    def test_is_deterministic(self) -> None:
        """A model-backed translator would break reproducibility here."""
        first = translate_program_name("Licence - Sciences politiques")
        assert first == translate_program_name("Licence - Sciences politiques")

    def test_unknown_wording_passes_through(self) -> None:
        assert translate_program_name("Zzz Qqq") == "Zzz Qqq"


class TestValidator:
    def test_accepts_a_real_record(self, parsed_programs) -> None:
        assert validate_program(parsed_programs[0]) is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [("capacity", 999_999), ("duration_years", Decimal("14"))],
    )
    def test_rejects_implausible_values(self, parsed_programs, field, value) -> None:
        data = parsed_programs[0].model_dump()
        data[field] = value
        assert validate_program(ScrapedProgram.model_validate(data)) is not None


class TestDeduplicator:
    def test_collapses_exact_duplicates(self, parsed_programs) -> None:
        report = deduplicate(parsed_programs + parsed_programs, detect_near=False)
        assert len(report.unique) == len(parsed_programs)
        assert report.exact_merged == len(parsed_programs)

    def test_keeps_programmes_that_share_a_name(self, parsed_programs) -> None:
        """Several PASS programmes per university share one display name and
        differ only by their minor. Keying on the name destroys real places."""
        shared = [p for p in parsed_programs if "PASS" in p.name_local]
        if len(shared) < 2:
            pytest.skip("sample carries no name collision")
        report = deduplicate(shared, detect_near=False)
        assert len(report.unique) == len(shared)

    def test_similarity_recognises_near_names(self) -> None:
        assert similarity("Licence Droit", "Licence - Droit") > 0.9

    def test_near_duplicates_are_reported_not_merged(self, parsed_programs) -> None:
        report = deduplicate(parsed_programs, detect_near=True)
        assert len(report.unique) == len(parsed_programs)


class TestPipelineStages:
    def test_runs_end_to_end_in_memory(self, parsed_programs) -> None:
        items, result = process_items(parsed_programs, detect_near=False)
        assert len(items) == len(parsed_programs)
        assert result.rejected == 0

    def test_produces_an_english_name_and_keeps_the_original(self, parsed_programs) -> None:
        items, _ = process_items(parsed_programs, detect_near=False)
        assert items[0].name_local == parsed_programs[0].name_local
        assert items[0].name != items[0].name_local

    def test_translation_can_be_switched_off(self, parsed_programs) -> None:
        items, _ = process_items(parsed_programs, translate=False, detect_near=False)
        assert items[0].name == items[0].name_local
