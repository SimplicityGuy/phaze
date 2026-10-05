"""Pydantic schemas for PUT /api/internal/agent/metadata/{file_id} (phase-25)."""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


# Upper bound on a stored container bitrate, in BITS per second -- see the `bitrate` comment in
# MetadataWriteRequest. Shared with services/metadata.py, which drops a reading past it to None
# BEFORE the request is built, so an implausible reader value can never fail extraction.
MAX_BITRATE_BPS = 1_000_000_000


class MetadataWriteRequest(BaseModel):
    """Tag-metadata write body. Mirrors FileMetadata column shape 1:1 (no agent_id, no file_id -- both come from path/auth).

    Fields are all optional because the agent may have partial knowledge
    depending on file format / read success. Last-write-wins per D-14.
    """

    model_config = ConfigDict(extra="forbid")

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    # year/track_number/bitrate all map to `Integer` (int4) columns (models/metadata.py). Each takes
    # a DOMAIN bound narrower than the int32 column fallback (wire_bounds rule 3, phaze-bd4n):
    #   - year: tag readers represent a year as a 4-digit field (ID3 TYER and friends); 0-9999 covers
    #     every real recording date (including pre-1900 historical transfers) while rejecting a
    #     bogus multi-billion value a buggy tag reader could otherwise emit.
    #   - track_number: no release (including box sets) plausibly has more than a few hundred
    #     tracks; 0-9999 is generous headroom on the same 4-digit-field convention as year.
    #   - bitrate: BITS per second, matching what every mutagen format reader (`wave.py`,
    #     `flac.py`, `mp4.py`, `mp3.py`, ...) actually reports (phaze-iw2k -- this comment
    #     previously said "kbps", which was the bug: a kbps-sized bound rejected every
    #     stereo CD-quality-or-better WAV/AIFF and 24-bit hi-res FLAC/ALAC, which extract_tags
    #     has always stored in bps). The original 0-50,000,000 bound covered even absurd multichannel
    #     hi-res PCM (e.g. 8ch/24-bit/192kHz = 36,864,000 bps) -- but it was sized for audio
    #     only, and video containers are extracted too (phaze-3p82d: a real 82.3 Mbps mp4 failed
    #     extraction). MAX_BITRATE_BPS (1 Gbps) covers high-bitrate video (UHD Blu-ray tops out at
    #     128 Mbps; mezzanine codecs such as ProRes run to hundreds of Mbps) while staying well inside the int4
    #     column ceiling (2,147,483,647). It also sits below every value mutagen's ASF reader
    #     emits when it misreads a VIDEO stream's type-specific data as WAVEFORMATEX: that value
    #     is (0x02 | N << 8 | N << 24) * 8 for a BITMAPINFOHEADER of N >= 40 bytes, i.e. at
    #     least 5,368,791,056 bps. Display call sites (services/review.py,
    #     services/dedup.py) divide by 1000 to render kbps -- the STORED unit is bps, not kbps.
    #     (templates/duplicates/partials/comparison_table.html was a third such site until
    #     phaze-ur8o3 deleted it as unreachable; the stored unit is unchanged by that.)
    year: int | None = Field(default=None, ge=0, le=9999)
    genre: str | None = None
    track_number: int | None = Field(default=None, ge=0, le=9999)
    duration: float | None = Field(default=None, ge=0.0)
    bitrate: int | None = Field(default=None, ge=0, le=MAX_BITRATE_BPS)
    raw_tags: dict | None = None


class MetadataWriteResponse(BaseModel):
    """Minimal echo response confirming the metadata write."""

    agent_id: str
    file_id: uuid.UUID


class MetadataFailurePayload(BaseModel):
    """Optional triage body for POST /metadata/{file_id}/failed (FAIL-02 / D-10).

    Mirrors ``AnalysisFailurePayload`` (schemas/agent_analysis.py) verbatim: a new agent
    image POSTs this so the persisted ``metadata`` failure row carries a triage
    ``error_message``; an OLD (bodyless) agent still gets a 200 because
    ``report_metadata_failed`` binds ``body: MetadataFailurePayload | None = None`` (CR-02
    version-skew guard, D-10). ``reason`` is a ``Literal`` so the wire can only carry the
    three classifications; ``error`` is a bounded free-text detail (``max_length`` caps the
    DoS-via-huge-string threat, T-81-03-04). ``extra='forbid'`` rejects any attempt to
    smuggle an ``agent_id``/``file_id`` in the body (AUTH-01, T-81-03-02 -> 422).
    """

    model_config = ConfigDict(extra="forbid")

    reason: Literal["timeout", "crashed", "error"]
    error: str | None = Field(default=None, max_length=2000)


class MetadataFailureResponse(BaseModel):
    """Success body of POST /metadata/{file_id}/failed (L-02 / CR-02).

    The terminal-ack endpoint the metadata task calls on a retries-exhausted
    failure so every ``extract_file_metadata`` run clears its
    ``extract_file_metadata:<file_id>`` scheduling-ledger row exactly once (the
    success path clears via ``put_metadata``). ``cleared`` is always ``True`` --
    the clear is a no-op when the row is already absent, but the ack semantics
    are "the row is gone now" regardless.
    """

    agent_id: str
    file_id: uuid.UUID
    cleared: Literal[True]
