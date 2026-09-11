from scripts.compare_python_ast import executable_ast


def test_comments_and_docstrings_do_not_change_executable_ast() -> None:
    before = '''"""Old module text."""
# old rationale
def value(item: int) -> int:
    """Old function text."""
    return item + 1
'''
    after = '''"""New module text."""
# new rationale over two lines
# with an updated citation
def value(item: int) -> int:
    """New function text."""
    return item + 1
'''
    assert executable_ast(before) == executable_ast(after)


def test_executable_change_changes_ast() -> None:
    assert executable_ast("value = 1\n") != executable_ast("value = 2\n")


def test_non_docstring_string_expression_remains_executable() -> None:
    before = 'def value() -> None:\n    pass\n    "first"\n'
    after = 'def value() -> None:\n    pass\n    "second"\n'
    assert executable_ast(before) != executable_ast(after)


def test_type_comment_change_changes_ast() -> None:
    assert executable_ast("value = []  # type: list[str]\n") != executable_ast("value = []  # type: list[int]\n")


def test_type_ignore_line_may_move_but_tag_may_not_change() -> None:
    before = "value = call()  # type: ignore[arg-type]\n"
    moved = "# rationale\nvalue = call()  # type: ignore[arg-type]\n"
    changed = "value = call()  # type: ignore[assignment]\n"
    assert executable_ast(before) == executable_ast(moved)
    assert executable_ast(before) != executable_ast(changed)
