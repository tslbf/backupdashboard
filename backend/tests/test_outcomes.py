from __future__ import annotations

import pytest

from app.outcomes import (
    FAILED,
    MISSED,
    RUNNING,
    SUCCESS,
    UNKNOWN,
    WARNING,
    normalize,
    worst,
)


class TestVendorVocabularies:
    """Three consoles, three vocabularies, one set of counts."""

    @pytest.mark.parametrize(
        "source,raw,expected",
        [
            ("veeam", "Success", SUCCESS),
            ("veeam", "Warning", WARNING),
            ("veeam", "Failed", FAILED),
            ("veeam", "None", RUNNING),
            ("nable", 5, SUCCESS),
            ("nable", 8, WARNING),
            ("nable", 2, FAILED),
            ("nable", 3, FAILED),
            ("nable", 7, MISSED),
            ("nable", 10, FAILED),
            ("nable", "Completed", SUCCESS),
            ("nable", "CompletedWithErrors", WARNING),
            ("azure", "Completed", SUCCESS),
            ("azure", "CompletedWithWarnings", WARNING),
            ("azure", "Failed", FAILED),
            ("azure", "InProgress", RUNNING),
            ("azure", "Cancelled", FAILED),
        ],
    )
    def test_known_values(self, source, raw, expected):
        assert normalize(source, raw) == expected

    def test_the_three_success_words_agree(self):
        assert (
            normalize("veeam", "Success")
            == normalize("nable", 5)
            == normalize("azure", "Completed")
            == SUCCESS
        )

    def test_the_three_partial_words_agree(self):
        assert (
            normalize("veeam", "Warning")
            == normalize("nable", 8)
            == normalize("azure", "CompletedWithWarnings")
            == WARNING
        )


class TestRobustness:
    def test_case_and_spacing_are_ignored(self):
        assert normalize("azure", "completed with warnings") == WARNING
        assert normalize("veeam", "  FAILED  ") == FAILED

    def test_none_and_empty_are_unknown_not_success(self):
        assert normalize("veeam", None) == UNKNOWN
        assert normalize("veeam", "") == UNKNOWN

    def test_unrecognized_value_falls_back_on_meaning(self):
        # A vendor adding a new status must not silently read as clean.
        assert normalize("veeam", "FailedToStart") == FAILED
        assert normalize("azure", "CompletedWithSomethingNew") == SUCCESS
        assert normalize("veeam", "Bananas") == UNKNOWN

    def test_warning_is_checked_before_success(self):
        assert normalize("azure", "CompletedWithWarnings") == WARNING

    def test_unmapped_nable_code_is_unknown_not_guessed(self):
        assert normalize("nable", 99) == UNKNOWN


class TestRollup:
    """A server backed up by two tools gets one verdict for the night."""

    def test_worst_wins(self):
        assert worst([SUCCESS, FAILED]) == FAILED
        assert worst([SUCCESS, WARNING]) == WARNING
        assert worst([SUCCESS, SUCCESS]) == SUCCESS

    def test_a_failure_is_not_hidden_behind_a_success(self):
        # The dangerous case: one tool succeeded, so the server "has a backup" —
        # but the failed job still needs someone to look at it.
        assert worst([SUCCESS, FAILED]) != SUCCESS

    def test_missed_outranks_warning(self):
        assert worst([WARNING, MISSED]) == MISSED

    def test_empty_is_unknown(self):
        assert worst([]) == UNKNOWN
