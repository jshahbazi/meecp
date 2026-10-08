"""Human-readable client registration names; slot UUIDs remain routing identities."""
from __future__ import annotations

from collections import Counter
import re
from typing import Iterable

RESERVED_NAMES = {"meecp-management", "mcpme-management"}
MAX_NAME_LENGTH = 64


def name_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:MAX_NAME_LENGTH].rstrip("-")


def connection_names(slots: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Assign safe names unique among installed apps, including normalized collisions."""
    bases = {slot_id: name_slug(name) for slot_id, name in slots}
    counts = Counter(bases.values())

    def suffixed(slot_id: str) -> str:
        base = bases[slot_id] or "app"
        # Full UUID suffixes also distinguish installations sharing the first eight digits.
        return base[:MAX_NAME_LENGTH - len(slot_id) - 1].rstrip("-") + "-" + slot_id

    result = {slot_id: (suffixed(slot_id) if not base or counts[base] > 1 or base in RESERVED_NAMES else base)
              for slot_id, base in bases.items()}
    while True:
        collisions = Counter(result.values())
        conflicts = [slot_id for slot_id, name in result.items()
                     if collisions[name] > 1 or name in RESERVED_NAMES]
        if not conflicts:
            return result
        # A user-entered name can equal another app's generated suffix. Disambiguate
        # those names too; UUID-suffixed names cannot collide with each other.
        for slot_id in conflicts:
            result[slot_id] = suffixed(slot_id)
