"""phaze-itjat: per-row retry acks are auto-dismissing OOB toasts; the bulk health-card acks stay inline spans."""

from __future__ import annotations

import pytest

from phaze.routers.pipeline._common import templates


_TEMPLATES = ["pipeline/partials/retry_failed_response.html", "pipeline/partials/metadata_retry_response.html"]
_BRANCHES = [
    {"count": 1, "no_active_agent": False},
    {"count": 0, "no_active_agent": False},
    {"count": 0, "no_active_agent": True},
]


@pytest.mark.parametrize("name", _TEMPLATES)
@pytest.mark.parametrize("ctx", _BRANCHES)
def test_per_file_ack_is_an_auto_dismissing_toast(name: str, ctx: dict[str, object]) -> None:
    html = templates.get_template(name).render(**ctx, toast=True)
    assert 'hx-swap-oob="beforeend:#toast-container"' in html
    assert "setTimeout(() => show = false, 5000)" in html
    assert '<span class="text-sm' not in html


@pytest.mark.parametrize("name", _TEMPLATES)
@pytest.mark.parametrize("ctx", _BRANCHES)
def test_bulk_ack_stays_an_inline_span(name: str, ctx: dict[str, object]) -> None:
    html = templates.get_template(name).render(**ctx)
    assert '<span class="text-sm' in html
    assert "toast-container" not in html


@pytest.mark.parametrize(("extra", "text"), [({"enqueue_failed": True}, "could not re-queue"), ({"blocked": True}, "stuck and holding its slot")])
def test_analysis_per_file_error_branches_are_toasts(extra: dict[str, object], text: str) -> None:
    html = templates.get_template(_TEMPLATES[0]).render(count=0, no_active_agent=False, toast=True, **extra)
    assert "toast-container" in html
    assert text in html
