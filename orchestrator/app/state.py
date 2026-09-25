"""État persistant de l'orchestrateur (SQLite) : dernière vue connue des tâches,
événements déjà traités, curseur de polling."""
from __future__ import annotations

import sqlite3
import threading


class StateStore:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS wp_state (
                wp_id            INTEGER PRIMARY KEY,
                status_id        INTEGER NOT NULL,
                last_activity_id INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS processed_events (
                key        TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def get_wp(self, wp_id: int) -> tuple[int, int] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status_id, last_activity_id FROM wp_state WHERE wp_id = ?", (wp_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def save_wp(self, wp_id: int, status_id: int, last_activity_id: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO wp_state (wp_id, status_id, last_activity_id) VALUES (?, ?, ?) "
                "ON CONFLICT(wp_id) DO UPDATE SET status_id = excluded.status_id, "
                "last_activity_id = excluded.last_activity_id",
                (wp_id, status_id, last_activity_id))

    def mark_processed(self, key: str) -> bool:
        """True si l'événement est nouveau, False s'il a déjà été vu."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO processed_events (key) VALUES (?)", (key,))
        return cur.rowcount == 1

    def incr(self, key: str, delta: float = 1) -> float:
        """Incrément atomique d'un compteur (exécutions, cycles, coûts). Renvoie la nouvelle valeur."""
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            value = (float(row[0]) if row else 0.0) + delta
            self._db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, repr(value)))
        return value

    def number(self, key: str) -> float:
        value = self.get(key)
        return float(value) if value else 0.0

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
