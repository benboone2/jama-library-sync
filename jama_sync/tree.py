"""Build item hierarchy trees from Jama and diff them to produce sync plans."""

from __future__ import annotations

import logging
from typing import Any

from .client import JamaSyncClient
from .models import ActionType, SyncAction, SyncPlan, TreeNode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------

def _node_from_item(item: dict[str, Any]) -> TreeNode:
    """Convert a raw Jama item dict into a TreeNode (no children yet)."""
    fields = item.get("fields", {})
    return TreeNode(
        item_id=item["id"],
        global_id=item.get("globalId", item.get("documentKey", "")),
        name=fields.get("name", fields.get("documentKey", f"item-{item['id']}")),
        item_type_id=item.get("itemType", 0),
        child_item_type_id=item.get("childItemType", None),
        fields=fields,
        children=[],
    )


def fetch_tree(client: JamaSyncClient, root_id: int, max_depth: int = 10) -> TreeNode:
    """Recursively fetch an item and its descendants from Jama.

    Parameters
    ----------
    client:
        Authenticated Jama API client.
    root_id:
        Item ID of the root node (typically a component).
    max_depth:
        Maximum recursion depth.  Default 10 supports deeply nested
        structures (component -> folder -> sub-folder -> ... -> leaf items).

    Returns
    -------
    TreeNode
        Root node with all descendants populated.
    """
    root_item = client.get_item(root_id)
    root = _node_from_item(root_item)
    logger.info("Fetching tree rooted at '%s' (id=%d) ...", root.name, root.item_id)

    _populate_children(client, root, depth=1, max_depth=max_depth)

    total = _count_nodes(root) - 1  # exclude root
    folders = _count_folders(root)
    logger.info(
        "  -> '%s': %d descendant(s), %d container(s)",
        root.name,
        total,
        folders,
    )
    return root


def _populate_children(
    client: JamaSyncClient,
    node: TreeNode,
    depth: int,
    max_depth: int,
) -> None:
    """Recursively fetch and attach children to *node*."""
    if depth > max_depth:
        return

    try:
        children_raw = client.get_children(node.item_id)
    except Exception as exc:
        logger.warning(
            "Could not fetch children of item %d ('%s'): %s",
            node.item_id,
            node.name,
            exc,
        )
        return

    for child_data in children_raw:
        child = _node_from_item(child_data)
        node.children.append(child)
        # Recurse into child (folders/sub-folders may contain more items)
        _populate_children(client, child, depth + 1, max_depth)


def _count_nodes(node: TreeNode) -> int:
    """Count total nodes in a tree (including the root)."""
    return 1 + sum(_count_nodes(c) for c in node.children)


def _count_folders(node: TreeNode) -> int:
    """Count nodes that have children (containers / folders)."""
    count = 1 if node.children else 0
    return count + sum(_count_folders(c) for c in node.children)


# ---------------------------------------------------------------------------
# Diffing -- fully recursive
# ---------------------------------------------------------------------------

def diff_trees(
    source: TreeNode,
    dest: TreeNode,
    dest_project_id: int,
    dest_name: str,
    exclude_ids: set[int] | None = None,
) -> SyncPlan:
    """Compare source and destination trees to produce a sync plan.

    Works at **arbitrary depth**.  At every level:

    - Nodes *with* children are treated as **containers** (folders/sets)
      and matched by name (case-insensitive).
    - Nodes *without* children are treated as **leaf items** and matched
      by Global ID.
    - Missing containers -> CREATE_FOLDER  (+ recurse into their subtree).
    - Missing leaf items -> CREATE_REUSE.

    Parameters
    ----------
    source:
        Source component tree (Library of Common Reference Material).
    dest:
        Destination component tree.
    dest_project_id:
        Jama project ID for the destination.
    dest_name:
        Human-readable name for logging.
    exclude_ids:
        Optional set of source item IDs to skip.  If an excluded ID is a
        folder/container, its entire subtree is also skipped.

    Returns
    -------
    SyncPlan
        Ordered list of actions -- parent folders always appear before
        their children so the executor can resolve IDs in order.
    """
    plan = SyncPlan(dest_name=dest_name, dest_project_id=dest_project_id)
    _exclude = exclude_ids or set()

    if _exclude:
        logger.info("  Excluding %d source item(s) by ID", len(_exclude))

    _diff_recursive(
        source_parent=source,
        dest_parent=dest,
        dest_parent_id=dest.item_id,
        dest_project_id=dest_project_id,
        dest_name=dest_name,
        plan=plan,
        indent=1,
        exclude_ids=_exclude,
    )

    logger.info("  %s", plan.summary())
    return plan


def _is_container(node: TreeNode) -> bool:
    """Return True if *node* is a folder/set (has children in the source)."""
    return len(node.children) > 0


def _diff_recursive(
    source_parent: TreeNode,
    dest_parent: TreeNode | None,
    dest_parent_id: int,
    dest_project_id: int,
    dest_name: str,
    plan: SyncPlan,
    indent: int,
    exclude_ids: set[int] | None = None,
) -> None:
    """Recursively diff *source_parent*'s children against *dest_parent*.

    Actions are appended to *plan* in depth-first order so that parent
    CREATE_FOLDER actions always precede their children.
    """
    prefix = "  " * indent
    _exclude = exclude_ids or set()

    for src_child in source_parent.children:
        # Skip excluded items (and their entire subtree if a container)
        if src_child.item_id in _exclude:
            subtree_size = _count_nodes(src_child) - 1
            if subtree_size > 0:
                logger.info(
                    "%s[%s] Excluding '%s' (id=%d) and %d descendant(s)",
                    prefix, dest_name, src_child.name, src_child.item_id, subtree_size,
                )
            else:
                logger.info(
                    "%s[%s] Excluding '%s' (id=%d)",
                    prefix, dest_name, src_child.name, src_child.item_id,
                )
            continue

        if _is_container(src_child):
            # --- Container (folder / sub-folder) ---
            dest_child = (
                dest_parent.find_child_by_name(src_child.name)
                if dest_parent is not None
                else None
            )

            if dest_child is None:
                # Container missing -> create it + recurse into its subtree
                descendant_count = _count_nodes(src_child) - 1
                logger.info(
                    "%s[%s] Container '%s' not found -> will create + sync %d descendant(s)",
                    prefix,
                    dest_name,
                    src_child.name,
                    descendant_count,
                )
                plan.actions.append(
                    SyncAction(
                        action_type=ActionType.CREATE_FOLDER,
                        source_node=src_child,
                        dest_parent_id=dest_parent_id,
                        dest_project_id=dest_project_id,
                        dest_name=dest_name,
                        source_parent_id=source_parent.item_id,
                    )
                )
                # Recurse -- children will use a sentinel dest_parent_id
                # (negative of source folder's item_id) that gets resolved
                # to the real dest folder ID during execution.
                _diff_recursive(
                    source_parent=src_child,
                    dest_parent=None,           # nothing exists yet
                    dest_parent_id=-src_child.item_id,  # sentinel
                    dest_project_id=dest_project_id,
                    dest_name=dest_name,
                    plan=plan,
                    indent=indent + 1,
                    exclude_ids=_exclude,
                )
            else:
                # Container exists -> recurse to compare children
                _diff_recursive(
                    source_parent=src_child,
                    dest_parent=dest_child,
                    dest_parent_id=dest_child.item_id,
                    dest_project_id=dest_project_id,
                    dest_name=dest_name,
                    plan=plan,
                    indent=indent + 1,
                    exclude_ids=_exclude,
                )

        else:
            # --- Leaf item ---
            already_exists = False
            if dest_parent is not None:
                existing_global_ids = dest_parent.child_global_ids()
                already_exists = src_child.global_id in existing_global_ids

            if not already_exists:
                plan.actions.append(
                    SyncAction(
                        action_type=ActionType.CREATE_REUSE,
                        source_node=src_child,
                        dest_parent_id=dest_parent_id,
                        dest_project_id=dest_project_id,
                        dest_name=dest_name,
                        source_parent_id=source_parent.item_id,
                    )
                )
