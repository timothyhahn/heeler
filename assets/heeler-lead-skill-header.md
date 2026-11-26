---
name: heeler-lead
description: Coordinate local herdr workstreams as lead only on explicit human request, never merely on discovery.
disable-model-invocation: true
---

# Explicit selection required

Adopt this role only on an explicit human request to use the Heeler lead skill.
Discovery or connection to Heeler MCP is not authorization to become the lead.
The MCP tools must already be configured; do not install, reconfigure, approve
permissions or change trust implicitly. Workers do not adopt this role by default.

`disable-model-invocation` is a Pi-specific manual-invocation gate, not a portable
Agent Skills guarantee. In other harnesses, use a supported manual-only loading
mode or do not register this skill. The canonical instructions follow in full.
