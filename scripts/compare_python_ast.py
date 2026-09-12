"""Prove that Python comment/docstring edits preserve the executable AST."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import subprocess  # nosec B404 -- fixed git argv, no shell, repository-local read operations only
import sys
import tokenize
from typing import TYPE_CHECKING, cast


if TYPE_CHECKING:
    from collections.abc import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]


class _ExecutableAst(ast.NodeTransformer):
    """Remove docstrings and source positions while retaining typing directives."""

    @staticmethod
    def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            return body[1:]
        return body

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        for type_ignore in node.type_ignores:
            type_ignore.lineno = 0
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node


def executable_ast(source: str, *, filename: str = "<unknown>") -> str:
    """Return a position-independent dump of executable and typing syntax."""
    tree = ast.parse(source, filename=filename, type_comments=True, feature_version=sys.version_info[:2])
    normalized = _ExecutableAst().visit(tree)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, annotate_fields=True, include_attributes=False)


def _decode(source: bytes, *, filename: str) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(iter(source.splitlines(keepends=True)).__next__)
        return source.decode(encoding)
    except (LookupError, SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot decode {filename}: {exc}") from exc


def _git(*args: str, text: bool = True, check: bool = True) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603  # nosec B603 B607 -- fixed git executable, no shell
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=text,
        check=check,
    )


def _read_version(path: str, revision: str | None) -> str:
    if revision is None:
        source = (REPO_ROOT / path).read_bytes()
    else:
        completed = _git("show", f"{revision}:{path}", text=False)
        source = cast("bytes", completed.stdout)
    return _decode(source, filename=f"{revision or 'WORKTREE'}:{path}")


def changed_python_paths(base: str, target: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return comparable modified Python paths and non-modification path changes."""
    args = ["diff", "--name-status", "--find-renames", base]
    if target is not None:
        args.append(target)
    args.extend(("--", "*.py"))
    completed = _git(*args)
    output = completed.stdout
    if not isinstance(output, str):
        raise TypeError("git diff returned bytes unexpectedly")
    comparable: list[str] = []
    structural: list[str] = []
    for line in output.splitlines():
        status, *names = line.split("\t")
        if status == "M" and len(names) == 1:
            comparable.append(names[0])
        else:
            structural.append(line)
    return tuple(sorted(comparable)), tuple(sorted(structural))


def compare_paths(paths: Sequence[str], *, base: str, target: str | None = None) -> list[str]:
    """Return paths whose executable AST differs between the two versions."""
    different: list[str] = []
    for path in paths:
        before = executable_ast(_read_version(path, base), filename=f"{base}:{path}")
        after = executable_ast(_read_version(path, target), filename=f"{target or 'WORKTREE'}:{path}")
        if before != after:
            different.append(path)
    return different


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="revision containing the pre-edit files")
    parser.add_argument("--target", help="post-edit revision (default: index and working tree)")
    parser.add_argument("paths", nargs="*", help="Python paths; default: modified .py files from git diff")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    paths = tuple(args.paths)
    structural: tuple[str, ...] = ()
    if not paths:
        paths, structural = changed_python_paths(args.base, args.target)
    if structural:
        print("Not comment-only: Python files were added, deleted, copied, or renamed:", file=sys.stderr)  # noqa: T201
        for change in structural:
            print(f"  {change}", file=sys.stderr)  # noqa: T201
    different = compare_paths(paths, base=args.base, target=args.target)
    if different:
        print("Executable Python AST changed:", file=sys.stderr)  # noqa: T201
        for path in different:
            print(f"  {path}", file=sys.stderr)  # noqa: T201
    if structural or different:
        return 1
    print(f"Executable Python AST unchanged for {len(paths)} file(s).")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
