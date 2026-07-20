"""Tests for THE sortable-column contract (phaze-a6hm.1, src/phaze/routers/column_sort.py).

Contract rule 7 makes every claim in that module's docstring a test obligation, so this file is
organised rule by rule. The load-bearing test is :class:`TestUnwhitelistedSortCannotReachAColumn`:
it asserts the STRUCTURAL property of rule 2 -- an unrecognised ``sort`` value never becomes a
column -- rather than merely asserting a status code, which would pass against an implementation
that happily ``getattr``-ed its way to whatever the request named.
"""

import pytest
from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.routers.column_sort import ASCENDING, DESCENDING, SortableColumn, SortContract


FILES_SORT = SortContract(
    endpoint="/pipeline/pending-files",
    target="#metadata-files-view",
    columns=(
        SortableColumn(key="filename", label="File", expression=FileRecord.original_filename),
        SortableColumn(key="file_type", label="Format", expression=FileRecord.file_type),
        SortableColumn(key="file_size", label="Size", expression=FileRecord.file_size),
    ),
    default_key="filename",
)


def _compiled_order_by(sort_value: str | None) -> str:
    """Compile a real SELECT ordered by whatever ``sort_value`` resolves to; return its ORDER BY clause.

    Deliberately narrowed to the ORDER BY rather than the whole statement. ``SELECT *`` on a mapped
    entity emits EVERY column name -- including unwhitelisted ones like ``original_path`` -- so a
    substring check over the full SQL would fail on the projection and tell us nothing about the
    clause the sort value actually controls.
    """
    state = FILES_SORT.resolve(sort=sort_value)
    stmt = select(FileRecord).order_by(*state.order_by())
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    return sql.split("ORDER BY", 1)[1]


class TestUnwhitelistedSortCannotReachAColumn:
    """Contract rule 2: the whitelist maps to COLUMN OBJECTS, so an unknown key has nothing to reach.

    This is the regression the bead requires. Each case is a value that a naive implementation would
    have happily turned into a column, an attribute, or SQL text.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "password",  # a column on some other table
            "id",  # a real column that is deliberately NOT offered
            "original_path",  # a real FileRecord column that is NOT whitelisted
            "__class__",  # would resolve under getattr()
            "metadata",  # would resolve under getattr() on a declarative model
            "original_filename",  # the ATTRIBUTE name behind key="filename" -- still not a key
            "file_size; DROP TABLE files",  # would be catastrophic under text() interpolation
            "1) OR 1=1 --",
            "",
        ],
    )
    def test_unwhitelisted_value_resolves_to_the_default_key(self, hostile: str) -> None:
        """An unrecognised sort value is DISCARDED at resolve() and replaced by the default."""
        assert FILES_SORT.resolve(sort=hostile).key == "filename"

    @pytest.mark.parametrize("hostile", ["original_path", "__class__", "file_size; DROP TABLE files", "1) OR 1=1 --"])
    def test_unwhitelisted_value_never_appears_in_the_emitted_sql(self, hostile: str) -> None:
        """The stronger assertion: the hostile string is absent from the compiled ORDER BY entirely.

        A status-code assertion would pass even if the value reached a column. This checks the actual
        SQL, which is the only place the injection would show up.
        """
        sql = _compiled_order_by(hostile)
        assert hostile not in sql
        assert "files.original_filename ASC" in sql

    def test_every_whitelisted_key_reaches_its_own_column(self) -> None:
        """The complement: the guard rejects the unknown WITHOUT breaking the known."""
        assert "files.file_size ASC" in _compiled_order_by("file_size")
        assert "files.file_type ASC" in _compiled_order_by("file_type")

    def test_resolution_is_equality_not_prefix_or_substring_matching(self) -> None:
        """``file`` is a prefix of two real keys and must still be rejected (rule 2: equality only)."""
        assert FILES_SORT.resolve(sort="file").key == "filename"
        assert FILES_SORT.resolve(sort="FILE_SIZE").key == "filename"


class TestUnknownValueDegradesRatherThanRaising:
    """Contract rule 3: a render-path allowlist degrades to the default; it does NOT 422."""

    def test_unknown_sort_does_not_raise(self) -> None:
        assert FILES_SORT.resolve(sort="nonsense", order="sideways").key == "filename"

    def test_unknown_order_falls_back_to_the_contract_default(self) -> None:
        assert FILES_SORT.resolve(sort="file_size", order="sideways").order == ASCENDING
        assert FILES_SORT.resolve(sort="file_size", order="DESC").order == ASCENDING

    def test_absent_params_yield_the_defaults(self) -> None:
        state = FILES_SORT.resolve()
        assert (state.key, state.order) == ("filename", ASCENDING)


class TestOrderByIsDisplayOrderOnly:
    """Contract rule 1 / paging contract rule 4: order_by() is the display order, never the tiebreaker."""

    def test_direction_is_applied(self) -> None:
        assert "files.file_size DESC" in str(
            select(FileRecord).order_by(*FILES_SORT.resolve(sort="file_size", order=DESCENDING).order_by()).compile()
        )

    def test_order_by_is_a_single_element_clause(self) -> None:
        assert len(FILES_SORT.resolve(sort="file_size").order_by()) == 1


class TestViewStatePreservation:
    """Contract rule 4: a header click changes the order and NOTHING else."""

    def test_url_carries_every_preserved_parameter(self) -> None:
        state = FILES_SORT.resolve(sort="filename", view_state={"stage": "metadata", "page_size": 50, "q": "live set"})
        url = state.url_for("Format")
        assert url.startswith("/pipeline/pending-files?")
        assert "stage=metadata" in url
        assert "page_size=50" in url
        assert "q=live+set" in url
        assert "sort=file_type" in url

    def test_none_valued_view_state_is_dropped_not_stringified(self) -> None:
        """An absent filter must not become the literal string ``None`` in the URL."""
        url = FILES_SORT.resolve(sort="filename", view_state={"status": None, "page_size": 50}).url_for("Format")
        assert "status=" not in url
        assert "None" not in url

    def test_sorting_resets_to_page_one(self) -> None:
        """A re-sort must not hold an offset that means nothing under the new order."""
        assert "page=" not in FILES_SORT.resolve(sort="filename", view_state={"page_size": 50}).url_for("Format")

    def test_query_state_lets_a_pager_carry_the_sort_forward(self) -> None:
        assert FILES_SORT.resolve(sort="file_size", order=DESCENDING).query_state() == "&sort=file_size&order=desc"


class TestToggleSemantics:
    """Contract rule 4: clicking the active column toggles; clicking another starts ascending."""

    def test_active_ascending_column_toggles_to_descending(self) -> None:
        assert FILES_SORT.resolve(sort="filename", order=ASCENDING).next_order("File") == DESCENDING

    def test_active_descending_column_toggles_back_to_ascending(self) -> None:
        assert FILES_SORT.resolve(sort="filename", order=DESCENDING).next_order("File") == ASCENDING

    def test_inactive_column_starts_ascending_rather_than_inheriting_the_direction(self) -> None:
        """Inheriting reads as the table re-sorting itself in a direction nobody chose."""
        assert FILES_SORT.resolve(sort="filename", order=DESCENDING).next_order("Size") == ASCENDING


class TestAriaSort:
    """Contract rule 5: the header announces its own state to assistive technology."""

    def test_active_column_reports_its_direction(self) -> None:
        state = FILES_SORT.resolve(sort="file_size", order=DESCENDING)
        assert state.aria_sort("Size") == "descending"
        assert FILES_SORT.resolve(sort="file_size", order=ASCENDING).aria_sort("Size") == "ascending"

    def test_inactive_sortable_column_reports_none_not_a_missing_attribute(self) -> None:
        """``none`` means "sortable, not currently sorted" -- omitting it would mean "not sortable"."""
        assert FILES_SORT.resolve(sort="file_size").aria_sort("File") == "none"

    def test_caret_indicator_is_present_only_on_the_active_column(self) -> None:
        state = FILES_SORT.resolve(sort="file_size", order=DESCENDING)
        assert state.indicator("Size") == "▼"
        assert state.indicator("File") == ""
        assert FILES_SORT.resolve(sort="file_size", order=ASCENDING).indicator("Size") == "▲"


class TestLabelRecognition:
    """The mechanism that lets ONE partial serve nine workspaces with no per-workspace branching."""

    def test_whitelisted_labels_are_sortable(self) -> None:
        state = FILES_SORT.resolve()
        assert state.is_sortable("File")
        assert state.is_sortable("Size")

    def test_unlisted_labels_are_not_sortable(self) -> None:
        """A column this table does not offer renders as a plain header, not a broken button."""
        state = FILES_SORT.resolve()
        assert not state.is_sortable("Existing tags")
        assert not state.is_sortable("Duration")

    def test_url_for_an_unsortable_label_is_inert_rather_than_an_exception(self) -> None:
        assert FILES_SORT.resolve().url_for("Existing tags") == "/pipeline/pending-files"


class TestMisWiredContractFailsAtImportTime:
    """Contract rule 6: every one of these is a one-character typo with an invisible runtime symptom."""

    def test_empty_column_set_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one SortableColumn"):
            SortContract(endpoint="/x", target="#x", columns=(), default_key="a")

    def test_duplicate_keys_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate column keys"):
            SortContract(
                endpoint="/x",
                target="#x",
                columns=(
                    SortableColumn(key="a", label="A", expression=FileRecord.id),
                    SortableColumn(key="a", label="B", expression=FileRecord.file_size),
                ),
                default_key="a",
            )

    def test_duplicate_labels_are_rejected(self) -> None:
        """Two columns sharing a label make the shared partial pick an arbitrary one of them."""
        with pytest.raises(ValueError, match="duplicate column labels"):
            SortContract(
                endpoint="/x",
                target="#x",
                columns=(
                    SortableColumn(key="a", label="Same", expression=FileRecord.id),
                    SortableColumn(key="b", label="Same", expression=FileRecord.file_size),
                ),
                default_key="a",
            )

    def test_default_key_outside_the_whitelist_is_rejected(self) -> None:
        """Otherwise order_by() raises StopIteration on the very first render."""
        with pytest.raises(ValueError, match="is not one of its columns"):
            SortContract(endpoint="/x", target="#x", columns=(SortableColumn(key="a", label="A", expression=FileRecord.id),), default_key="nope")

    def test_invalid_default_order_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be 'asc' or 'desc'"):
            SortContract(
                endpoint="/x",
                target="#x",
                columns=(SortableColumn(key="a", label="A", expression=FileRecord.id),),
                default_key="a",
                default_order="ascending",
            )
