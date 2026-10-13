"""Shared attention derivation: existing Summary policy, independent of transport and persistence."""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

from phaze.utils.humanize import format_count
from phaze.web.template_globals import open_label


class AttentionInputs(Protocol):
    """The read-only slice of Summary inputs needed by attention policy."""

    @property
    def stalled_analyses(self) -> int: ...

    @property
    def inadmissible_count(self) -> int: ...

    @property
    def awaiting_cloud_count(self) -> int: ...

    @property
    def awaiting_hold_reason(self) -> str | None: ...

    @property
    def queued_behind_quota_count(self) -> int: ...

    @property
    def stage_paused(self) -> dict[str, bool]: ...

    @property
    def active_fileservers(self) -> int | None: ...

    @property
    def lanes(self) -> list[dict[str, Any]]: ...


def _stage_count(bucket: dict[str, int | None], key: str) -> int:
    return int(bucket.get(key) or 0)


def analysis_attention_known(
    lanes: list[dict[str, Any]], awaiting_count: int, hold_reason: str | None, active_fileservers: int | None = None
) -> bool:
    """Unavailable connections are measured incidents; unreadable queue/hold measurements are unknown.

    A measured absence of fileservers explains the missing local queue through its own incident;
    it does not invent local queue counts. An absent queue with a live/unknown fileserver is unmeasured.
    """
    return (
        bool(lanes)
        and all(
            lane.get("available") is not None
            and ((lane.get("stuck") is not None or active_fileservers == 0) if lane["kind"] == "local" else lane.get("in_flight") is not None)
            for lane in lanes
        )
        and not (awaiting_count and hold_reason in {None, "held", "reason unavailable"})
    )


def _stage_paused_with_pending_work(bucket: dict[str, int | None], paused: bool) -> bool:
    return bool(paused and (_stage_count(bucket, "not_started") or _stage_count(bucket, "in_flight")))


def _attention_item(
    priority: int,
    title: str,
    detail: str,
    href: str,
    action: str,
    tone: str = "attention",
    *,
    identity: str = "",
    count: int = 1,
    scope: str = "pipeline",
) -> dict[str, str | int]:
    return {
        "id": identity or title,
        "count": count,
        "scope": scope,
        "priority": priority,
        "title": title,
        "detail": detail,
        "href": href,
        "action": action,
        "tone": tone,
    }


def _awaiting_cloud_target(hold_reason: str) -> tuple[str, str]:
    """Resolve the (href, action) pair that best explains an awaiting-cloud hold reason."""
    if hold_reason in {"cloud routing disabled", "held — cloud routing paused (force-local)"}:
        return "/s/runtime-config", open_label("runtime-config")
    if hold_reason in {"held — no cloud backend reachable", "held — no fileserver agent online"} or hold_reason.startswith(
        "held — all lanes at capacity"
    ):
        return "/s/agents", open_label("agents")
    return "/s/analyze", open_label("analyze")


def _failure_attention_items(metadata_failed: int, analyze_failed: int, orphan_total: int, stalled_analyses: int) -> list[dict[str, str | int]]:
    """Attention items for terminal failures and orphaned work (priorities 10/11/20)."""
    items: list[dict[str, str | int]] = []
    if metadata_failed:
        items.append(
            _attention_item(
                10,
                "Metadata failures",
                f"{format_count(metadata_failed)} file(s) need inspection or retry.",
                "/s/files?stage=metadata&bucket=failed&scope=media",
                open_label("files"),
                "danger",
                identity="metadata-failed",
                count=metadata_failed,
                scope="metadata",
            )
        )
    if analyze_failed:
        stalled_detail = f"; {format_count(stalled_analyses)} stopped by the progress watchdog" if stalled_analyses else ""
        items.append(
            _attention_item(
                11,
                "Analysis failures",
                f"{format_count(analyze_failed)} file(s) reached terminal failure{stalled_detail}.",
                "/s/files?stage=analyze&bucket=failed&scope=media",
                "Open failed files",
                "danger",
                identity="analysis-failed",
                count=analyze_failed,
                scope="analyze",
            )
        )
    if orphan_total:
        items.append(
            _attention_item(
                20,
                "Orphaned work",
                f"{format_count(orphan_total)} scheduled file(s) have no live job or domain result.",
                "/s/analyze#analyze-recovery",
                "Open recovery",
                identity="pipeline-orphans",
                count=orphan_total,
            )
        )
    return items


def _capacity_attention_items(metadata: dict[str, int | None], analyze: dict[str, int | None], inputs: AttentionInputs) -> list[dict[str, str | int]]:
    """Attention items for cloud/compute capacity and stage pauses (priorities 30-50)."""
    items: list[dict[str, str | int]] = []
    if inputs.inadmissible_count:
        items.append(
            _attention_item(
                30,
                "Cloud jobs blocked by configuration",
                f"{format_count(inputs.inadmissible_count)} active cloud job(s) are Inadmissible.",
                "/s/agents",
                open_label("agents"),
                identity="cloud-inadmissible",
                count=inputs.inadmissible_count,
                scope="analyze",
            )
        )
    if (
        inputs.awaiting_cloud_count
        and inputs.awaiting_hold_reason not in {None, "held", "reason unavailable"}
        and not (
            inputs.awaiting_hold_reason.startswith(("held — all lanes at capacity", "queued —"))
            or inputs.awaiting_hold_reason == "held — no cloud backend reachable"
            or inputs.awaiting_hold_reason.startswith("held — control plane unreachable")
            or inputs.awaiting_hold_reason == "held — no fileserver agent online"
        )
    ):
        hold_reason = inputs.awaiting_hold_reason or "reason unavailable"
        href, action = _awaiting_cloud_target(hold_reason)
        items.append(
            _attention_item(
                40,
                "Files awaiting cloud routing",
                f"{format_count(inputs.awaiting_cloud_count)} file(s): {hold_reason}.",
                href,
                action,
                identity="cloud-routing-held",
                count=inputs.awaiting_cloud_count,
                scope="analyze",
            )
        )
    for priority, stage_name, stage_label, stage_bucket in (
        (45, "metadata", "Metadata enrichment", metadata),
        (46, "analyze", "Audio analysis", analyze),
    ):
        if _stage_paused_with_pending_work(stage_bucket, inputs.stage_paused[stage_name]):
            items.append(
                _attention_item(
                    priority,
                    f"{stage_label} is paused",
                    f"{_stage_count(stage_bucket, 'not_started')} not started; {_stage_count(stage_bucket, 'in_flight')} still marked in flight.",
                    f"/s/{stage_name}",
                    open_label(stage_name),
                    identity=f"{stage_name}-paused",
                    count=_stage_count(stage_bucket, "not_started") + _stage_count(stage_bucket, "in_flight"),
                    scope=stage_name,
                )
            )
    if inputs.active_fileservers == 0:
        items.append(
            _attention_item(
                50,
                "No file-server agent available",
                "Discovery and file-owned work cannot be dispatched until an agent checks in.",
                "/s/agents",
                open_label("agents"),
                identity="fileserver-unavailable",
                count=1,
            )
        )
    return items


def _lane_attention_items(lanes: list[dict[str, Any]]) -> list[dict[str, str | int]]:
    """Independent causes per lane; connection availability never means a free slot."""
    items: list[dict[str, str | int]] = []
    for lane in lanes:
        lane_id = str(lane["id"])
        href = f"/s/analyze?lane={quote(lane_id, safe='')}"
        label = f"{lane['kind']} · {lane_id}"
        causes: list[tuple[str, int, str, str, str]] = []
        if lane.get("stuck"):
            causes.append(
                (
                    "stuck",
                    int(lane["stuck"]),
                    "Stuck analysis work",
                    "Claimed but not started, stranded, or silent past the heartbeat bound. Inspect the worker and waiting list.",
                    "danger",
                )
            )
        if lane.get("breaker_held"):
            causes.append(
                (
                    "held",
                    1,
                    "Lane dispatch held",
                    "Pods cannot reach the control plane. No attempts are charged; dispatch resumes after a successful probe. Check DNS and the network path."
                    + (f" Recorded reason: {lane['breaker_reason']}" if lane.get("breaker_reason") else ""),
                    "danger",
                )
            )
        if lane.get("available") is False:
            causes.append(
                (
                    "unavailable",
                    1,
                    "Analysis lane unavailable",
                    "The lane connection is unavailable; new work waits for connectivity. Inspect the lane and its agent or cluster connection.",
                    "attention",
                )
            )
        in_flight, cap = lane.get("in_flight"), lane.get("cap")
        if lane["kind"] != "local" and in_flight is not None and cap and in_flight > cap:
            causes.append(
                (
                    "over-capacity",
                    int(in_flight) - int(cap),
                    "Lane exceeds capacity",
                    f"{format_count(in_flight)} in-flight jobs exceed scheduler cap {format_count(cap)}. Stop new admission and inspect the lane.",
                    "danger",
                )
            )
        for cause, count, title, detail, tone in causes:
            items.append(
                _attention_item(
                    25, f"{title}: {label}", detail, href, "Open lane", tone, identity=f"lane:{lane_id}:{cause}", count=count, scope="analyze"
                )
            )
    return items


def derive_attention_items(
    metadata: dict[str, int | None],
    analyze: dict[str, int | None],
    failures: tuple[int, int, int],
    inputs: AttentionInputs,
) -> list[dict[str, str | int]]:
    """Assemble the priority-sorted attention-queue entries for the Summary overview."""
    metadata_failed, analyze_failed, orphan_total = failures
    items = (
        _failure_attention_items(metadata_failed, analyze_failed, orphan_total, inputs.stalled_analyses)
        + _lane_attention_items(inputs.lanes)
        + _capacity_attention_items(metadata, analyze, inputs)
    )
    items.sort(key=lambda item: int(item["priority"]))
    return items
