"""Journal d'exécution hors OpenProject : un fichier JSONL par jour, rétention bornée."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


class Journal:
    def __init__(self, directory: str, retention_days: int):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._retention_s = retention_days * 86400

    def write(self, record: dict) -> None:
        now = datetime.now(timezone.utc)
        record = {"ts": now.isoformat(), **record}
        with open(self._dir / f"runs-{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def purge(self) -> int:
        limit = time.time() - self._retention_s
        removed = 0
        for f in self._dir.glob("runs-*.jsonl"):
            if f.stat().st_mtime < limit:
                f.unlink()
                removed += 1
        return removed
