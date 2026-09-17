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
    since_minutes: int = 60, target_filter: str = ""
) -> dict:
    """
    See what's happened recently, optionally filtered to a specific target.

    Call this before starting nontrivial work, especially on an area, entity,
    or app another agent might also be touching. This is the main defense
    against two agents colliding (e.g. one agent reconfiguring an add-on while
    another agent's request is in flight against it).

    Args:
        since_minutes: How far back to look. Default 60.
        target_filter: Optional substring match against the target field,
            e.g. "Pergola" or "media_player.pool_soundbar" or "app:ha_mcp".
    """
    cutoff = _now() - since_minutes * 60
    with _conn() as c:
        if target_filter:
            rows = c.execute(
                "SELECT * FROM activity_log WHERE ts >= ? AND target LIKE ? "
                "ORDER BY ts DESC LIMIT 200",
                (cutoff, f"%{target_filter}%"),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM activity_log WHERE ts >= ? ORDER BY ts DESC LIMIT 200",
                (cutoff,),
            ).fetchall()

    entries = [
        {
            "logged_at": _fmt_ts(r["ts"]),
            "agent": r["agent"],
            "action": r["action"],
            "target": r["target"],
            "notes": r["notes"],
        }
        for r in rows
    ]
    return {"since_minutes": since_minutes, "target_filter": target_filter or None,
            "count": len(entries), "entries": entries}


# ---------------------------------------------------------------------------
# Soft claims
# ---------------------------------------------------------------------------

@mcp.tool
def trtl_claim_area(agent: str, area_or_entity: str, ttl_minutes: int = 20,
                     reason: str = "") -> dict:
    """
    Advisory, expiring claim that you're working on something — a heads-up for
    other agents, not an enforced lock. Nobody is blocked from acting on a
    claimed target; this only makes the claim visible via
    trtl_get_recent_activity / trtl_list_active_claims.

    Call this before starting multi-step work on a specific area, entity, or
    subsystem (e.g. "Pergola", "app:ha_mcp", "Sidebar config").

    Args:
        agent: Who you are, e.g. "claude", "chatgpt".
        area_or_entity: What you're about to work on.
        ttl_minutes: How long the claim should stand before auto-expiring.
            Default 20 — pick something realistic for the task size.
        reason: Brief description of what you're doing.
    """
    now = _now()
    expires = now + ttl_minutes * 60
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM claims WHERE target = ? AND expires_at > ?",
            (area_or_entity, now),
        ).fetchone()
        c.execute(
            "INSERT INTO claims (target, agent, reason, claimed_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(target) DO UPDATE SET agent=excluded.agent, "
            "reason=excluded.reason, claimed_at=excluded.claimed_at, "
            "expires_at=excluded.expires_at",
            (area_or_entity, agent, reason, now, expires),
        )
    result = {
        "target": area_or_entity,
        "claimed_by": agent,
        "expires_at": _fmt_ts(expires),
        "ttl_minutes": ttl_minutes,
    }
    if existing and existing["agent"] != agent:
        result["warning"] = (
            f"This target was already claimed by '{existing['agent']}' "
            f"(reason: {existing['reason']!r}), expiring {_fmt_ts(existing['expires_at'])}. "
            "Your claim has overwritten it. Consider checking with the other "
            "agent/person before proceeding if the work might conflict."
        )
    return result


@mcp.tool
def trtl_release_claim(agent: str, area_or_entity: str) -> dict:
    """
    Release a claim you hold once you're done working on that area/entity.

    Args:
        agent: Who you are.
        area_or_entity: The target to release.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM claims WHERE target = ?", (area_or_entity,)
        ).fetchone()
        if not row:
            return {"released": False, "reason": "No claim found for this target."}
        if row["agent"] != agent:
            return {
                "released": False,
                "reason": f"Claim is held by '{row['agent']}', not '{agent}'. "
                          "Not releasing someone else's claim automatically.",
            }
        c.execute("DELETE FROM claims WHERE target = ?", (area_or_entity,))
    return {"released": True, "target": area_or_entity}


@mcp.tool
def trtl_list_active_claims() -> dict:
    """
    List all currently active (non-expired) claims. Check this alongside
    trtl_get_recent_activity before starting work that might overlap with
    something another agent is mid-task on.
    """
    now = _now()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM claims WHERE expires_at > ? ORDER BY expires_at ASC",
            (now,),
        ).fetchall()
        c.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))
    claims = [
        {
            "target": r["target"],
            "agent": r["agent"],
            "reason": r["reason"],
            "claimed_at": _fmt_ts(r["claimed_at"]),
            "expires_at": _fmt_ts(r["expires_at"]),
        }
        for r in rows
    ]
    return {"count": len(claims), "claims": claims}


# ---------------------------------------------------------------------------
# Open issues
# ---------------------------------------------------------------------------

@mcp.tool
def trtl_add_issue(created_by: str, title: str, details: str = "") -> dict:
    """
    Add a persistent open issue/TODO to the shared build tracker — things like
    known bugs, blocked tasks, or follow-ups (e.g. "Bond pergola requires held
    RF signal, not momentary pulse — needs Bond app check").

    Args:
        created_by: Who you are.
        title: Short summary.
        details: Longer context, current status, next steps.
    """
    issue_id = str(uuid.uuid4())[:8]
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO issues (id, created_at, created_by, title, details, status) "
            "VALUES (?, ?, ?, ?, ?, 'open')",
            (issue_id, now, created_by, title, details),
        )
    return {"id": issue_id, "title": title, "created_at": _fmt_ts(now), "status": "open"}


@mcp.tool
def trtl_list_open_issues() -> dict:
    """
    List all currently open issues on the shared build tracker. Check this at
    the start of a session to see what's already known to be broken, blocked,
    or pending — before re-diagnosing something someone already found.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM issues WHERE status = 'open' ORDER BY created_at ASC"
        ).fetchall()
    issues = [
        {
            "id": r["id"],
            "title": r["title"],
            "details": r["details"],
            "created_by": r["created_by"],
            "created_at": _fmt_ts(r["created_at"]),
        }
        for r in rows
    ]
    return {"count": len(issues), "issues": issues}


@mcp.tool
def trtl_resolve_issue(issue_id: str, resolved_by: str, resolution_notes: str = "") -> dict:
    """
    Mark an open issue as resolved.

    Args:
        issue_id: The id returned by trtl_add_issue or trtl_list_open_issues.
        resolved_by: Who you are.
        resolution_notes: What fixed it / final state.
    """
    now = _now()
    with _conn() as c:
        row = c.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if not row:
            return {"resolved": False, "reason": f"No issue found with id {issue_id!r}."}
        if row["status"] == "resolved":
            return {"resolved": False, "reason": "Issue is already resolved.",
                     "resolved_at": _fmt_ts(row["resolved_at"]) if row["resolved_at"] else None}
        c.execute(
            "UPDATE issues SET status='resolved', resolved_at=?, resolved_by=?, "
            "resolution_notes=? WHERE id=?",
            (now, resolved_by, resolution_notes, issue_id),
        )
    return {"resolved": True, "id": issue_id, "resolved_at": _fmt_ts(now)}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

_init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9584"))
    secret_path = os.environ.get("TRTL_SECRET_PATH", "")
    path = f"/{secret_path}" if secret_path else "/"
    print("=" * 70)
    print(f"TRTL Ops Log MCP server starting on 0.0.0.0:{port}{path}")
    print(f"Database: {DB_PATH}")
    print("=" * 70)
    mcp.run(transport="http", host="0.0.0.0", port=port, path=path, stateless_http=True)
