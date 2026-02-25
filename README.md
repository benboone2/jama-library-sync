# Jama Library Sync

Automates reuse of a shared Library of Common Reference Material across multiple Jama Connect projects.

Instead of manually adding items one-by-one from the library into each destination project, this tool reads the source component, compares it against each destination, creates missing folders and reuse items, and establishes Global ID sync links -- all via the Jama REST API.

## What it does

1. Fetches the full item tree from a source library component
2. Fetches the item tree from each destination component
3. Diffs the trees to find missing folders and items
4. Creates folders and reuse items in the destination (in alphabetical order)
5. Establishes Jama Global ID sync links so reused items stay synchronized

## Prerequisites

- Python 3.10+
- A Jama Connect instance with REST API access
- OAuth 2.0 API credentials (Client ID + Secret)

## Setup

1. Clone the repository:

   ```
   git clone <repo-url>
   cd jama-library-sync
   ```

2. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

3. Create your config file:

   ```
   cp config.example.yaml config.yaml
   ```

4. Edit `config.yaml` with your Jama credentials and project/component IDs.

## Configuration

Edit `config.yaml` to configure:

- **jama** -- Your Jama instance URL and OAuth 2.0 credentials
- **source** -- The library project and component to sync from
- **source.exclude_ids** -- Optional list of item IDs to skip (folders and their subtrees are also skipped)
- **destinations** -- One or more projects/components to sync into
- **sync.dry_run** -- Set to `true` to preview changes without making them

### Getting Jama IDs

- **Project ID**: Open the project in Jama, check the URL (e.g., `/project/63/...`)
- **Component ID**: Click the component in the Explorer Tree, check the URL or use the API (`GET /projects/{id}/components`)
- **Item ID**: Click any item, check the URL or the item details panel

### Excluding items

Add item IDs to `source.exclude_ids` to skip them during sync. If the excluded item is a folder, its entire subtree is also skipped:

```yaml
source:
  project_id: 63
  component_id: 8010
  exclude_ids:
    - 714956
    - 346807
```

## Usage

### Dry run (preview changes)

```
python sync.py --dry-run
```

### Execute sync

```
python sync.py --no-dry-run
```

### Verbose logging

```
python sync.py --dry-run --verbose
```

### Custom config file

```
python sync.py --config config.test.yaml --dry-run
```

## Checking sync status

After reuse links are established, source item fields can change over time (e.g., a definition gets updated). Jama tracks this but doesn't auto-push changes to copies. Use `check_item_sync.py` to find and fix stale copies.

### Report out-of-sync items

```
python check_item_sync.py
```

### Fix out-of-sync items (push source fields to stale copies)

```
python check_item_sync.py --fix
```

This walks every leaf item in the source library, checks each synced copy's status via the API, and (with `--fix`) pushes the source's current field values to any out-of-sync copies.

## How Jama reuse works

Jama "reuse" is a two-step process:

1. **Create a duplicate item** (`POST /items`) in the destination with the same field data
2. **Sync Global IDs** (`POST /items/{pool}/synceditems`) to link the new item to the source

After syncing, both items share the same Global ID and field changes propagate through Jama's built-in reuse mechanism.

## Project structure

```
jama-library-sync/
  sync.py                  # CLI -- create reuse links for missing items
  check_item_sync.py       # CLI -- check/fix out-of-sync reused items
  config.example.yaml      # Example config (safe to commit)
  config.yaml              # Your config (gitignored)
  requirements.txt         # Python dependencies
  jama_sync/
    __init__.py
    client.py              # Jama API wrapper with retry logic
    models.py              # Data classes (TreeNode, SyncAction, SyncPlan, etc.)
    tree.py                # Tree fetching and diffing
    reuse.py               # Sync execution (folder creation, item reuse, Global ID linking)
```

## Notes

- The tool defaults to dry run mode for safety. Use `--no-dry-run` to execute.
- New items are created in alphabetical order so they appear sorted in the destination.
- Folders are matched by name (case-insensitive). Leaf items are matched by Global ID.
- The tool is idempotent -- running it again will only create items that are missing.
