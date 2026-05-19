"""候选去重（SQLite 持久化）。

跨 cron / watcher 调用持久化已发现 (platform, note_id)，避免重复写入飞书候选清单。
TTL 由 discover.yaml.limits.dedupe_window_days 控制（默认 30 天）。

新增于 2026-05-11。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.utils.logger import logger

DB_PATH = Path("/Users/zhaoliang/Documents/GitHub/AI-Media2Doc/data/discover/dedupe.db")


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovered (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            discovered_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_at ON discovered(discovered_at)")
    return conn


def _key(platform: str, note_id: str) -> str:
    return f"{platform}:{note_id}"


def is_duplicate(platform: str, note_id: str, ttl_days: int = 30) -> bool:
    """(platform, note_id) 在 TTL 内是否已发现过。"""
    try:
        with _get_conn() as conn:
            cutoff = datetime.now() - timedelta(days=ttl_days)
            row = conn.execute(
                "SELECT 1 FROM discovered WHERE id = ? AND discovered_at >= ? LIMIT 1",
                (_key(platform, note_id), cutoff.isoformat()),
            ).fetchone()
            return row is not None
    except sqlite3.Error as e:
        logger.warning(f"[dedupe.is_duplicate] {e}")
        return False


def mark_seen(platform: str, note_id: str, status: str = "candidate"):
    """标记已发现。status: candidate / auto_rewrite / drop"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO discovered (id, platform, discovered_at, status) VALUES (?, ?, ?, ?)",
                (_key(platform, note_id), platform, datetime.now().isoformat(), status),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"[dedupe.mark_seen] {e}")


def cleanup_old(ttl_days: int = 30) -> int:
    """删除超过 TTL 的记录，返回删除行数。"""
    try:
        with _get_conn() as conn:
            cutoff = datetime.now() - timedelta(days=ttl_days)
            cur = conn.execute("DELETE FROM discovered WHERE discovered_at < ?", (cutoff.isoformat(),))
            conn.commit()
            return cur.rowcount
    except sqlite3.Error as e:
        logger.warning(f"[dedupe.cleanup_old] {e}")
        return 0


def stats() -> dict:
    """统计：总条数 / 各平台 / 各 status"""
    try:
        with _get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM discovered").fetchone()[0]
            by_platform = dict(conn.execute("SELECT platform, COUNT(*) FROM discovered GROUP BY platform").fetchall())
            by_status = dict(conn.execute("SELECT status, COUNT(*) FROM discovered GROUP BY status").fetchall())
            return {"total": total, "by_platform": by_platform, "by_status": by_status}
    except sqlite3.Error:
        return {"total": 0, "by_platform": {}, "by_status": {}}
