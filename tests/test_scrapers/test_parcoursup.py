"""Parcoursup parser and scraper contract tests. No network access."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.exceptions import ParserError, RobotsDisallowedError, StructureChangedError
from src.core.models import DegreeLevel, UniversityType
from src.scrapers.base import RobotsPolicyMode
from src.scrapers.france.parcoursup import ParcoursupScraper
from src.scrapers.france.parsers.parcoursup_parser import (
    coerce_int,
    degree_level_for,
    parse_record,
)


class TestCoercion:
    """The two ODS endpoints type the same columns differently."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (20, 20),          # /exports/json returns numbers
            ("20", 20),        # /records returns the same column as a string
            ("1 065", 1065),   # thousands separator
            ("7.0", 7),
            ("", None),
            (None, None),
            ("n/a", None),
            (True, None),      # a bool is not a count
        ],
    )
    def test_handles_both_endpoint_typings(self, value, expected) -> None:
        assert coerce_int(value) == expected


class TestDegreeLevel:
    """Level and duration are derived from the diploma type."""

    @pytest.mark.parametrize(
        ("filiere", "level"),
        [
            ("Licence", DegreeLevel.BACHELOR),
            ("BUT", DegreeLevel.BACHELOR),
            ("IFSI", DegreeLevel.BACHELOR),
            ("Ecole d'Ingénieur", DegreeLevel.MASTER),
            ("BTS", DegreeLevel.OTHER),
            ("CPGE", DegreeLevel.OTHER),
        ],
    )
    def test_known_filieres(self, filiere, level) -> None:
        assert degree_level_for(filiere)[0] is level

    def test_unknown_filiere_is_not_guessed(self) -> None:
        """A new filiere must fall back rather than be assigned a level."""
        assert degree_level_for("Nouvelle Filiere 2030") == (DegreeLevel.OTHER, None)

    def test_business_schools_leave_level_unclaimed(self) -> None:
        """Post-secondary business schools span three- and five-year programmes."""
        level, duration = degree_level_for("Ecole de Commerce")
        assert level is DegreeLevel.OTHER
        assert duration is None


class TestParseRecord:
    """Field mapping against the committed sample."""

    def test_every_sample_record_parses(self, raw_records, parsed_programs) -> None:
        assert len(parsed_programs) == len(raw_records) == 25

    def test_maps_the_core_fields(self, raw_records, parsed_programs) -> None:
        record, program = raw_records[0], parsed_programs[0]
        assert program.capacity == record["capa_fin"]
        assert program.university.external_id == record["cod_uai"]
        assert program.university.city == record["ville_etab"]
        assert program.source_platform == "parcoursup"

    def test_session_becomes_an_academic_year(self, parsed_programs) -> None:
        assert all(p.academic_year == "2025-2026" for p in parsed_programs)

    def test_carries_the_platform_programme_code(self, raw_records, parsed_programs) -> None:
        """external_id is what makes a programme identifiable; names are not unique."""
        assert parsed_programs[0].external_id == str(raw_records[0]["cod_aff_form"])

    def test_acceptance_rate_is_admitted_over_applicants(
        self, raw_records, parsed_programs
    ) -> None:
        """Not taux_acces_ens, which measures offers made and is a percentage."""
        record, stat = raw_records[0], parsed_programs[0].statistics[0]
        expected = (Decimal(record["acc_tot"]) / Decimal(record["voe_tot"])).quantize(
            Decimal("0.0001")
        )
        assert stat.acceptance_rate == expected
        assert stat.acceptance_rate != Decimal(record["taux_acces_ens"]) / 100

    def test_acceptance_rate_always_within_bounds(self, parsed_programs) -> None:
        for program in parsed_programs:
            rate = program.statistics[0].acceptance_rate
            assert rate is None or Decimal(0) <= rate <= Decimal(1)

    def test_keeps_the_full_source_record(self, raw_records, parsed_programs) -> None:
        assert parsed_programs[0].raw_data == raw_records[0]

    def test_maps_institution_ownership(self, parsed_programs) -> None:
        types = {p.university.type for p in parsed_programs}
        assert UniversityType.PUBLIC in types

    @pytest.mark.parametrize(
        ("mutation", "field"),
        [
            ({"g_ea_lib_vx": ""}, "g_ea_lib_vx"),
            ({"lib_for_voe_ins": "", "form_lib_voe_acc": ""}, "lib_for_voe_ins"),
            ({"session": "not-a-year"}, "session"),
        ],
    )
    def test_rejects_unusable_records(self, raw_records, mutation, field) -> None:
        broken = {**raw_records[0], **mutation}
        with pytest.raises(ParserError) as excinfo:
            parse_record(broken)
        assert excinfo.value.field == field


class TestScraperContract:
    """Behaviour BaseScraper imposes on every scraper."""

    def test_declares_platform_and_country(self) -> None:
        assert ParcoursupScraper.platform == "parcoursup"
        assert ParcoursupScraper.country_code == "FR"

    def test_uses_the_export_endpoint_not_paginated_records(self) -> None:
        """The paginated endpoint caps offset+limit at 10000 for a 14252-row set."""
        assert "/exports/json" in ParcoursupScraper.export_url

    def test_api_client_policy_carries_a_justification(self) -> None:
        assert ParcoursupScraper.robots_policy is RobotsPolicyMode.API_CLIENT
        assert ParcoursupScraper.robots_policy_justification

    async def test_api_client_without_justification_is_refused(self) -> None:
        """Declaring the policy without stating why must not grant an exemption."""

        class Undeclared(ParcoursupScraper):
            robots_policy_justification = None

        with pytest.raises(RobotsDisallowedError):
            await Undeclared(persist_run=False).check_robots("https://example.org/api/x")

    def test_intact_records_pass_the_drift_check(self, raw_records) -> None:
        ParcoursupScraper(persist_run=False).check_structure(raw_records)

    def test_a_removed_column_aborts_the_run(self, raw_records) -> None:
        """Silently mapping a renamed column to None would erase good data."""
        drifted = [{k: v for k, v in r.items() if k != "capa_fin"} for r in raw_records]
        with pytest.raises(StructureChangedError) as excinfo:
            ParcoursupScraper(persist_run=False).check_structure(drifted)
        assert "capa_fin" in excinfo.value.missing

    def test_tolerates_a_field_missing_from_one_record(self, raw_records) -> None:
        """Optional fields are legitimately absent from individual records."""
        mixed = [raw_records[0], {k: v for k, v in raw_records[1].items() if k != "capa_fin"}]
        ParcoursupScraper(persist_run=False).check_structure(mixed)
