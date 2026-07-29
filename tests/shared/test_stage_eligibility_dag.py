"""DB-free unit tests for ``ELIGIBILITY_DAG`` topology + the pure ``eligible()`` predicate (Phase 78).

Bucket: ``shared`` (DB-free, no Postgres, no SQLAlchemy). Covers:
- ELIG-01: enrich stages (metadata/analyze) have NO upstream — a discovered file is
  simultaneously eligible for all three in any order.
- ELIG-02: downstream stages gate on upstream conjuncts; apply gates on an APPROVED proposal
  existing (``has_approved_proposal``), NOT on bare ``done(review)``.
- ELIG-03: a FAILED analyze is terminal (excluded from the analyze-eligible set) — the 44.5K
  over-enqueue guard mirrored from ``reenqueue.py:_select_done_analyze_ids``.
- ELIG-04: a FAILED metadata stays eligible for auto-retry, in contrast to the analyze carve-out.
"""

from __future__ import annotations

from phaze.enums.stage import ELIGIBILITY_DAG, Stage, Status, eligible


# --------------------------------------------------------------------------------------
# ELIGIBILITY_DAG topology (ELIG-01 / ELIG-02)
# --------------------------------------------------------------------------------------
def test_dag_enrich_stages_have_no_upstream() -> None:
    assert ELIGIBILITY_DAG[Stage.METADATA] == ()
    assert ELIGIBILITY_DAG[Stage.ANALYZE] == ()


def test_dag_downstream_topology() -> None:
    # phaze-0jpe: TRACKLIST is upstream-INDEPENDENT. It used to be gated on (Stage.FINGERPRINT,);
    # with fingerprinting removed, tracklists come from 1001tracklists and were never a function of
    # audio matching, so the empty tuple is the correct end state, not a dangling upstream.
    assert ELIGIBILITY_DAG[Stage.TRACKLIST] == ()
    assert ELIGIBILITY_DAG[Stage.PROPOSE] == (Stage.METADATA, Stage.ANALYZE)
    assert ELIGIBILITY_DAG[Stage.REVIEW] == (Stage.PROPOSE,)
    assert ELIGIBILITY_DAG[Stage.APPLY] == (Stage.REVIEW,)


def test_dag_covers_every_stage() -> None:
    assert set(ELIGIBILITY_DAG) == set(Stage)


# --------------------------------------------------------------------------------------
# ELIG-01: a discovered file is eligible for every enrich stage in any order
# --------------------------------------------------------------------------------------
def test_discovered_file_eligible_for_all_enrich_stages() -> None:
    status_map: dict[Stage, Status] = {
        Stage.METADATA: Status.NOT_STARTED,
        Stage.ANALYZE: Status.NOT_STARTED,
    }
    assert eligible(status_map, Stage.METADATA) is True
    assert eligible(status_map, Stage.ANALYZE) is True


def test_empty_status_map_eligible_for_enrich() -> None:
    # A missing entry defaults to NOT_STARTED — a freshly discovered file with no derived rows yet.
    assert eligible({}, Stage.METADATA) is True
    assert eligible({}, Stage.ANALYZE) is True


# --------------------------------------------------------------------------------------
# metadata: eligible iff status NOT in (DONE, IN_FLIGHT) — no failure carve-out
# --------------------------------------------------------------------------------------
def test_done_enrich_not_eligible() -> None:
    assert eligible({Stage.METADATA: Status.DONE}, Stage.METADATA) is False


def test_inflight_enrich_not_eligible() -> None:
    assert eligible({Stage.METADATA: Status.IN_FLIGHT}, Stage.METADATA) is False


def test_failed_metadata_still_eligible() -> None:
    assert eligible({Stage.METADATA: Status.FAILED}, Stage.METADATA) is True


# --------------------------------------------------------------------------------------
# ELIG-03: a FAILED analyze is terminal (the only enrich failure carve-out)
# --------------------------------------------------------------------------------------
def test_terminal_failed_analyze_not_eligible() -> None:
    # ELIG-03 — the 44.5K over-enqueue guard: a genuinely un-analyzable file must NEVER auto-loop.
    assert eligible({Stage.ANALYZE: Status.FAILED}, Stage.ANALYZE) is False


def test_not_started_analyze_is_eligible() -> None:
    assert eligible({Stage.ANALYZE: Status.NOT_STARTED}, Stage.ANALYZE) is True


def test_inflight_analyze_not_eligible() -> None:
    assert eligible({Stage.ANALYZE: Status.IN_FLIGHT}, Stage.ANALYZE) is False


# --------------------------------------------------------------------------------------
# ELIG-02: downstream conjuncts
# --------------------------------------------------------------------------------------
def test_tracklist_is_upstream_independent() -> None:
    # phaze-0jpe: TRACKLIST used to be gated on done(FINGERPRINT). With an EMPTY conjunct list the
    # rule reduces to "not already DONE" -- which is the whole predicate, so this must hold with an
    # empty status map (no upstream to satisfy) and fail only once tracklist itself is DONE.
    assert eligible({}, Stage.TRACKLIST) is True
    assert eligible({Stage.METADATA: Status.NOT_STARTED, Stage.ANALYZE: Status.NOT_STARTED}, Stage.TRACKLIST) is True
    # Already tracklisted -> not eligible.
    assert eligible({Stage.TRACKLIST: Status.DONE}, Stage.TRACKLIST) is False


def test_propose_requires_metadata_and_analyze_done() -> None:
    assert eligible({Stage.METADATA: Status.DONE, Stage.ANALYZE: Status.NOT_STARTED}, Stage.PROPOSE) is False
    assert eligible({Stage.METADATA: Status.NOT_STARTED, Stage.ANALYZE: Status.DONE}, Stage.PROPOSE) is False
    assert eligible({Stage.METADATA: Status.DONE, Stage.ANALYZE: Status.DONE}, Stage.PROPOSE) is True
    both_done_proposed = {Stage.METADATA: Status.DONE, Stage.ANALYZE: Status.DONE, Stage.PROPOSE: Status.DONE}
    assert eligible(both_done_proposed, Stage.PROPOSE) is False


def test_review_requires_proposal_exists() -> None:
    assert eligible({Stage.PROPOSE: Status.NOT_STARTED}, Stage.REVIEW) is False
    assert eligible({Stage.PROPOSE: Status.DONE}, Stage.REVIEW) is True
    assert eligible({Stage.PROPOSE: Status.DONE, Stage.REVIEW: Status.DONE}, Stage.REVIEW) is False


# --------------------------------------------------------------------------------------
# ELIG-02: apply gates on an APPROVED proposal, NOT bare done(review)
# --------------------------------------------------------------------------------------
def test_apply_requires_approved_proposal_not_bare_review_done() -> None:
    reviewed = {Stage.REVIEW: Status.DONE, Stage.APPLY: Status.NOT_STARTED}
    # A pending-only proposal (review done, but not approved) is NOT apply-eligible.
    assert eligible(reviewed, Stage.APPLY, has_approved_proposal=False) is False
    # An approved proposal makes it eligible.
    assert eligible(reviewed, Stage.APPLY, has_approved_proposal=True) is True


def test_apply_already_done_not_eligible() -> None:
    applied = {Stage.REVIEW: Status.DONE, Stage.APPLY: Status.DONE}
    assert eligible(applied, Stage.APPLY, has_approved_proposal=True) is False
