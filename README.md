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

## Building folder structures

`build_folders.py` creates a nested folder structure under any Jama item (component, set, or folder) from an indented text outline. It uses the same `config.yaml` (only the `jama` section is required) and is independent of the library sync.

### Outline format

Two spaces per level. An optional `| KEY` sets the item type the folder holds; subfolders inherit it. Lines starting with `#` are ignored. See `outline_example.txt`:

```
Flight Deck | REQ
  Controls
    Sidestick
    Throttle Quadrant
  Displays
Monuments | DES
  Glareshield
```

### Preview (dry run, the default)

```
python build_folders.py outline_example.txt --parent 123456 --child-type REQ
```

### Create the folders

```
python build_folders.py outline_example.txt --parent 123456 --child-type REQ --no-dry-run
```

- `--parent` is the API ID of the parent item (the number after `docId=` in the Jama URL, not the document key).
- `--child-type` is the default item type key for folders with no `| KEY`.
- All item type keys are validated before anything is created.
- Folders that already exist under the same parent with the same name (case-insensitive) are reused, so re-running is safe.

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
  build_folders.py         # CLI -- build a folder structure from an outline
  outline_example.txt      # Example outline for build_folders.py
  config.example.yaml      # Example config (safe to commit)
  config.yaml              # Your config (gitignored)
  requirements.txt         # Python dependencies
  jama_sync/
    __init__.py
    client.py              # Jama API wrapper with retry logic
    models.py              # Data classes (TreeNode, SyncAction, SyncPlan, etc.)
    tree.py                # Tree fetching and diffing
    reuse.py               # Sync execution (folder creation, item reuse, Global ID linking)
    folders.py             # Outline parsing and folder-structure building
```

## Notes

- The tool defaults to dry run mode for safety. Use `--no-dry-run` to execute.
- New items are created in alphabetical order so they appear sorted in the destination.
- Folders are matched by name (case-insensitive). Leaf items are matched by Global ID.
- The tool is idempotent -- running it again will only create items that are missing.
