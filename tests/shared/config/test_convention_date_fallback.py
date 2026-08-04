"""The fail-closed rollout knobs for the convention-derived date fallback (phaze-5fta.4).

The default of ``convention_date_fallback_enabled`` is not a preference -- it is the epic's DESIGN
note 5. A rename proposal is the one output that permanently rewrites what is on disk.

phaze-5fta.5 has since run the external validation the note demanded, and it passed (130 derived
dates checked against published event dates from five independent sources: 120 discriminating
matches, 9 non-discriminating, 0 contradictions, across 12 release groups). The flag still defaults
OFF, because "the derived dates are correct" and "inferred dates may rewrite filenames" are
different questions and only the first has been answered here.
"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from phaze.config import ControlSettings


class TestFailClosedDefault:
    def test_the_fallback_defaults_off(self) -> None:
        assert ControlSettings().convention_date_fallback_enabled is False

    def test_it_takes_an_explicit_operator_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PHAZE_CONVENTION_DATE_FALLBACK_ENABLED", "true")
        assert ControlSettings().convention_date_fallback_enabled is True


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
