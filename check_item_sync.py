#!/usr/bin/env python3
"""Check and fix out-of-sync reused items across Jama projects.

Walks the source library tree, finds every leaf item that has synced
copies in other projects, checks whether each copy is in sync, and
optionally pushes the source field values to any stale copies.

Usage
-----
    # Report only -- show which items are out of sync:
    python check_item_sync.py

    # Fix out-of-sync items (push source fields to stale copies):
    python check_item_sync.py --fix

    # Verbose logging:
    python check_item_sync.py --verbose

    # Custom config:
    python check_item_sync.py --config config.test.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from jama_sync.client import JamaSyncClient
from jama_sync.reuse import _EXCLUDED_FIELDS
from jama_sync.tree import fetch_tree
from jama_sync.models import TreeNode

logger = logging.getLogger("jama_sync")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _writable_fields(fields: dict) -> dict:
    """Strip read-only / non-portable fields before PUT."""
    return {k: v for k, v in fields.items() if k not in _EXCLUDED_FIELDS}


def _collect_leaf_items(node: TreeNode, exclude_ids: set[int]) -> list[TreeNode]:
    """Recursively collect all leaf items (non-containers) from the tree."""
    leaves: list[TreeNode] = []
    for child in node.children:
        if child.item_id in exclude_ids:
            continue
        if child.children:
            leaves.extend(_collect_leaf_items(child, exclude_ids))
        else:
            leaves.append(child)
    return leaves


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def check_sync(
    client: JamaSyncClient,
    source_tree: TreeNode,
    exclude_ids: set[int],
    fix: bool = False,
) -> tuple[int, int, int, int]:
    """Check sync status for all leaf items in the source tree.

    Parameters
    ----------
    client:
        Authenticated Jama API client.
    source_tree:
        The source library tree.
    exclude_ids:
        Item IDs to skip.
    fix:
        If True, push source fields to out-of-sync copies.

    Returns
    -------
    tuple of (checked, in_sync, out_of_sync, fixed)
    """
    leaves = _collect_leaf_items(source_tree, exclude_ids)
    logger.info("Found %d leaf item(s) to check.", len(leaves))

    total_checked = 0
    total_in_sync = 0
    total_out_of_sync = 0
    total_fixed = 0

    for i, leaf in enumerate(leaves, 1):
        if i % 50 == 0:
            logger.info("  Progress: %d / %d items ...", i, len(leaves))

        # Get all synced copies of this source item
        try:
            synced_items = client.get_items_synced(leaf.item_id)
        except Exception as exc:
            logger.warning(
                "  Could not get synced items for '%s' (id=%d): %s",
                leaf.name, leaf.item_id, exc,
            )
            continue

        # Filter to only copies in OTHER projects (skip the source itself)
        copies = [s for s in synced_items if s["id"] != leaf.item_id]
        if not copies:
            continue

        # Check each copy's sync status
        for copy in copies:
            copy_id = copy["id"]
            copy_project = copy.get("project", "?")
            total_checked += 1

            try:
                in_sync = client.get_sync_status(leaf.item_id, copy_id)
            except Exception as exc:
                logger.warning(
                    "  Could not check sync status for '%s' (source=%d, copy=%d): %s",
                    leaf.name, leaf.item_id, copy_id, exc,
                )
                continue

            if in_sync:
                total_in_sync += 1
            else:
                total_out_of_sync += 1
                logger.info(
                    "  OUT OF SYNC: '%s' (source=%d, copy=%d, project=%s)",
                    leaf.name, leaf.item_id, copy_id, copy_project,
                )

                if fix:
                    try:
                        _push_fields(client, leaf.item_id, copy)
                        total_fixed += 1
                        logger.info(
                            "    FIXED: pushed source fields to copy %d",
                            copy_id,
                        )
                    except Exception as exc:
                        logger.error(
                            "    FAILED to fix copy %d: %s",
                            copy_id, exc,
                        )

    return total_checked, total_in_sync, total_out_of_sync, total_fixed


def _push_fields(
    client: JamaSyncClient,
    source_item_id: int,
    copy_item: dict,
) -> None:
    """Read the source item's fields and PUT them to a stale copy."""
    # Get fresh source data
    source = client.get_item(source_item_id)
    source_fields = _writable_fields(source.get("fields", {}))

    # Get copy's location so we don't change it
    copy_id = copy_item["id"]
    copy_project = copy_item.get("project", 0)
    copy_item_type = copy_item.get("itemType", 0)
    copy_child_item_type = copy_item.get("childItemType", None)
    copy_parent = copy_item.get("location", {}).get("parent", {}).get("item", 0)

    # Preserve the copy's setKey if it has one (project-specific)
    copy_fields = copy_item.get("fields", {})
    if "setKey" in copy_fields and "setKey" in source_fields:
        source_fields["setKey"] = copy_fields["setKey"]

    client.update_item(
        item_id=copy_id,
        project_id=copy_project,
        item_type_id=copy_item_type,
        child_item_type_id=copy_child_item_type,
        parent_id=copy_parent,
        fields=source_fields,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: Config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check and fix out-of-sync reused items across Jama projects.",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"),
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Push source fields to out-of-sync copies (without this flag, report only)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    sync_cfg = cfg.get("sync", {})

    # Logging
    level = logging.DEBUG if args.verbose else getattr(
        logging, sync_cfg.get("log_level", "INFO").upper(), logging.INFO
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
    )
    logger.setLevel(level)
    logger.addHandler(handler)

    if args.fix:
        logger.info("=" * 60)
        logger.info("  FIX MODE -- will push source fields to stale copies")
        logger.info("=" * 60)
    else:
        logger.info("=" * 60)
        logger.info("  REPORT MODE -- showing out-of-sync items (use --fix to update)")
        logger.info("=" * 60)

    # Connect
    jama_cfg = cfg["jama"]
    client = JamaSyncClient(
        base_url=jama_cfg["base_url"],
        client_id=jama_cfg["client_id"],
        client_secret=jama_cfg["client_secret"],
    )

    # Fetch source tree
    source_cfg = cfg["source"]
    logger.info("Fetching source tree (component=%d) ...", source_cfg["component_id"])
    source_tree = fetch_tree(client, source_cfg["component_id"])

    exclude_ids: set[int] = set(source_cfg.get("exclude_ids", []))
    if exclude_ids:
        logger.info("Excluding %d source item(s): %s", len(exclude_ids), sorted(exclude_ids))

    # Check sync
    logger.info("")
    checked, in_sync, out_of_sync, fixed = check_sync(
        client, source_tree, exclude_ids, fix=args.fix,
    )

    # Summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("  SYNC STATUS SUMMARY")
    logger.info("=" * 60)
    logger.info("  Copies checked:      %d", checked)
    logger.info("  In sync:             %d", in_sync)
    logger.info("  Out of sync:         %d", out_of_sync)
    if args.fix:
        logger.info("  Fixed:               %d", fixed)
    else:
        if out_of_sync > 0:
            logger.info("")
            logger.info("  Run with --fix to push source fields to stale copies.")
    logger.info("=" * 60)

    if out_of_sync > 0 and not args.fix:
        sys.exit(1)


if __name__ == "__main__":
    main()
