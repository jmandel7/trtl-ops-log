"""
TRTL Ops Log — a small shared coordination MCP server for TRTL WRLD.

Purpose: give multiple AI agents (Claude, ChatGPT, others) working against the
same Home Assistant instance a shared, queryable record of what's been done,
what's in progress, and what's still open — so agents don't collide the way
they did before this tool existed (an add-on reconfigured mid-request by one
agent crashed a request from another).

Design principles:
- Soft coordination, not hard locking. Claims are advisory (a TTL'd flag),
  never enforced — no agent is ever blocked from acting. This avoids deadlocks
  and keeps the tool simple, at the cost of relying on agents actually
  checking before they act (which we prompt them to do via tool descriptions).
- Single SQLite file under /data. No separate DB service, fits the local-first
  philosophy already used elsewhere in this build (MQTT over cloud, etc).
- Runs as its own small FastMCP app, structurally identical to how ha-mcp
  itself runs, so it slots into the existing Nabu Casa Webhook Proxy pattern
  with zero new infrastructure concepts for the person maintaining this.
"""

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from fastmcp import FastMCP

DB_PATH = os.environ.get("TRTL_OPS_DB", "/data/trtl_ops.sqlite3")

mcp = FastMCP(
    name="trtl-ops-log",
    instructions=(
        "Shared coordination log for agents (Claude, ChatGPT, etc.) working on "
        "the same Home Assistant instance for the TRTL WRLD project. "
        "Call trtl_get_recent_activity and trtl_list_open_issues before making "
        "any nontrivial change, so you don't collide with another agent's "
        "in-flight work. Log meaningful actions with trtl_log_action. Use "
        "trtl_claim_area before starting multi-step work on a specific area, "
        "entity, or subsystem, and release it with trtl_release_claim when done."
    ),
)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_log (
                id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                agent TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                notes TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS claims (
                target TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                reason TEXT,
                claimed_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS issues (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                created_by TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_at REAL,
                resolved_by TEXT,
                resolution_notes TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_claims_expires ON claims(expires_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status)")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> float:
    return time.time()


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@mcp.tool
def trtl_log_action(agent: str, action: str, target: str = "", notes: str = "") -> dict:
    """
    Record a meaningful action you just took (or are about to take) against
    Home Assistant, so other agents can see it.

    Call this for anything beyond a trivial read: config changes, entity
    renames, automation edits, area reassignments, add-on/app reconfiguration,
    scene creation, dashboard edits, etc. Cheap and append-only — when in
    doubt, log it.

    Args:
        agent: Who you are, e.g. "claude", "chatgpt". Be consistent so activity
            can be filtered by agent later.
        action: Short verb phrase, e.g. "renamed entity", "edited automation",
            "changed add-on config", "created scene".
        target: What it applied to, e.g. "media_player.pool_soundbar",
            "area:Pergola", "app:ha_mcp". Optional but strongly recommended.
        notes: Any context worth preserving, e.g. before/after values, why.
    """
    entry_id = str(uuid.uuid4())
    ts = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO activity_log (id, ts, agent, action, target, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, ts, agent, action, target, notes),
        )
    return {
        "id": entry_id,
        "logged_at": _fmt_ts(ts),
        "agent": agent,
        "action": action,
        "target": target,
    }


@mcp.tool
def trtl_get_recent_activity(
    since_minutes: int = 
