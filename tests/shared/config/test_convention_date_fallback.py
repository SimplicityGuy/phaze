"""The fail-closed rollout knobs for the convention-derived date fallback (phaze-5fta.4).

The default of ``convention_date_fallback_enabled`` is not a preference -- it is the epic's DESIGN
note 5. Convention-derived dates must not drive rename proposals until phaze-5fta.5 validates them
against an independent source, because a rename proposal is the one output that permanently
rewrites what is on disk and "internally consistent" was never checked against "correct".
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
    def test_the_two_bars_have_named_conservative_defaults(self) -> None:
        settings = ControlSettings()
        assert settings.convention_date_min_supporting == 50
        assert settings.convention_date_min_purity == pytest.approx(0.99)

    def test_the_evidence_bar_is_operator_settable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """phaze-5fta.5 sets the validated values -- it must not need a code change to do it."""
        monkeypatch.setenv("PHAZE_CONVENTION_DATE_MIN_SUPPORTING", "200")
        assert ControlSettings().convention_date_min_supporting == 200

    def test_the_purity_bar_is_operator_settable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PHAZE_CONVENTION_DATE_MIN_PURITY", "0.95")
        assert ControlSettings().convention_date_min_purity == pytest.approx(0.95)

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
