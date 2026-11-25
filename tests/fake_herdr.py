"""Stateful local CLI double. Accepts only the native operations used by Heeler."""
import fcntl
import json
from pathlib import Path
import socket
import sys
from typing import NoReturn

root = Path(__file__).parent
native_lock = (root / "native.lock").open("a")
fcntl.flock(native_lock, fcntl.LOCK_EX)
state_path = root / "native.json"
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args.copy())


def save():
    state_path.write_text(json.dumps(state))


def reply(kind, **fields):
    save()
    print(json.dumps({"id": "fake", "result": {"type": kind, **fields}}))
    sys.exit(0)


def fail(message) -> NoReturn:
    save()
    print(message, file=sys.stderr)
    sys.exit(1)


def dispatch_gate(command):
    gate = state.get("dispatch_gate")
    if gate is None or gate["command"] != command:
        return
    if gate.get("skip", 0):
        gate["skip"] -= 1
        return
    del state["dispatch_gate"]
    save()
    path = root / gate["socket"]
    assert path.is_relative_to(root)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(4)
        connection.connect(str(path))
        connection.sendall(b"x")
        assert connection.recv(1) == b"g", "dispatch gate was not released"


def options(items):
    result = {}
    items = iter(items)
    for item in items:
        if item == "--no-focus":
            result["no-focus"] = True
        elif item in {"--label", "--workspace", "--pane", "--direction", "--cwd",
                      "--kind", "--timeout", "--source", "--lines", "--format"}:
            value = next(items, None)
            if value is None:
                fail(f"Missing value for {item}")
            result[item[2:]] = value
        else:
            # Legacy herdr leaves reject --flag=value and bare -- terminators.
            fail(f"Unexpected CLI argument: {item}")
    return result


if args == ["session", "list", "--json"]:
    save()
    print(json.dumps({"sessions": [{"name": "default", "default": True, "running": True}]}))
    sys.exit(0)
if not args or args.pop(0) != "--session=default":
    fail("Only the explicit default local session exists")
if args == ["api", "snapshot"]:
    reply("session_snapshot", snapshot=state["snapshot"])

snapshot = state["snapshot"]
area = args[0] if args else None
command = args[1] if len(args) > 1 else None
if (area, command) in [("workspace", "rename"), ("tab", "rename")]:
    assert area is not None
    if len(args) != 4:
        fail("Rename requires exactly one ID and one literal label argument")
    native_id, label = args[2:]
    if not native_id.startswith("w") or label.startswith("-"):
        fail("Unexpected rename option or ID")
    if state.pop("fail_rename", False):
        fail("native rename denied")
    if state.pop("rename_api_error", False):
        save()
        print(json.dumps({"error": "native rename API denied"}))
        sys.exit(0)
    item = next((item for item in snapshot[area + "s"] if item[area + "_id"] == native_id), None)
    if item is None:
        fail(f"{area} not found")
    item["label"] = label
    reply(area + "_info", **{area: item})

if (area, command) in [("workspace", "create"), ("tab", "create"), ("pane", "split")]:
    opts = options(args[2:])
    source = None
    workspace = None
    assert opts["no-focus"] is True and opts["cwd"].startswith("/")
    if area == "workspace":
        workspace_id = f"w{len(snapshot['workspaces']) + 1}"
        workspace = {"workspace_id": workspace_id, "number": len(snapshot["workspaces"]) + 1,
                     "label": opts["label"], "focused": False, "pane_count": 1, "tab_count": 1,
                     "active_tab_id": f"{workspace_id}:t1", "agent_status": "idle"}
        snapshot["workspaces"].append(workspace)
    elif area == "tab":
        workspace_id = opts["workspace"]
        assert any(w["workspace_id"] == workspace_id for w in snapshot["workspaces"])
    else:
        source = next(p for p in snapshot["panes"] if p["pane_id"] == opts["pane"])
        workspace_id = source["workspace_id"]
        assert opts["direction"] in ["right", "down"]
    number = 1 + sum(p["workspace_id"] == workspace_id for p in snapshot["panes"])
    tab_number = 1 + sum(t["workspace_id"] == workspace_id for t in snapshot["tabs"]) if area == "tab" else 1
    if area == "pane":
        assert source is not None
        tab_id = source["tab_id"]
    else:
        tab_id = f"{workspace_id}:t{tab_number}"
    tab = {"tab_id": tab_id, "workspace_id": workspace_id,
           "number": tab_number, "label": opts.get("label", "Shell"), "focused": False,
           "pane_count": 1, "agent_status": "idle"}
    pane = {"pane_id": f"{workspace_id}:p{number}", "terminal_id": f"term_{workspace_id}_{number}",
            "workspace_id": workspace_id, "tab_id": tab["tab_id"], "focused": False,
            "agent_status": "idle", "revision": 1, "cwd": opts["cwd"], "label": None}
    snapshot["panes"].append(pane)
    if area != "pane":
        snapshot["tabs"].append(tab)
    if area == "workspace":
        assert workspace is not None
        reply("workspace_created", workspace=workspace, tab=tab, root_pane=pane)
    if area == "tab":
        reply("tab_created", tab=tab, root_pane=pane)
    reply("pane_info", pane=pane)

if area != "agent":
    fail("Unsupported native operation")
if command == "start":
    name = args[2]
    boundary = args.index("--")
    opts = options(args[3:boundary])
    pane = next(p for p in snapshot["panes"] if p["pane_id"] == opts["pane"])
    if any(a["pane_id"] == pane["pane_id"] for a in snapshot["agents"]):
        fail("agent already running")
    agent = {**pane, "name": name, "agent": opts["kind"],
             "agent_status": "unknown" if opts["kind"] == "codex" else "idle",
             "interactive_ready": True, "launch_pending": False,
             "agent_session": {"source": "herdr:" + opts["kind"], "agent": opts["kind"],
                               "kind": "id", "value": "session-" + name}}
    snapshot["agents"].append(agent)
    reply("agent_started", agent=agent, argv=[opts["kind"], *args[boundary + 1:]])

pane_id = args[2]
assert pane_id.startswith("w"), "agent target must be the first operand, without a -- marker"
if command in ["get", "focus"]:
    assert len(args) == 3
opts = options(args[3:]) if command in ["wait", "read"] else {}
agent = next((a for a in snapshot["agents"] if a["pane_id"] == pane_id), None)
if agent is None:
    fail("agent not found")
if command == "get":
    dispatch_gate("get")
    overrides = state.get("get_overrides", [])
    reply("agent_info", agent={**agent, **(overrides.pop(0) if overrides else {})})
if command == "prompt":
    assert len(args) == 4
    text = args[3]
    # Inspect persisted notes at the exact moment terminal submission happens.
    notes = json.loads((root / "state" / "workstreams.json").read_text())
    assignment_id = text.splitlines()[0].split(": ", 1)[1]
    attempts = [(r, a) for r in notes["records"] for a in r["workstream"].get("assignments", [])
                if a["id"] == assignment_id and a["target"]["pane_id"] == pane_id and a["delivery"] == "uncertain"]
    assert attempts, "handoff must be recorded before input"
    state.setdefault("prompts", []).append({
        "pane_id": pane_id, "text": text, "report_path": attempts[0][1]["report_path"],
    })
    dispatch_gate("prompt")
    if state.pop("fail_prompt", False):
        fail("connection lost after possible submission")
    if state.pop("conflict_after_prompt", False):
        # A competing notes writer wins after the input is acknowledged.
        with (root / "state" / "workstreams.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = attempts[0][0]
            record["revision"] += 1
            record["workstream"]["next_action"] = "Competing edit"
            (root / "state" / "workstreams.json").write_text(json.dumps(notes))
    reply("agent_prompted", agent=agent)
if command == "wait":
    assert 0 < int(opts["timeout"]) <= 60000
    if state.pop("wait_stays_idle", False):
        reply("agent_info", agent=agent)
    agent["agent_status"] = "done"
    prompts = [p for p in state.get("prompts", []) if p["pane_id"] == pane_id]
    if prompts:
        report = Path(prompts[-1]["report_path"])
        assert report.is_relative_to(root), "fake agents only write inside the temporary test root"
        report.write_text("# Synthetic agent report\nNo real code was changed. Checks and agent activity are simulated.\n")
    reply("agent_info", agent=agent)
if command == "read":
    assert opts["format"] == "text" and opts["source"] in ["visible", "recent-unwrapped"]
    assert 0 < int(opts["lines"]) <= 500
    save()
    print("Report written. Review the report and checks before accepting the work.")
    sys.exit(0)
if command == "focus":
    snapshot["focused_pane_id"] = pane_id
    reply("agent_info", agent=agent)
fail("Unsupported agent operation")
