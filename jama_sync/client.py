"""Thin wrapper around py-jama-rest-client with retry logic and logging."""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any, Callable

from py_jama_rest_client.client import JamaClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0          # seconds -- exponential: 2, 4, 8 ...
_WRITE_DELAY = 0.5           # seconds between write operations


def _retry(func: Callable) -> Callable:
    """Retry on transient API errors (429 rate-limit, 5xx server errors)."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc)
                # Detect retryable conditions from the exception message
                is_retryable = any(
                    code in exc_str for code in ("429", "500", "502", "503", "504")
                )
                if is_retryable and attempt < _MAX_RETRIES:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning(
                        "Attempt %d/%d for %s failed (%s). Retrying in %.1fs ...",
                        attempt,
                        _MAX_RETRIES,
                        func.__name__,
                        exc_str[:120],
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise
        raise last_exc  # type: ignore[misc]  # unreachable but satisfies type checker

    return wrapper


# ---------------------------------------------------------------------------
# JamaSyncClient
# ---------------------------------------------------------------------------


class JamaSyncClient:
    """High-level Jama API client for library sync operations.

    Wraps ``py-jama-rest-client.JamaClient`` with:
    - OAuth 2.0 authentication
    - Automatic retry with exponential back-off
    - Convenience methods aligned to sync workflow
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str) -> None:
        logger.info("Connecting to Jama at %s ...", base_url)
        self._client = JamaClient(
            host_domain=base_url,
            credentials=(client_id, client_secret),
            oauth=True,
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    @_retry
    def get_item(self, item_id: int) -> dict[str, Any]:
        """Fetch a single item by ID."""
        logger.debug("GET item %d", item_id)
        return self._client.get_item(item_id)

    @_retry
    def get_children(self, item_id: int) -> list[dict[str, Any]]:
        """Fetch all direct children of an item."""
        logger.debug("GET children of item %d", item_id)
        return self._client.get_item_children(item_id)

    @_retry
    def get_items_synced(self, item_id: int) -> list[dict[str, Any]]:
        """Get all items synced with the given item (shared Global ID)."""
        logger.debug("GET synced items for %d", item_id)
        return self._client.get_items_synceditems(item_id)

    # ------------------------------------------------------------------
    # Write operations (include a small delay to be kind to the API)
    # ------------------------------------------------------------------

    @_retry
    def create_item(
        self,
        project_id: int,
        item_type_id: int,
        child_item_type_id: int | None,
        parent_id: int,
        fields: dict[str, Any],
    ) -> int:
        """Create a new item under *parent_id* and return its item ID.

        Parameters
        ----------
        project_id:
            Destination project ID.
        item_type_id:
            Jama item-type ID for the new item.
        child_item_type_id:
            Jama item-type ID for children of this item (can be same as item_type_id).
            Pass ``None`` if the item will not have children.
        parent_id:
            Item ID of the parent (folder or component) under which to create.
        fields:
            Item field values (must include at least ``name``).
        """
        logger.debug(
            "POST item under parent %d  (project=%d, type=%d, name=%r)",
            parent_id,
            project_id,
            item_type_id,
            fields.get("name", "?"),
        )
        location = {"item": parent_id}
        result = self._client.post_item(
            project=project_id,
            item_type_id=item_type_id,
            child_item_type_id=child_item_type_id or item_type_id,
            location=location,
            fields=fields,
        )
        time.sleep(_WRITE_DELAY)
        return result

    @_retry
    def sync_items(self, new_item_id: int, source_item_id: int) -> int:
        """Establish a Global-ID sync link between two items (Jama "reuse").

        After this call both items share the same Global ID and stay
        synchronised through Jama's built-in reuse mechanism.

        Parameters
        ----------
        new_item_id:
            The newly created destination item (will adopt the Global ID).
        source_item_id:
            The original item in the source library (owns the Global ID pool).

        Returns
        -------
        int
            The modified item's ID.
        """
        logger.debug(
            "POST sync  new_item=%d  <->  source=%d",
            new_item_id,
            source_item_id,
        )
        result = self._client.post_item_sync(
            source_item=new_item_id,
            pool_item=source_item_id,
        )
        time.sleep(_WRITE_DELAY)
        return result

