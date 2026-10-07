"""The linking chain's rules, one by one, over a synthetic in-memory agent (phaze-rmhfr).

Pure: no database. Every threshold and tie rule is the one ``docs/spikes/phaze-9aker-companion-
matching-accuracy.md`` measured (cited on each constant in ``services/companion_linking.py``); each
test here pins one of them, usually with the near miss beside the hit. The chain run through the real
association path, against Postgres, is ``tests/discovery/services/test_companion.py``.
"""

from __future__ import annotations

import time
import uuid

import pytest

from phaze.services.companion_content import never_link_reason
from phaze.services.companion_linking import (
    CLOSE_NAME_MARGIN,
    CLOSE_NAME_THRESHOLD,
    AgentMediaIndex,
    LinkDecision,
    LinkInput,
    dates_agree,
    full_dates,
    is_collection_folder,
    link_companion,
    name_similarity,
    name_tokens,
)


def _index(*paths: str) -> tuple[AgentMediaIndex, dict[str, uuid.UUID]]:
    """An agent holding media at ``paths``; returns the index and ``path -> id``."""
    ids = {path: uuid.uuid4() for path in paths}
    return AgentMediaIndex((media_id, path) for path, media_id in ids.items()), ids


def _companion(path: str, *references: str, source: str = "cue_file", **kwargs: object) -> LinkInput:
    return LinkInput(
        path=path,
        references=tuple((name, source) for name in references),
        junk_class=kwargs.get("junk_class"),  # type: ignore[arg-type]
        is_tracklist=bool(kwargs.get("is_tracklist", False)),
        identical_copy_linked=bool(kwargs.get("identical_copy_linked", False)),
    )


def _decide(index: AgentMediaIndex, ids: dict[str, uuid.UUID], companion: LinkInput) -> tuple[str, set[str]]:
    """The deciding step and the linked media PATHS, for readable assertions."""
    decision: LinkDecision = link_companion(companion, index)
    by_id = {media_id: path for path, media_id in ids.items()}
    return decision.step, {by_id[media_id] for media_id in decision.media_ids}


# --- names --------------------------------------------------------------------------------------------


def test_name_tokens_strip_scene_index_group_tail_copy_marker_and_source_tags() -> None:
    assert name_tokens("00-some_artist-live_at_venue-fm-31-03-2012-GRP") == ["some", "artist", "at", "venue", "31", "03", "2012"]
    assert name_tokens("Some Artist - Live at Venue (31-03-2012) MP3") == ["some", "artist", "at", "venue", "31", "03", "2012"]
    assert name_tokens("Show (1)") == name_tokens("Show") == ["show"]
    # A spaced name is not scene style, so its last dash-word is title, never a release group.
    assert name_tokens("Artist - Night-Owls") == ["artist", "night", "owls"]


def test_name_similarity_is_the_better_of_token_jaccard_and_sequence_ratio() -> None:
    assert name_similarity(["a", "b"], ["a", "b"]) == 1.0
    assert name_similarity([], ["a"]) == 0.0
    assert name_similarity(["artist", "one"], ["one", "artist"]) == 1.0  # Jaccard ignores order
    assert 0.0 < name_similarity(["artist", "7"], ["artist", "17"]) < 1.0


def test_full_dates_take_either_order_and_the_guard_needs_the_same_day() -> None:
    assert full_dates("Set 2012-03-31") == full_dates("Set 31.03.2012") == full_dates("Set 2012 31 03")
    assert dates_agree(full_dates("a 31-03-2012"), full_dates("b 2012.03.31"))
    assert not dates_agree(full_dates("a 31-03-2012"), full_dates("b 2012.03.30"))
    assert not dates_agree(full_dates("a 31-03-2012"), full_dates("b 31-03-2011"))
    assert not dates_agree(full_dates("a 31-03-2012"), set())  # a dated companion needs a dated media name
    assert dates_agree(set(), full_dates("b 2012.03.31"))  # an undated companion is never guarded


def test_a_collection_is_dated_files_sharing_no_date() -> None:
    episodes = ["2011 03 05 Show #279 Part 1.mp3", "2011 03 05 Show #279 Part 2.mp3", "2011 03 12 Show #280 Part 1.mp3"]
    assert is_collection_folder(episodes)
    # One release's parts share their date, or carry none.
    assert not is_collection_folder(["01-artist-live-31-03-2012.mp3", "02-other-live-31-03-2012.mp3"])
    assert not is_collection_folder(["cd1.mp3", "cd2.mp3", "cd3.mp3"])
    # One file naming two dates (broadcast and event) is one recording, not a collection.
    assert not is_collection_folder(["2019 05 18 Show 060 - Live at Club 15.12.2019.mp3"])


# --- the veto ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("junk_class", ["empty", "all_nul", "known_stamp", "site_ad"])
def test_junk_is_vetoed_before_any_step_even_with_a_resolvable_reference(junk_class: str) -> None:
    index, ids = _index("/r/set/set.mp3")
    assert _decide(index, ids, _companion("/r/set/set.cue", "set.mp3", junk_class=junk_class)) == ("junk", set())


def test_a_duplicate_orphan_is_vetoed_but_a_copy_beside_media_still_links() -> None:
    """Decision 1: the copy in a media-less folder goes to review; a copy beside its own media is a second release copy."""
    index, ids = _index("/r/a/set.mp3", "/r/b/set.mp3")
    assert _decide(index, ids, _companion("/r/leftover/set.cue", "set.mp3", identical_copy_linked=True)) == ("duplicate", set())
    assert _decide(index, ids, _companion("/r/b/set.cue", "set.mp3", identical_copy_linked=True)) == ("reference", {"/r/b/set.mp3"})


def test_never_link_reason_is_the_shared_predicate() -> None:
    assert never_link_reason("known_stamp", folder_has_media=True, identical_copy_linked=False) == "known_stamp"
    assert never_link_reason(None, folder_has_media=False, identical_copy_linked=True) == "duplicate"
    assert never_link_reason(None, folder_has_media=True, identical_copy_linked=True) is None
    assert never_link_reason(None, folder_has_media=False, identical_copy_linked=False) is None


# --- step 2: content references ----------------------------------------------------------------------


def test_a_reference_resolves_in_its_own_folder_exactly_then_case_insensitively_then_by_stem() -> None:
    index, ids = _index("/r/set/Part1.mp3", "/r/set/part2.flac", "/r/set/Part3.mp3")
    exact = _companion("/r/set/set.cue", "Part1.mp3")
    folded = _companion("/r/set/set.m3u", "PART2.FLAC", source="m3u")
    other_extension = _companion("/r/set/wav.cue", "Part3.wav")
    assert _decide(index, ids, exact) == ("reference", {"/r/set/Part1.mp3"})
    assert _decide(index, ids, folded) == ("reference", {"/r/set/part2.flac"})
    assert _decide(index, ids, other_extension) == ("reference", {"/r/set/Part3.mp3"})


def test_an_nfo_token_that_merely_ends_with_a_media_filename_resolves_in_its_own_folder() -> None:
    """Survey §4.2: ``Files.: <name>.mp3`` -- the stored token keeps the words before the filename."""
    index, ids = _index("/r/set/01-artist-live.mp3", "/r/set/02-artist-encore.mp3")
    token = _companion("/r/set/00-release.nfo", "Files.: 01-artist-live.mp3", source="text_token")
    assert _decide(index, ids, token) == ("reference", {"/r/set/01-artist-live.mp3"})
    # A CUE FILE value is a filename, never a free-text token, so the tail rule does not apply to it.
    cue = _companion("/r/set/00-release.cue", "Files.: 01-artist-live.mp3")
    assert _decide(index, ids, cue)[0] == "folder"


def test_a_reference_resolves_anywhere_on_the_agent_when_unique() -> None:
    index, ids = _index("/r/releases/Set A/set-a.mp3", "/r/other/unrelated.mp3")
    assert _decide(index, ids, _companion("/r/leftovers/Set A/set-a.cue", "set-a.mp3")) == ("reference", {"/r/releases/Set A/set-a.mp3"})


def test_a_shared_basename_resolves_only_through_a_single_same_named_release_folder() -> None:
    index, ids = _index("/r/x/Set_B-2019/01-intro.mp3", "/r/y/Other Set/01-intro.mp3", "/r/z/Third/01-intro.mp3")
    same_release = _companion("/r/copies/Set B 2019/set.m3u", "01-intro.mp3", source="m3u")
    nowhere = _companion("/r/copies/Unknown/set.m3u", "01-intro.mp3", source="m3u")
    assert _decide(index, ids, same_release) == ("reference", {"/r/x/Set_B-2019/01-intro.mp3"})
    assert _decide(index, ids, nowhere) == ("unlinked", set())


def test_a_shared_basename_in_two_same_named_release_folders_is_refused() -> None:
    """The spike's twice-filed playlist (§4.3): two candidates in folders of the same release name is still ambiguous."""
    index, ids = _index("/r/a/Set C/01.mp3", "/r/b/Set C/01.mp3")
    assert _decide(index, ids, _companion("/r/c/Set C/set.m3u", "01.mp3", source="m3u")) == ("unlinked", set())


def test_an_agent_wide_reference_must_name_a_media_file() -> None:
    index, ids = _index("/r/a/notes.txt.mp3", "/r/a/set.mp3")
    assert _decide(index, ids, _companion("/r/notes/pointer.cue", "set.wav.bin")) == ("unlinked", set())


# --- steps 3-4: own folder -----------------------------------------------------------------------------


def test_own_folder_stem_narrows_a_multi_media_folder_before_the_folder_rule() -> None:
    index, ids = _index("/r/mixes/Mix 01.mp3", "/r/mixes/Mix 02.mp3", "/r/mixes/Mix 03.mp3")
    assert _decide(index, ids, _companion("/r/mixes/01-mix_02.txt", is_tracklist=True)) == ("stem", {"/r/mixes/Mix 02.mp3"})
    assert _decide(index, ids, _companion("/r/mixes/release.nfo"))[1] == {"/r/mixes/Mix 01.mp3", "/r/mixes/Mix 02.mp3", "/r/mixes/Mix 03.mp3"}


def test_a_reference_wins_over_the_own_folder_rules() -> None:
    index, ids = _index("/r/set/Mix 01.mp3", "/r/set/Mix 02.mp3")
    assert _decide(index, ids, _companion("/r/set/Mix 01.cue", "Mix 02.mp3")) == ("reference", {"/r/set/Mix 02.mp3"})


def test_a_collection_tracklist_needs_a_reference_or_a_stem_match() -> None:
    """Operator decision 2 (2026-10-07, "Require a reference (Recommended)"; epic phaze-4x319)."""
    folder = "/r/Show 2011 03"
    index, ids = _index(
        f"{folder}/2011 03 05 Show #279 Part 1.mp3", f"{folder}/2011 03 05 Show #279 Part 2.mp3", f"{folder}/2011 03 12 Show #280 Part 1.mp3"
    )
    assert _decide(index, ids, _companion(f"{folder}/Show #279.txt", is_tracklist=True)) == ("unlinked", set())
    # The same release note, not a tracklist, still links by the folder rule: the decision covers tracklists.
    assert _decide(index, ids, _companion(f"{folder}/Show #279.nfo"))[0] == "folder"
    # And a tracklist beside one release's parts (no collection) links to every part.
    parts, part_ids = _index("/r/Live 2012-03-31/cd1.mp3", "/r/Live 2012-03-31/cd2.mp3")
    assert _decide(parts, part_ids, _companion("/r/Live 2012-03-31/tracklist.txt", is_tracklist=True))[0] == "folder"


# --- step 5: release-folder twin -------------------------------------------------------------------------


def test_a_twin_needs_exactly_one_same_named_media_folder_and_a_long_enough_name() -> None:
    index, ids = _index("/r/a/Artist-Live_At_Venue-2019/01.mp3", "/r/a/Artist-Live_At_Venue-2019/02.mp3", "/r/a/Short/01.mp3")
    assert _decide(index, ids, _companion("/r/b/Artist Live At Venue 2019/notes.nfo")) == (
        "twin",
        {"/r/a/Artist-Live_At_Venue-2019/01.mp3", "/r/a/Artist-Live_At_Venue-2019/02.mp3"},
    )
    assert _decide(index, ids, _companion("/r/b/Short/notes.nfo"))[0] != "twin"  # 5 alphanumerics < 12
    two, two_ids = _index("/r/a/Artist-Live_At_Venue-2019/01.mp3", "/r/c/Artist Live At Venue 2019/01.mp3")
    # Two twins: no twin link. (Close name may still pick one, by its own threshold and margin.)
    assert _decide(two, two_ids, _companion("/r/b/ARTIST_LIVE_AT_VENUE_2019/notes.nfo"))[0] != "twin"


def test_a_reference_wins_over_the_twin() -> None:
    index, ids = _index("/r/a/Artist-Live_At_Venue-2019/01.mp3", "/r/elsewhere/named.mp3")
    assert _decide(index, ids, _companion("/r/b/Artist-Live_At_Venue-2019/x.cue", "named.mp3")) == ("reference", {"/r/elsewhere/named.mp3"})


# --- step 6: whole-agent close name -----------------------------------------------------------------------


def test_close_name_links_a_tag_stripped_match_anywhere_on_the_agent() -> None:
    index, ids = _index("/r/dump/Some Artist - Live at Venue (31-03-2012).mp3", "/r/dump/Other Artist - Studio Mix.mp3")
    companion = _companion("/r/info/Some_Artist-Live_at_Venue-FM-31-03-2012-GRP/00-release.nfo")
    assert _decide(index, ids, companion) == ("close_name", {"/r/dump/Some Artist - Live at Venue (31-03-2012).mp3"})


def test_close_name_below_the_threshold_links_nothing() -> None:
    assert CLOSE_NAME_THRESHOLD == 0.9
    index, ids = _index("/r/dump/Some Artist - Night at the Venue.mp3")
    assert _decide(index, ids, _companion("/r/info/Some Artist - Morning in a Garden/notes.nfo")) == ("unlinked", set())


def test_close_name_date_guard_refuses_another_days_set() -> None:
    index, ids = _index("/r/dump/Some Artist - Live at Venue (30-03-2012).mp3")
    assert _decide(index, ids, _companion("/r/info/Some Artist - Live at Venue (31-03-2012)/notes.nfo")) == ("unlinked", set())


def test_close_name_refuses_a_rival_in_another_folder_within_the_margin_but_not_one_in_the_same_folder() -> None:
    """Spike §2 (dw): rivals in the best candidate's own folder do not count (one release, several parts)."""
    assert CLOSE_NAME_MARGIN == 0.05
    rivals, rival_ids = _index("/r/a/Some Artist - Live at Venue.mp3", "/r/b/Some Artist - Live at Venue.mp3")
    assert _decide(rivals, rival_ids, _companion("/r/info/Some Artist - Live at Venue/notes.nfo")) == ("unlinked", set())
    parts, part_ids = _index("/r/a/Some Artist - Live at Venue.mp3", "/r/a/Some Artist - Live at Venue.flac")
    step, linked = _decide(parts, part_ids, _companion("/r/info/Some Artist - Live at Venue/notes.nfo"))
    assert step == "close_name"
    assert len(linked) == 1


def test_close_name_ignores_tokens_held_by_two_thousand_or_more_media_names() -> None:
    """A token everyone holds selects no candidates, so a companion sharing only it links nothing."""
    paths = [f"/r/dump/common {index}.mp3" for index in range(2000)]
    index, ids = _index(*paths)
    assert _decide(index, ids, _companion("/r/info/common/notes.nfo")) == ("unlinked", set())


def test_close_name_is_deterministic_on_ties() -> None:
    index, _ids = _index("/r/b/Some Artist - Live.mp3", "/r/a/Some Artist - Live.mp3")
    first = link_companion(_companion("/r/b/info/Some Artist - Live/notes.nfo"), index)
    assert all(link_companion(_companion("/r/b/info/Some Artist - Live/notes.nfo"), index) == first for _ in range(5))


def test_an_adversarial_name_is_scored_in_bounded_time() -> None:
    """Names are cut to 255 characters before any pattern or SequenceMatcher sees them."""
    long_name = "a-" * 100_000
    index, _ids = _index(f"/r/dump/{long_name}.mp3", f"/r/dump/{'b ' * 50_000}.mp3")
    started = time.monotonic()
    link_companion(_companion(f"/r/info/{long_name}/{'c-' * 100_000}.nfo"), index)
    assert time.monotonic() - started < 2.0


def test_the_index_refuses_rows_after_its_close_name_index_is_built() -> None:
    index, _ids = _index("/r/a/one.mp3")
    index.close_names()
    with pytest.raises(RuntimeError, match="already built"):
        index.add(uuid.uuid4(), "/r/a/two.mp3")


def test_no_step_consults_the_parent_folder() -> None:
    """phaze-ehryj's parent-folder name fallback is retired (operator decision 8, 2026-10-07; epic phaze-4x319).

    The parent holds media named exactly like the companion's folder and own stem, and close name cannot
    reach it (the margin rival sits in another folder): the companion links nothing rather than pair by proximity.
    """
    index, ids = _index("/r/set/Live Set.mp3", "/r/other/Live Set.mp3")
    assert _decide(index, ids, _companion("/r/set/Live Set/Live Set.nfo")) == ("unlinked", set())


def test_names_with_no_letters_or_digits_are_never_a_stem_match() -> None:
    """Two stems that both key to "" are not a stem match (phaze-ehryj's rule, kept for step 3)."""
    index, ids = _index("/r/set/__.mp3", "/r/set/other.mp3")
    assert _decide(index, ids, _companion("/r/set/--.nfo"))[0] == "folder"
