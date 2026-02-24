"""Execute a sync plan -- create folders, duplicate items, and establish Global-ID links."""

from __future__ import annotations

import logging
import re

from .client import JamaSyncClient
from .models import ActionResult, ActionType, SyncAction, SyncPlan, SyncResult

logger = logging.getLogger(__name__)


def execute_sync(
    client: JamaSyncClient,
    plan: SyncPlan,
    dry_run: bool = False,
) -> SyncResult:
    """Execute every action in *plan*, creating folders and reused items.

    Parameters
    ----------
    client:
        Authenticated Jama API client.
    plan:
        The sync plan produced by :func:`diff_trees`.
    dry_run:
        If ``True``, log what *would* happen but make no API calls.

    Returns
    -------
    SyncResult
        Per-action outcomes including IDs of newly created items (keyed by
        destination folder ID, for the reorder step).
    """
    result = SyncResult(dest_name=plan.dest_name)

    if plan.is_empty:
        logger.info("[%s] Nothing to sync -- already up to date.", plan.dest_name)
        return result

    # Sort CREATE_REUSE actions alphabetically within each parent group.
    # POST /items appends new items at the end of the parent, so creating
    # them in A-Z order means they land in alphabetical order.  We keep
    # CREATE_FOLDER actions in their original position (depth-first) so
    # sentinel IDs resolve correctly.
    _sort_actions_alphabetically(plan.actions)

    # Map source folder item_id -> newly created destination folder item_id.
    # When diff_trees encounters a missing folder it records children with
    # dest_parent_id = -source_folder_id (a negative sentinel).  After the
    # folder is created we store the mapping here so children can resolve
    # their real destination parent.
    created_folder_map: dict[int, int] = {}

    for action in plan.actions:
        # ---------------------------------------------------------
        # Resolve sentinel parent IDs for BOTH folders and items.
        # Sentinels are negative: -source_parent_item_id.
        # ---------------------------------------------------------
        effective_parent = action.dest_parent_id
        if effective_parent < 0:
            source_parent_id = -effective_parent  # undo negation
            resolved = created_folder_map.get(source_parent_id)
            if resolved is not None:
                effective_parent = resolved
            else:
                result.results.append(
                    ActionResult(
                        action=action,
                        success=False,
                        error=(
                            f"Cannot resolve destination folder for source "
                            f"parent {source_parent_id} -- parent folder "
                            f"creation may have failed."
                        ),
                    )
                )
                continue

        if action.action_type == ActionType.CREATE_FOLDER:
            # Override the action's dest_parent_id with the resolved value
            # so _create_folder uses the real destination parent.
            action.dest_parent_id = effective_parent
            ar = _create_folder(client, action, dry_run)
            result.results.append(ar)
            if ar.success and ar.created_id is not None:
                # Key = source folder's item_id  ->  Value = new dest folder id
                created_folder_map[action.source_node.item_id] = ar.created_id

        elif action.action_type == ActionType.CREATE_REUSE:
            ar = _create_reused_item(client, action, effective_parent, dry_run)
            result.results.append(ar)

            # Track new item for reorder step
            if ar.success and ar.created_id is not None:
                result.new_item_ids.setdefault(effective_parent, []).append(
                    ar.created_id
                )

    logger.info("[%s] Sync complete -- %s", plan.dest_name, result.summary())
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sort_actions_alphabetically(actions: list[SyncAction]) -> None:
    """Sort CREATE_REUSE actions alphabetically (in-place) within each parent.

    Since ``POST /items`` appends new items at the end of the parent's
    child list, creating them in A-Z order ensures they land in
    alphabetical order.

    CREATE_FOLDER actions stay in their original (depth-first) positions
    so that sentinel parent IDs resolve correctly during execution.
    All CREATE_REUSE items for a given parent are collected, sorted A-Z,
    and placed after the last CREATE_FOLDER action for that same parent
    group (or in their original position cluster).
    """
    # Separate folder actions (order-sensitive) from reuse actions (sortable).
    folder_actions = [a for a in actions if a.action_type == ActionType.CREATE_FOLDER]
    reuse_actions = [a for a in actions if a.action_type == ActionType.CREATE_REUSE]

    # Group reuse actions by dest_parent_id and sort each group A-Z.
    from collections import defaultdict
    reuse_by_parent: dict[int, list[SyncAction]] = defaultdict(list)
    for a in reuse_actions:
        reuse_by_parent[a.dest_parent_id].append(a)
    for parent_id in reuse_by_parent:
        reuse_by_parent[parent_id].sort(key=lambda a: a.source_node.name.lower())

    # Rebuild the actions list: walk through the original order.
    # - Emit CREATE_FOLDER actions in their original position.
    # - When we encounter the first CREATE_REUSE for a parent, emit all
    #   sorted reuse actions for that parent at once, then skip subsequent
    #   occurrences.
    emitted_parents: set[int] = set()
    new_actions: list[SyncAction] = []

    for action in actions:
        if action.action_type == ActionType.CREATE_FOLDER:
            new_actions.append(action)
        elif action.action_type == ActionType.CREATE_REUSE:
            pid = action.dest_parent_id
            if pid not in emitted_parents:
                emitted_parents.add(pid)
                new_actions.extend(reuse_by_parent[pid])
            # else: already emitted, skip

    actions[:] = new_actions


# Fields that must be stripped before POST/PUT requests.
# Includes read-only system fields and project-scoped fields whose IDs
# are not valid across projects.
_EXCLUDED_FIELDS = frozenset({
    # Read-only system fields
    "documentKey",
    "globalId",
    "createdBy",
    "createdDate",
    "modifiedBy",
    "modifiedDate",
    # Project-scoped -- release IDs are per-project and not portable
    "release",
})

# Additional fields stripped only when creating folders (where we generate
# our own setKey).  For reused leaf items that happen to be empty sets,
# we keep the source setKey so Jama doesn't reject the POST.
_FOLDER_EXTRA_EXCLUDED = frozenset({"setKey"})

# Track generated setKeys within a run to avoid duplicates across folders.
_used_set_keys: set[str] = set()


def _writable_fields(fields: dict, extra_exclude: frozenset = frozenset()) -> dict:
    """Return a copy of *fields* with excluded fields removed."""
    exclude = _EXCLUDED_FIELDS | extra_exclude
    return {k: v for k, v in fields.items() if k not in exclude}


def _generate_set_key(name: str) -> str:
    """Generate a unique setKey from a folder name.

    Jama constraints: 1-16 characters, letters/numbers/underscores only.
    Strategy: strip special chars, uppercase, truncate to 16.  If the
    result collides with a previously generated key in this run, append
    a numeric suffix.
    """
    key = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    key = key[:16]

    # Ensure uniqueness within this sync run
    candidate = key
    counter = 1
    while candidate in _used_set_keys:
        suffix = f"_{counter}"
        candidate = key[: 16 - len(suffix)] + suffix
        counter += 1

    _used_set_keys.add(candidate)
    return candidate


def _create_folder(
    client: JamaSyncClient,
    action: SyncAction,
    dry_run: bool,
) -> ActionResult:
    """Create a structural folder in the destination."""
    src = action.source_node
    set_key = _generate_set_key(src.name)
    label = f"Create folder '{src.name}' (setKey={set_key}) in [{action.dest_name}]"

    if dry_run:
        logger.info("  [DRY RUN] %s", label)
        return ActionResult(action=action, success=True, message=f"DRY RUN: {label}")

    try:
        # Build fields: name + setKey (required by Jama for sets/folders)
        folder_fields = {
            "name": src.fields.get("name", src.name),
            "setKey": set_key,
        }
        # Carry over description if present
        if "description" in src.fields:
            folder_fields["description"] = src.fields["description"]

        logger.debug("  Folder fields: %s", folder_fields)

        new_id = client.create_item(
            project_id=action.dest_project_id,
            item_type_id=src.item_type_id,
            child_item_type_id=src.child_item_type_id,
            parent_id=action.dest_parent_id,
            fields=folder_fields,
        )
        msg = f"{label} -> id={new_id}"
        logger.info("  OK %s", msg)
        return ActionResult(action=action, success=True, created_id=new_id, message=msg)

    except Exception as exc:
        msg = f"{label} FAILED: {exc}"
        logger.error("  FAIL %s", msg)
        return ActionResult(action=action, success=False, error=str(exc))


def _create_reused_item(
    client: JamaSyncClient,
    action: SyncAction,
    dest_parent_id: int,
    dry_run: bool,
) -> ActionResult:
    """Create a duplicate item in the destination and sync its Global ID.

    Two API calls:
    1. ``POST /items`` -- create the item with source field data.
    2. ``POST /items/{pool}/synceditems`` -- link Global IDs so the item
       stays synchronised with the source ("reuse").
    """
    src = action.source_node
    label = f"Reuse '{src.name}' (global_id={src.global_id}) -> [{action.dest_name}]"

    if dry_run:
        logger.info("  [DRY RUN] %s", label)
        return ActionResult(action=action, success=True, message=f"DRY RUN: {label}")

    try:
        # Step 1: Create duplicate item in destination
        #         Strip read-only system fields that Jama rejects on POST.
        clean_fields = _writable_fields(src.fields)

        # If the source item has a setKey (it's a set/folder type even if
        # empty), generate a unique one for the destination.
        if "setKey" in src.fields:
            clean_fields["setKey"] = _generate_set_key(src.name)

        logger.debug("  Item fields for '%s': %s", src.name, clean_fields)
        new_id = client.create_item(
            project_id=action.dest_project_id,
            item_type_id=src.item_type_id,
            child_item_type_id=src.child_item_type_id,
            parent_id=dest_parent_id,
            fields=clean_fields,
        )
        logger.debug("  Created item id=%d for '%s'", new_id, src.name)

        # Step 2: Sync Global IDs  (new item adopts source's Global ID)
        client.sync_items(new_item_id=new_id, source_item_id=src.item_id)

        msg = f"{label} -> new_id={new_id}"
        logger.info("  OK %s", msg)
        return ActionResult(action=action, success=True, created_id=new_id, message=msg)

    except Exception as exc:
        msg = f"{label} FAILED: {exc}"
        logger.error("  FAIL %s", msg)
        return ActionResult(action=action, success=False, error=str(exc))
