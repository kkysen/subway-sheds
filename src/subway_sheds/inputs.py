"""What a step's output is made from, to make it again only when that changes."""

import ast
import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

from subway_sheds import paths

PACKAGE = Path(__file__).parent
# The `route` crate, which `_route` is built from.
ROUTE = paths.ROOT / "route"


def modules(name: str) -> set[str]:
    """`name` and every module of this package it imports, however indirectly."""
    seen: set[str] = set()
    todo = [name]
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        source = PACKAGE / f"{module}.py"
        # `_route` has no source here, but in `ROUTE`.
        if not source.exists():
            continue
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "subway_sheds":
                todo += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                todo += _ours(node.module)
            elif isinstance(node, ast.Import):
                todo += [m for alias in node.names for m in _ours(alias.name)]
    return seen


def _ours(module: str) -> list[str]:
    package, _, rest = module.partition(".")
    return [rest.split(".")[0]] if package == "subway_sheds" and rest else []


def files(name: str, also: Iterable[Path] = ()) -> list[Path]:
    """
    What module `name` makes its output from:
    its and its imports' sources, the `route` crate's if they import `_route`,
    the files they read as `paths` names them, those `also`,
    and `uv.lock`, for the libraries they use.
    """
    names = modules(name)
    sources = [PACKAGE / f"{m}.py" for m in names if (PACKAGE / f"{m}.py").exists()]
    read = {
        getattr(paths, attr)
        for source in sources
        for attr in re.findall(r"\bpaths\.([A-Z][A-Z0-9_]*)\b", source.read_text())
    }
    out = set(sources) | {p for p in read | set(also) if p.is_file()}
    if "_route" in names:
        out |= {ROUTE / "Cargo.toml", ROUTE / "Cargo.lock", ROUTE / "build.rs"}
        out |= {p for p in (ROUTE / "src").rglob("*") if p.is_file()}
    out |= {paths.ROOT / "uv.lock", paths.ROOT / ".python-version"}
    return sorted(out)


def digest(name: str, also: Iterable[Path] = ()) -> str:
    """A hash of `files`, names and contents, which changes whenever any of them does."""
    out = hashlib.sha256()
    for path in files(name, also):
        out.update(str(path.relative_to(paths.ROOT)).encode() + b"\0")
        out.update(hashlib.sha256(path.read_bytes()).digest())
    return out.hexdigest()
