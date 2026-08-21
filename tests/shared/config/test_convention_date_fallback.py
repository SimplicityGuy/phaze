"""The rollout knobs for the convention-derived date fallback (phaze-5fta.4).

The default of ``convention_date_fallback_enabled`` is not a preference -- it is the epic's DESIGN
note 5. The flag shipped fail-closed because a rename proposal feeds the one workflow that
permanently rewrites what is on disk.

phaze-5fta.5 ran the external validation the note demanded, and it passed (130 derived dates checked
against published event dates from five independent sources: 120 discriminating matches, 9
non-discriminating, 0 contradictions, across 12 release groups). On that evidence the operator
flipped the default ON (2026-08-04). The second question the validation deliberately left open --
"may inferred dates rewrite filenames" -- was answered by the operator, and narrowly: a derived date
reaches a rename PROPOSAL, never the filesystem, and the approval workflow still gates every move.

These tests pin BOTH directions. The default is a decision with a record behind it, so a silent flip
either way should fail the build rather than change behavior on a live archive unnoticed.
"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from phaze.config import ControlSettings


class TestEnabledDefault:
    def test_the_fallback_defaults_on(self) -> None:
        """Enabled by operator decision (2026-08-04) on phaze-5fta.5's passing external validation."""
        assert ControlSettings().convention_date_fallback_enabled is True

    def test_it_stays_one_env_var_from_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Restoring the pre-phaze-5fta behavior must not need a code change or a deploy of one."""
        monkeypatch.setenv("PHAZE_CONVENTION_DATE_FALLBACK_ENABLED", "false")
        assert ControlSettings().convention_date_fallback_enabled is False


class TestThresholds:
    def test_the_two_bars_carry_the_validated_defaults(self) -> None:
        """The values phaze-5fta.5 measured, not the placeholders phaze-5fta.4 shipped.

        50 was confirmed against the live corpus (it sits on the coverage knee: 10 groups and
        84.9% of the grouped ambiguous files, where dropping to 1 would buy 11.8 more points
        across 65 further groups). 1.0 is a REVISION of the provisional 0.99, which at this
        evidence bar admitted an identical group set to 0.95 and passed-or-failed a single
        contradicting file purely on how large its group happened to be.
        """
        settings = ControlSettings()
        assert settings.convention_date_min_supporting == 50
        assert settings.convention_date_min_purity == pytest.approx(1.0)

    def test_the_evidence_bar_is_operator_settable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """phaze-5fta.5 sets the validated values -- it must not need a code change to do it."""
        monkeypatch.setenv("PHAZE_CONVENTION_DATE_MIN_SUPPORTING", "200")
        assert ControlSettings().convention_date_min_supporting == 200

    def test_the_purity_bar_is_operator_settable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Relaxing below unanimity is a deliberate operator act, and stays one env var away.

        0.99 is the specific value that re-admits the one group phaze-5fta.5's raise excluded
        (164 supporting / 1 contradicting = 0.9939), which is why it is the value exercised here.
        """
        monkeypatch.setenv("PHAZE_CONVENTION_DATE_MIN_PURITY", "0.99")
        assert ControlSettings().convention_date_min_purity == pytest.approx(0.99)

    @pytest.mark.parametrize(
        ("variable", "value"),
        [
            ("PHAZE_CONVENTION_DATE_MIN_SUPPORTING", "0"),  # zero evidence is not a bar
            ("PHAZE_CONVENTION_DATE_MIN_PURITY", "1.5"),  # purity is a share, not a score
            ("PHAZE_CONVENTION_DATE_MIN_PURITY", "-0.1"),
        ],
    )
    def test_an_out_of_range_bar_fails_fast_at_startup(self, monkeypatch: pytest.MonkeyPatch, variable: str, value: str) -> None:
        monkeypatch.setenv(variable, value)
        with pytest.raises(ValidationError):
            ControlSettings()
