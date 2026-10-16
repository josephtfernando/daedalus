"""Where a seat's facts live (SPEC §7).

Memory is **namespaced**: what one identity knows is not what another knows. The
layout is deliberately boring, because the file is the truth and the owner has to
be able to read it:

```
$DAEDALUS_HOME/memory/facets/*.md                     ← the default namespace
$DAEDALUS_HOME/memory/namespaces/<name>/facets/*.md   ← every other one
```

The default namespace is the *root*: it is where every fact written before seats
had namespaces already is. That is the whole reason for the asymmetry — a new
arrangement must not orphan the existing memory, or the first thing it loses is
what the owner told it last month.

Two consequences worth stating, because both are choices:

- **The directory is the namespace.** No entry carries an `ns=` marker; the file
  it lives in decides. So moving a note between namespaces is `mv`, and it is a
  real move — which is the right kind of power for a file-first store, and the
  reason the name is validated rather than pasted into a path.
- **A namespace name is a path component.** It is checked (ASCII, no separators,
  no `..`) instead of trusted, because it arrives from a charter file the owner
  edits by hand and ends up as a directory name.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The namespace that existed before seats did: the coordinator's, and the root
#: of the tree. Named for the default seat, and deliberately *not* derived from
#: it — this name is data's address, not a seat's nickname.
DEFAULT_NAMESPACE = "daedalus"

#: A namespace name has to survive a round trip through a directory name. Lower
#: case, digits, `-` and `_`; it must start with a letter or digit, so `.`,
#: `..`, and a leading dash are all out.
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class NamespaceError(ValueError):
    """A namespace that could not be a directory name, named by where it came from."""


def check(name: str, *, where: str = "") -> str:
    """Return ``name`` if it is a usable namespace, refuse it otherwise.

    A namespace ends up as a path component, so this is the one place that has to
    be boring and strict: ``../private`` is not a namespace, it is a way out of
    the memory tree.
    """
    text = str(name).strip()
    if not NAME.match(text):
        origin = f"{where}: " if where else ""
        raise NamespaceError(
            f"{origin}{text!r} is not a namespace name — use lower-case letters, "
            "digits, `-` and `_` (it becomes a directory under memory/namespaces/)"
        )
    return text


def facets_dir(root: Path, namespace: str = DEFAULT_NAMESPACE) -> Path:
    """The directory holding one namespace's notes.

    The default namespace is the root's own ``facets/``; every other one is a
    child of ``namespaces/``. Nothing else in the tree changes, which is what
    makes an existing install an install with one namespace rather than a
    migration.
    """
    name = check(namespace)
    if name == DEFAULT_NAMESPACE:
        return Path(root) / "facets"
    return Path(root) / "namespaces" / name / "facets"


def facets_dirs(root: Path) -> list[tuple[str, Path]]:
    """Every namespace with a directory on disk, default first.

    Read from the filesystem rather than from a list of known seats: a directory
    the owner made by hand is a namespace the store should not drop on the floor
    when it rebuilds the index.
    """
    out: list[tuple[str, Path]] = [(DEFAULT_NAMESPACE, facets_dir(root))]
    base = Path(root) / "namespaces"
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "facets").is_dir():
                out.append((child.name, child / "facets"))
    return out
