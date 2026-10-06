#!/usr/bin/env python3
"""Build a nested folder structure under a Jama item from an indented outline.

The parent can be a component, set, or folder.  Folders that already exist
under the same parent with the same name are reused, not duplicated, so
re-running is safe.  See ``outline_example.txt`` for the outline format.

Usage
-----
    # Preview (default) -- validates type keys and shows what would be created:
    python build_folders.py outline.txt --parent 123456 --child-type REQ

    # Create the folders:
    python build_folders.py outline.txt --parent 123456 --child-type REQ --no-dry-run

    # Custom config + verbose logging:
    python build_folders.py outline.txt --parent 123456 --child-type REQ --config config.test.yaml -v

``--parent`` is the API ID of the parent item (the number after ``docId=`` in
the Jama URL, not the document key).  Only the ``jama`` section of the config
file is used.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from jama_sync.client import JamaSyncClient
from jama_sync.folders import (
    FOLDER_TYPE_KEY,
    OutlineError,
    build_folders,
    format_tree,
    iter_nodes,
    parse_outline,
)

logger = logging.getLogger("jama_sync")


def load_config(path: Path) -> dict:
    """Load the YAML config; only the ``jama`` section is required."""
    if not path.exists():
        print(
            f"ERROR: Config file not found: {path}\n"
            f"  Copy config.example.yaml -> config.yaml and fill in your values.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    jama = cfg.get("jama", {})
    missing = [k for k in ("base_url", "client_id", "client_secret") if not jama.get(k)]
    if missing:
        print("ERROR: Config validation failed:", file=sys.stderr)
        for k in missing:
            print(f"  * jama.{k} is required", file=sys.stderr)
        sys.exit(1)
    return cfg


def setup_logging(level_name: str, verbose: bool) -> None:
    level = logging.DEBUG if verbose else getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
    )
    logger.setLevel(level)
    logger.addHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a nested folder structure under a Jama item from an indented outline.",
    )
    parser.add_argument("outline", type=Path, help="Path to the indented outline text file")
    parser.add_argument(
        "--parent", type=int, required=True,
        help="API ID of the component/set/folder to build under",
    )
    parser.add_argument(
        "--child-type", required=True,
        help="Default item type key the folders hold, e.g. REQ (override per line with '| KEY')",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"),
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without making any API writes (default)",
    )
    parser.add_argument(
        "--no-dry-run", action="store_true",
        help="Create the folders",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG-level logging")
    args = parser.parse_args()

    cfg = load_config(args.config)
    sync_cfg = cfg.get("sync", {})
    setup_logging(sync_cfg.get("log_level", "INFO"), args.verbose)
    dry_run = args.dry_run or not args.no_dry_run

    # --- Parse the outline (no API calls yet) ---
    try:
        tree = parse_outline(str(args.outline), args.child_type.upper())
    except (OSError, OutlineError) as exc:
        logger.error("Could not read outline %s: %s", args.outline, exc)
        sys.exit(1)
    if not tree:
        logger.error("Outline %s contains no folders.", args.outline)
        sys.exit(1)

    logger.info("Outline (%d folder(s)):", sum(1 for _ in iter_nodes(tree)))
    for line in format_tree(tree):
        logger.info("  %s", line)

    if dry_run:
        logger.info("")
        logger.info("=" * 60)
        logger.info("  DRY RUN -- no changes will be made")
        logger.info("=" * 60)

    # --- Connect and resolve item types ---
    jama_cfg = cfg["jama"]
    client = JamaSyncClient(
        base_url=jama_cfg["base_url"],
        client_id=jama_cfg["client_id"],
        client_secret=jama_cfg["client_secret"],
    )

    type_ids = {t["typeKey"].upper(): t["id"] for t in client.get_item_types()}
    folder_type_id = type_ids.get(FOLDER_TYPE_KEY)
    if folder_type_id is None:
        logger.error("No item type with key %s (Folder) found.", FOLDER_TYPE_KEY)
        sys.exit(1)

    unknown = {n.type_key for n in iter_nodes(tree)} - type_ids.keys()
    if unknown:
        logger.error(
            "Unknown item type key(s): %s. Use a valid --child-type / '| KEY'.",
            ", ".join(sorted(unknown)),
        )
        sys.exit(1)

    try:
        parent = client.get_item(args.parent)
    except Exception as exc:
        logger.error("Failed to fetch parent item (id=%d): %s", args.parent, exc)
        sys.exit(1)
    project_id = parent["project"]

    # --- Build ---
    logger.info("")
    logger.info(
        "Building under '%s' (id=%d, project=%d) ...",
        parent.get("fields", {}).get("name", "?"), args.parent, project_id,
    )
    result = build_folders(client, tree, args.parent, project_id, folder_type_id, type_ids, dry_run)

    # --- Summary ---
    logger.info("")
    logger.info("=" * 60)
    logger.info("  SUMMARY")
    logger.info("=" * 60)
    logger.info("  Folders %s:  %d", "to create" if dry_run else "created  ", result.created)
    logger.info("  Already existed:    %d", result.existing)
    logger.info("  Failures:           %d", result.failed)
    if dry_run:
        logger.info("")
        logger.info("  This was a DRY RUN. To create the folders, run with --no-dry-run")
    logger.info("=" * 60)

    if result.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
