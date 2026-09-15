# TRTL Ops Log

A small shared coordination MCP server for TRTL WRLD, so multiple AI agents
(Claude, ChatGPT, whoever else) working against the same Home Assistant
instance can see what's already been done, what's in progress, and what's
still open — instead of colliding, which has already happened once in this
build (one agent reconfiguring an app mid-request crashed another agent's
in-flight call).

It's built the same way as `ha-mcp` itself: a FastMCP server, running as its
own HA App, generating a persistent secret path on first boot, meant to sit
behind the same kind of Nabu Casa Webhook Proxy pattern already working for
ChatGPT.

## What it gives agents

Eight tools:

- `trtl_log_action` — record a meaningful action (config change, rename,
  automation edit, etc.)
- `trtl_get_recent_activity` — see what's happened recently, optionally
  filtered to a target
- `trtl_claim_area` — advisory, expiring "heads up, I'm working on this"
  flag (not an enforced lock — nothing is ever blocked)
- `trtl_release_claim` — release a claim you hold
- `trtl_list_active_claims` — see all current claims
- `trtl_add_issue` / `trtl_list_open_issues` / `trtl_resolve_issue` — a
  lightweight persistent TODO list, queryable by any connected agent

Storage is
