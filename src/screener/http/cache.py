"""On-disk response cache keyed by URL + date.

During development the same lookups get re-run constantly; nothing already pulled today
should touch the network again. Entries are plain files so they can be inspected and
deleted by hand.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CacheEntry:
    body: str
    stored_at: datetime
    url: str


class DiskCache:
    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _path(self, url: str, kind: str) -> Path:
        key = self._key(url)
        # Shard by first two hex chars; a year of daily runs over 5k apps is a lot of files.
        return self.root / kind / key[:2] / f"{key}.json"

    def get(self, url: str, kind: str, ttl_hours: int) -> CacheEntry | None:
        if not self.enabled:
            return None
        path = self._path(url, kind)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
            stored_at = datetime.fromisoformat(payload["stored_at"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            # A corrupt entry is not worth a crash mid-crawl; treat it as a miss.
            path.unlink(missing_ok=True)
            return None

        age_hours = (datetime.now(UTC) - stored_at).total_seconds() / 3600.0
        if age_hours > ttl_hours:
            return None
        return CacheEntry(body=payload["body"], stored_at=stored_at, url=payload.get("url", url))

    def set(self, url: str, kind: str, body: str) -> None:
        if not self.enabled:
            return
        path = self._path(url, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "url": url,
            "kind": kind,
            "stored_at": datetime.now(UTC).isoformat(),
            "body": body,
        }
        # Write-then-rename so an interrupted run never leaves a half-written entry.
        tmp = path.with_suffix(f".{time.monotonic_ns()}.tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        tmp.replace(path)

    def clear(self, kind: str | None = None) -> int:
        """Delete cached responses. Returns the number of entries removed."""
        target = self.root / kind if kind else self.root
        if not target.exists():
            return 0
        count = sum(1 for _ in target.rglob("*.json"))
        shutil.rmtree(target)
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
        return count

    def stats(self) -> dict[str, int]:
        if not self.root.exists():
            return {}
        out: dict[str, int] = {}
        for child in sorted(self.root.iterdir()):
            if child.is_dir():
                out[child.name] = sum(1 for _ in child.rglob("*.json"))
        return out
