"""Build a nested folder structure under a Jama item from an indented text outline.

Outline format (2 spaces per level).  An optional ``| TYPEKEY`` suffix sets the
item type the folder holds; subfolders inherit it.  Blank lines and lines
starting with ``#`` are ignored::

    Flight Deck | REQ
      Controls
        Sidestick
        Throttle Quadrant
      Displays
    Monuments | DES
      Glareshield
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from jama_sync.client import JamaSyncClient

logger = logging.getLogger(__name__)

INDENT = 2
FOLDER_TYPE_KEY = "FLD"


class OutlineError(ValueError):
    """Raised when the outline file is malformed."""


@dataclass
class FolderNode:
    """One folder in the outline."""

    name: str
    type_key: str  # item type key of the items this folder holds
    children: list[FolderNode] = field(default_factory=list)


@dataclass
class BuildResult:
    created: int = 0
    existing: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# Outline parsing
# ---------------------------------------------------------------------------

def parse_outline(path: str, default_type: str) -> list[FolderNode]:
    """Parse an indented outline file into a list of root folder nodes."""
    roots: list[FolderNode] = []
    stack: list[tuple[int, FolderNode]] = []  # (level, node)
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n").replace("\t", " " * INDENT)
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            spaces = len(line) - len(line.lstrip(" "))
            if spaces % INDENT:
                raise OutlineError(f"Line {lineno}: indent must be a multiple of {INDENT} spaces")
            level = spaces // INDENT
            name, _, key = line.strip().partition("|")
            name, key = name.strip(), key.strip().upper()
            if not name:
                raise OutlineError(f"Line {lineno}: folder name is empty")

            while stack and stack[-1][0] >= level:
                stack.pop()
            if level > (stack[-1][0] + 1 if stack else 0):
                raise OutlineError(f"Line {lineno}: indented more than one level past its parent")

            siblings = stack[-1][1].children if stack else roots
            if any(s.name.casefold() == name.casefold() for s in siblings):
                raise OutlineError(f"Line {lineno}: duplicate folder name '{name}' under the same parent")

            inherited = stack[-1][1].type_key if stack else default_type
            node = FolderNode(name, key or inherited)
            siblings.append(node)
            stack.append((level, node))
    return roots


def iter_nodes(nodes: list[FolderNode]):
    """Yield every node in the tree, depth-first."""
    for n in nodes:
        yield n
        yield from iter_nodes(n.children)


def format_tree(nodes: list[FolderNode], depth: int = 0) -> list[str]:
    """Render the outline as indented lines for display."""
    lines: list[str] = []
    for n in nodes:
        lines.append(f"{'  ' * depth}- {n.name}  [{n.type_key}]")
        lines.extend(format_tree(n.children, depth + 1))
    return lines


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def build_folders(
    client: JamaSyncClient,
    nodes: list[FolderNode],
    parent_id: int | None,
    project_id: int,
    folder_type_id: int,
    type_ids: dict[str, int],
    dry_run: bool,
    depth: int = 0,
    result: BuildResult | None = None,
) -> BuildResult:
    """Create *nodes* (recursively) under *parent_id*.

    Folders that already exist under the same parent with the same name
    (case-insensitive) are reused, so re-running is safe.  *parent_id* is
    ``None`` in dry-run mode when the parent itself would be newly created.
    """
    result = result or BuildResult()
    pad = "  " * (depth + 1)

    existing: dict[str, int] = {}
    if parent_id is not None:
        existing = {
            c["fields"]["name"].strip().casefold(): c["id"]
            for c in client.get_children(parent_id)
            if c.get("itemType") == folder_type_id
        }

    for n in nodes:
        folder_id = existing.get(n.name.casefold())
        if folder_id is not None:
            result.existing += 1
            logger.info("%s= %s (exists, id=%d)", pad, n.name, folder_id)
        elif dry_run:
            result.created += 1
            logger.info("%s[DRY RUN] + %s [%s]", pad, n.name, n.type_key)
        else:
            try:
                folder_id = client.create_item(
                    project_id=project_id,
                    item_type_id=folder_type_id,
                    child_item_type_id=type_ids[n.type_key],
                    parent_id=parent_id,
                    fields={"name": n.name},
                )
            except Exception as exc:
                result.failed += 1
                logger.error("%sFAIL + %s: %s  (skipping its subfolders)", pad, n.name, exc)
                continue
            result.created += 1
            logger.info("%s+ %s [%s] (id=%d)", pad, n.name, n.type_key, folder_id)

        build_folders(client, n.children, folder_id, project_id, folder_type_id,
                      type_ids, dry_run, depth + 1, result)
    return result
