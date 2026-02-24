"""Data classes for Jama Library Sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


@dataclass
class TreeNode:
    """Represents an item in the Jama hierarchy (component, folder, or leaf item).

    Attributes:
        item_id:            Jama API ID of this item.
        global_id:          Jama Global ID (shared across reused/synced copies).
        name:               Display name of the item.
        item_type_id:       Jama item type ID.
        child_item_type_id: Jama item type ID for children of this item.
        fields:             Dict of all item fields (name, description, custom fields, etc.).
        children:           Ordered list of direct child nodes.
    """

    item_id: int
    global_id: str
    name: str
    item_type_id: int
    child_item_type_id: int | None
    fields: dict[str, Any]
    children: list[TreeNode] = field(default_factory=list)

    def child_global_ids(self) -> set[str]:
        """Return the set of global IDs for all direct children."""
        return {c.global_id for c in self.children}

    def find_child_by_name(self, name: str) -> TreeNode | None:
        """Find a direct child by name (case-insensitive)."""
        name_lower = name.lower()
        for child in self.children:
            if child.name.lower() == name_lower:
                return child
        return None

    def __repr__(self) -> str:
        return (
            f"TreeNode(id={self.item_id}, name={self.name!r}, "
            f"global_id={self.global_id!r}, children={len(self.children)})"
        )


class ActionType(Enum):
    """Types of sync actions."""

    CREATE_FOLDER = auto()
    CREATE_REUSE = auto()


@dataclass
class SyncAction:
    """A single planned sync operation.

    Attributes:
        action_type:      What to do (create folder or create reused item).
        source_node:      The source TreeNode being replicated.
        dest_parent_id:   The item ID of the destination parent (folder or component).
                          Use a *negative* sentinel to signal "resolve from
                          the created-folders map at execution time".  The
                          sentinel value is ``-source_parent_item_id``.
        dest_project_id:  The destination project ID.
        dest_name:        Human-readable destination project name (for logging).
        source_parent_id: Item ID of the *source* parent node.  Used during
                          execution to look up the corresponding newly-created
                          destination folder when ``dest_parent_id`` is a
                          sentinel (negative value).
    """

    action_type: ActionType
    source_node: TreeNode
    dest_parent_id: int
    dest_project_id: int
    dest_name: str
    source_parent_id: int = 0


@dataclass
class SyncPlan:
    """Complete plan of all actions to execute for one destination.

    Attributes:
        dest_name:      Human-readable destination project name.
        dest_project_id: Destination project ID.
        actions:        Ordered list of sync actions (folders first, then items).
        folders_to_create: Count of new folders.
        items_to_reuse:   Count of new items to reuse/sync.
    """

    dest_name: str
    dest_project_id: int
    actions: list[SyncAction] = field(default_factory=list)

    @property
    def folders_to_create(self) -> int:
        return sum(1 for a in self.actions if a.action_type == ActionType.CREATE_FOLDER)

    @property
    def items_to_reuse(self) -> int:
        return sum(1 for a in self.actions if a.action_type == ActionType.CREATE_REUSE)

    @property
    def is_empty(self) -> bool:
        return len(self.actions) == 0

    def summary(self) -> str:
        return (
            f"[{self.dest_name}] "
            f"{self.folders_to_create} folder(s) to create, "
            f"{self.items_to_reuse} item(s) to reuse"
        )


@dataclass
class ActionResult:
    """Result of executing a single sync action.

    Attributes:
        action:     The action that was attempted.
        success:    Whether it succeeded.
        created_id: The Jama item ID of the newly created item (if successful).
        message:    Human-readable description of what happened.
        error:      Error message if failed.
    """

    action: SyncAction
    success: bool
    created_id: int | None = None
    message: str = ""
    error: str = ""


@dataclass
class SyncResult:
    """Aggregated results of executing an entire sync plan.

    Attributes:
        dest_name:      Human-readable destination project name.
        results:        Individual action results.
        new_item_ids:   Mapping of dest_folder_id -> list of newly created item IDs
                        (used by reorder step).
    """

    dest_name: str
    results: list[ActionResult] = field(default_factory=list)
    new_item_ids: dict[int, list[int]] = field(default_factory=dict)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.success)

    def summary(self) -> str:
        return (
            f"[{self.dest_name}] "
            f"{self.succeeded} succeeded, {self.failed} failed"
        )
