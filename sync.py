#!/usr/bin/env python3
"""Jama Library Sync -- CLI entry point.

Syncs the Library of Common Reference Material component to one or more
destination projects via Jama Connect's REST API.

Usage
-----
    # Preview changes (dry run -- default on first run):
    python sync.py --dry-run

    # Execute sync:
    python sync.py

    # Custom config path + verbose logging:
    python sync.py --config /path/to/config.yaml --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from jama_sync.client import JamaSyncClient
from jama_sync.reuse import execute_sync
from jama_sync.tree import diff_trees, fetch_tree

logger = logging.getLogger("jama_sync")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    """Load and validate the YAML config file."""
    if not path.exists():
        print(
            f"ERROR: Config file not found: {path}\n"
            f"  Copy config.example.yaml -> config.yaml and fill in your values.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    # --- Validate required keys ---
    errors: list[str] = []

    jama = cfg.get("jama", {})
    for key in ("base_url", "client_id", "client_secret"):
        if not jama.get(key):
            errors.append(f"jama.{key} is required")

    source = cfg.get("source", {})
    for key in ("project_id", "component_id"):
        if not source.get(key):
            errors.append(f"source.{key} is required")

    destinations = cfg.get("destinations", [])
    if not destinations:
        errors.append("At least one destination is required")
    for i, dest in enumerate(destinations):
        for key in ("name", "project_id", "component_id"):
            if not dest.get(key):
                errors.append(f"destinations[{i}].{key} is required")

    if errors:
        print("ERROR: Config validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  * {e}", file=sys.stderr)
        sys.exit(1)

    return cfg


def setup_logging(level_name: str, verbose: bool) -> None:
    """Configure the root logger for jama_sync."""
    level = logging.DEBUG if verbose else getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
    )
    logger.setLevel(level)
    logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Jama Library of Common Reference Material to destination projects.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Preview changes without making any API writes",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Execute sync (override dry_run=true in config)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()

    # --- Load config ---
    cfg = load_config(args.config)
    sync_cfg = cfg.get("sync", {})

    # Determine dry_run: CLI flags override config
    if args.dry_run:
        dry_run = True
    elif args.no_dry_run:
        dry_run = False
    else:
        dry_run = sync_cfg.get("dry_run", True)  # default to dry_run for safety

    setup_logging(sync_cfg.get("log_level", "INFO"), args.verbose)

    if dry_run:
        logger.info("=" * 60)
        logger.info("  DRY RUN -- no changes will be made")
        logger.info("=" * 60)

    # --- Connect to Jama ---
    jama_cfg = cfg["jama"]
    client = JamaSyncClient(
        base_url=jama_cfg["base_url"],
        client_id=jama_cfg["client_id"],
        client_secret=jama_cfg["client_secret"],
    )

    # --- Validate source ---
    source_cfg = cfg["source"]
    logger.info("Validating source component (id=%d) ...", source_cfg["component_id"])
    try:
        source_tree = fetch_tree(client, source_cfg["component_id"])
    except Exception as exc:
        logger.error(
            "Failed to fetch source component (id=%d): %s",
            source_cfg["component_id"],
            exc,
        )
        sys.exit(1)

    # --- Build exclusion set ---
    exclude_ids: set[int] = set(source_cfg.get("exclude_ids", []))
    if exclude_ids:
        logger.info("Excluding %d source item(s): %s", len(exclude_ids), sorted(exclude_ids))

    # --- Process each destination ---
    total_synced = 0
    total_failed = 0

    for dest_cfg in cfg["destinations"]:
        dest_name = dest_cfg["name"]
        dest_project_id = dest_cfg["project_id"]
        dest_component_id = dest_cfg["component_id"]

        logger.info("")
        logger.info("-" * 50)
        logger.info("Destination: %s  (project=%d, component=%d)", dest_name, dest_project_id, dest_component_id)
        logger.info("-" * 50)

        # Fetch destination tree
        try:
            dest_tree = fetch_tree(client, dest_component_id)
        except Exception as exc:
            logger.error(
                "Failed to fetch destination '%s' (component=%d): %s",
                dest_name,
                dest_component_id,
                exc,
            )
            total_failed += 1
            continue

        # Diff
        plan = diff_trees(
            source_tree, dest_tree, dest_project_id, dest_name,
            exclude_ids=exclude_ids,
        )

        if plan.is_empty:
            logger.info("[%s] Already up to date -- nothing to sync.", dest_name)
            continue

        # Print plan
        logger.info("")
        logger.info("[%s] Sync plan:", dest_name)
        logger.info("  Folders to create:  %d", plan.folders_to_create)
        logger.info("  Items to reuse:     %d", plan.items_to_reuse)
        logger.info("")

        # Execute (new items are created in A-Z order so they append alphabetically)
        result = execute_sync(client, plan, dry_run=dry_run)
        total_synced += result.succeeded
        total_failed += result.failed

    # --- Summary ---
    logger.info("")
    logger.info("=" * 60)
    logger.info("  SUMMARY")
    logger.info("=" * 60)
    logger.info("  Items synced (reused):   %d", total_synced)
    logger.info("  Failures:                %d", total_failed)
    if dry_run:
        logger.info("")
        logger.info("  This was a DRY RUN. To apply changes, run with --no-dry-run")
    logger.info("=" * 60)

    if total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
