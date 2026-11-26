"""Stateful local CLI double. Accepts only the native operations used by Heeler."""
import fcntl
import json
from pathlib import Path
import socket
import sys
import time
from typing import Any, NoReturn

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
    request_id = "cli:agent:prompt" if kind == "agent_prompted" else "fake"
    print(json.dumps({"id": request_id, "result": {"type": kind, **fields}}))
    sys.exit(0)


def fail(message) -> NoReturn:
    save()
    print(message, file=sys.stderr)
    sys.exit(1)


def usage_error(message) -> NoReturn:
    save()
    print("error: " + message, file=sys.stderr)
    sys.exit(2)


def prompt_error(code, message) -> NoReturn:
    save()
    print(json.dumps({"id": "cli:agent:prompt", "error": {"code": code, "message": message}}), file=sys.stderr)
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


def outer_globals(raw):
    # Native 5bd900d main.rs:527–542 runs session.rs:59–84 and
    # remote/args.rs:48–119 BEFORE leaf verbatim text protection.
    route: dict[str, Any] = {"session": None}
    state.setdefault("routing", []).append(route)
    cleaned = []
    index = 0
    while index < len(raw):
        arg = raw[index]
        if arg == "--":
            cleaned.extend(raw[index:])
            break
        if arg == "--session":
            if index + 1 == len(raw):
                usage_error("missing value for --session")
            route["session"] = raw[index + 1]
            index += 2
            continue
        if arg.startswith("--session="):
            route["session"] = arg[len("--session="):]
            index += 1
            continue
        cleaned.append(arg)
        index += 1
    route["after_session"] = cleaned.copy()
    name = route["session"]
    if name is not None and (not name or len(name.encode()) > 64 or name in {".", ".."}
                             or not all(c.isascii() and (c.isalnum() or c in ".-_") for c in name)):
        usage_error("invalid session name")
    remote = None
    keybindings = "local"
    keybindings_seen = False
    handoff = False
    result = []
    index = 0
    while index < len(cleaned):
        arg = cleaned[index]
        if arg == "--":
            result.extend(cleaned[index:])
            break
        if arg == "--handoff":
            handoff = True
            index += 1
            continue
        if arg == "--remote" or arg.startswith("--remote="):
            if remote is not None:
                usage_error("--remote can only be specified once")
            if arg == "--remote":
                if index + 1 == len(cleaned):
                    usage_error("missing value for --remote")
                value = cleaned[index + 1]
                index += 2
            else:
                value = arg[len("--remote="):]
                index += 1
            if not value or value.startswith("-"):
                usage_error("invalid --remote target")
            remote = value
            continue
        if arg == "--remote-keybindings" or arg.startswith("--remote-keybindings="):
            if keybindings_seen:
                usage_error("--remote-keybindings can only be specified once")
            if arg == "--remote-keybindings":
                if index + 1 == len(cleaned):
                    usage_error("missing value for --remote-keybindings")
                value = cleaned[index + 1]
                index += 2
            else:
                value = arg[len("--remote-keybindings="):]
                index += 1
            if value not in {"local", "server"}:
                usage_error("--remote-keybindings must be 'local' or 'server'")
            keybindings = value
            keybindings_seen = True
            continue
        result.append(arg)
        index += 1
    if remote is None and keybindings_seen:
        usage_error("--remote-keybindings requires --remote")
    if remote is None and handoff:
        result.append("--handoff")
    route.update(after_remote=result.copy(), remote=remote, keybindings=keybindings, handoff=handoff)
    if remote is not None:
        # This strict local double never connects; prompt commands are not remote launch.
        usage_error("--remote can only be used with the default launch command")
    return result, name


args, selected_session = outer_globals(args)
if args == ["session", "list", "--json"]:
    save()
    print(json.dumps({"sessions": [{"name": "default", "default": True, "running": True}]}))
    sys.exit(0)
if selected_session != "default":
    fail("Only the explicit default local session exists")
if args == ["api", "snapshot"]:
    reply("session_snapshot", snapshot=state["snapshot"])

snapshot = state["snapshot"]


def public_number(value):
    alphabet = "123456789ABCDEFGHJKMNPQRSTVWXYZ0"
    result = 0
    for char in value:
        result = result * 32 + alphabet.index(char) + 1
    return result


def public_suffix(value):
    alphabet = "123456789ABCDEFGHJKMNPQRSTVWXYZ0"
    result = ""
    while value:
        value, digit = divmod(value - 1, 32)
        result = alphabet[digit] + result
    return result


def move_layout(tab):
    panes = [p for p in snapshot["panes"] if p["tab_id"] == tab["tab_id"]]
    focused = next((p["pane_id"] for p in panes if p.get("focused")), panes[0]["pane_id"])
    area = {"x": 0, "y": 0, "width": 80, "height": 24}
    return {"workspace_id": tab["workspace_id"], "tab_id": tab["tab_id"], "zoomed": state.get("move_zoomed", False),
            "area": area, "focused_pane_id": focused,
            "panes": [{"pane_id": p["pane_id"], "focused": p["pane_id"] == focused,
                       "rect": {**area, "x": i * (80 // len(panes)), "width": 80 // len(panes)}} for i, p in enumerate(panes)],
            "splits": [{"id": "split:0", "direction": "right", "ratio": 0.5, "rect": area}] if len(panes) == 2 else []}


def move_error(error) -> NoReturn:
    save()
    print(json.dumps({"id": "cli:pane:move", "error": error}), file=sys.stderr)
    sys.exit(1)


area = args[0] if args else None
command = args[1] if len(args) > 1 else None
if (area, command) == ("pane", "get"):
    if len(args) != 3:
        usage_error("Pane get requires one target")
    pane_id = state.get("aliases", {}).get(args[2], args[2])
    pane = next((p for p in snapshot["panes"] if p["pane_id"] == pane_id), None)
    if pane is None:
        fail("pane not found")
    dispatch_gate("pane_get")
    overrides = state.get("pane_get_overrides", [])
    reply("pane_info", pane={**pane, **(overrides.pop(0) if overrides else {})})

if (area, command) == ("pane", "move"):
    # Native NewTab destination; default focus is true, not Heeler's policy.
    if len(args) < 4 or args[3] != "--new-tab":
        usage_error("Fake supports only the native new-tab destination")
    opts = options(args[4:])
    if set(opts) - {"workspace", "label", "no-focus"} or "workspace" not in opts or "label" not in opts:
        usage_error("New-tab fixture requires explicit workspace/label")
    focus = not opts.get("no-focus", False)
    pane_id = state.get("aliases", {}).get(args[2], args[2])
    pane = next((p for p in snapshot["panes"] if p["pane_id"] == pane_id), None)
    if pane is None:
        move_error({"code": "pane_not_found", "message": "source pane not found"})
    old_pane, old_workspace, old_tab = pane["pane_id"], pane["workspace_id"], pane["tab_id"]
    source_tab = next(t for t in snapshot["tabs"] if t["tab_id"] == old_tab)
    layout = move_layout(source_tab)
    result = {"changed": False, "reason": "zoomed_tab", "previous_pane_id": old_pane,
              "previous_workspace_id": old_workspace, "previous_tab_id": old_tab, "pane": pane.copy(),
              "source_layout": layout, "target_layout": layout, "focused_pane_id": layout["focused_pane_id"]}
    state.setdefault("moves", []).append({"pane_id": args[2], "workspace_id": opts["workspace"], "label": opts["label"], "focus": focus})
    error = state.pop("move_error_before", None)
    if error is not None:
        move_error(error)
    if not state.get("move_zoomed", False):
        destination = next((w for w in snapshot["workspaces"] if w["workspace_id"] == opts["workspace"]), None)
        if destination is None:
            move_error({"code": "workspace_not_found", "message": "destination workspace not found"})
        counters = state.setdefault("move_numbers", {})
        for workspace in snapshot["workspaces"]:
            wid = workspace["workspace_id"]
            if wid not in counters:
                counters[wid] = {"tab": 1 + max(t["number"] for t in snapshot["tabs"] if t["workspace_id"] == wid),
                                 "pane": 1 + max(public_number(p["pane_id"].split(":p")[1]) for p in snapshot["panes"] if p["workspace_id"] == wid)}
        cross = old_workspace != destination["workspace_id"]
        new_pane = old_pane
        if cross:
            new_pane = destination["workspace_id"] + ":p" + public_suffix(counters[destination["workspace_id"]]["pane"])
            counters[destination["workspace_id"]]["pane"] += 1
            aliases = state.setdefault("aliases", {})
            for alias, current in list(aliases.items()):
                if current == old_pane:
                    aliases[alias] = new_pane
            aliases[old_pane] = new_pane
            aliases.pop(new_pane, None)
        number = counters[destination["workspace_id"]]["tab"]
        counters[destination["workspace_id"]]["tab"] += 1
        new_tab_id = destination["workspace_id"] + ":t" + public_suffix(number)
        pane.update(pane_id=new_pane, workspace_id=destination["workspace_id"], tab_id=new_tab_id, focused=False)
        for agent in snapshot["agents"]:
            if agent["pane_id"] == old_pane:
                agent.update(pane_id=new_pane, workspace_id=destination["workspace_id"], tab_id=new_tab_id)
        remaining = [p for p in snapshot["panes"] if p["tab_id"] == old_tab]
        if not remaining:
            snapshot["tabs"] = [t for t in snapshot["tabs"] if t["tab_id"] != old_tab]
            snapshot["layouts"] = [l for l in snapshot["layouts"] if l["tab_id"] != old_tab]
            result["closed_tab_id"] = old_tab
        else:
            source_tab["pane_count"] = len(remaining)
        if cross and not any(t["workspace_id"] == old_workspace for t in snapshot["tabs"]):
            snapshot["workspaces"] = [w for w in snapshot["workspaces"] if w["workspace_id"] != old_workspace]
            result["closed_workspace_id"] = old_workspace
        new_tab = {"tab_id": new_tab_id, "workspace_id": destination["workspace_id"], "number": number,
                   "label": opts["label"], "focused": False, "pane_count": 1, "agent_status": pane["agent_status"]}
        snapshot["tabs"].append(new_tab)
        for workspace in snapshot["workspaces"]:
            tabs = [t for t in snapshot["tabs"] if t["workspace_id"] == workspace["workspace_id"]]
            workspace["tab_count"] = len(tabs)
            workspace["pane_count"] = sum(p["workspace_id"] == workspace["workspace_id"] for p in snapshot["panes"])
            if workspace.get("active_tab_id") not in {t["tab_id"] for t in tabs}:
                workspace["active_tab_id"] = tabs[0]["tab_id"]
        if focus or not any(p["pane_id"] == snapshot["focused_pane_id"] and p["tab_id"] == snapshot["focused_tab_id"] for p in snapshot["panes"]):
            selected = pane if focus else next((p for p in snapshot["panes"] if p["workspace_id"] == snapshot["focused_workspace_id"]), snapshot["panes"][0])
            snapshot.update(focused_workspace_id=selected["workspace_id"], focused_tab_id=selected["tab_id"], focused_pane_id=selected["pane_id"])
        for collection, key, focus_key in [("workspaces", "workspace_id", "focused_workspace_id"), ("tabs", "tab_id", "focused_tab_id"), ("panes", "pane_id", "focused_pane_id")]:
            for item in snapshot[collection]:
                item["focused"] = item[key] == snapshot[focus_key]
        for agent in snapshot["agents"]:
            agent["focused"] = agent["pane_id"] == snapshot["focused_pane_id"]
        result.update(changed=True, pane=pane.copy(), created_tab=new_tab.copy(), target_layout=move_layout(new_tab), focused_pane_id=new_pane)
        result.pop("reason")
        if remaining:
            result["source_layout"] = move_layout(source_tab)
        else:
            result.pop("source_layout", None)
    dispatch_gate("move")
    error = state.pop("move_error_after", None)
    if error is not None:
        move_error(error)
    if state.pop("stall_move", False):
        save(); time.sleep(30)
    output = state.pop("move_output", None)
    if output is not None:
        save(); print(output); sys.exit(0)
    result.update(state.pop("move_result_overrides", {}))
    response = state.pop("move_response", None)
    save()
    print(json.dumps(response if response is not None else {"id": "cli:pane:move", "result": {"type": "pane_move", "move_result": result}}))
    sys.exit(0)

if (area, command) in [("workspace", "get"), ("tab", "get")]:
    assert area is not None
    if len(args) != 3:
        fail("Get requires exactly one native ID")
    item = next((item for item in snapshot[area + "s"] if item[area + "_id"] == args[2]), None)
    if item is None:
        fail(f"{area} not found")
    dispatch_gate(area + "_get")
    if area == "workspace":
        item = {**item, "tab_count": sum(t["workspace_id"] == args[2] for t in snapshot["tabs"]),
                "pane_count": sum(p["workspace_id"] == args[2] for p in snapshot["panes"])}
    overrides = state.get(area + "_get_overrides", [])
    reply(area + "_info", **{area: {**item, **(overrides.pop(0) if overrides else {})}})

if (area, command) == ("tab", "close"):
    if len(args) != 3:
        fail("Close requires exactly one native tab ID")
    tab_id = args[2]
    tab = next((t for t in snapshot["tabs"] if t["tab_id"] == tab_id), None)
    if tab is None:
        fail("tab not found")
    if state.pop("close_failure", False):
        fail("native close denied")
    error = state.pop("close_error", None)
    if error is not None:
        save()
        print(json.dumps({"error": error}))
        sys.exit(0)
    workspace_id = tab["workspace_id"]
    workspace = next(w for w in snapshot["workspaces"] if w["workspace_id"] == workspace_id)
    removed_panes = {p["pane_id"] for p in snapshot["panes"] if p["tab_id"] == tab_id}
    snapshot["tabs"] = [t for t in snapshot["tabs"] if t["tab_id"] != tab_id]
    snapshot["panes"] = [p for p in snapshot["panes"] if p["pane_id"] not in removed_panes]
    snapshot["agents"] = [a for a in snapshot["agents"] if a["pane_id"] not in removed_panes]
    snapshot["layouts"] = [layout for layout in snapshot["layouts"] if layout["tab_id"] != tab_id]
    remaining = [t for t in snapshot["tabs"] if t["workspace_id"] == workspace_id]
    if remaining:
        workspace["tab_count"] = len(remaining)
        workspace["pane_count"] = sum(p["workspace_id"] == workspace_id for p in snapshot["panes"])
        if workspace["active_tab_id"] == tab_id:
            workspace["active_tab_id"] = remaining[0]["tab_id"]
    else:
        # Native last-tab close removes its workspace. Heeler must preflight/refuse it.
        snapshot["workspaces"] = [w for w in snapshot["workspaces"] if w["workspace_id"] != workspace_id]
    if snapshot["focused_tab_id"] == tab_id:
        next_tab = remaining[0] if remaining else next(iter(snapshot["tabs"]), None)
        snapshot["focused_tab_id"] = next_tab["tab_id"] if next_tab else None
        snapshot["focused_workspace_id"] = next_tab["workspace_id"] if next_tab else None
        next_pane = next((p for p in snapshot["panes"] if next_tab and p["tab_id"] == next_tab["tab_id"]), None)
        snapshot["focused_pane_id"] = next_pane["pane_id"] if next_pane else None
        for t in snapshot["tabs"]:
            t["focused"] = t["tab_id"] == snapshot["focused_tab_id"]
        for p in snapshot["panes"]:
            p["focused"] = p["pane_id"] == snapshot["focused_pane_id"]
    dispatch_gate("tab_close")
    override = state.pop("close_response", None)
    if override is not None:
        save()
        print(json.dumps(override))
        sys.exit(0)
    reply("ok")

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
    override = state.pop("get_response", None)
    if override is not None:
        save()
        print(json.dumps(override))
        sys.exit(0)
    overrides = state.get("get_overrides", [])
    reply("agent_info", agent={**agent, **(overrides.pop(0) if overrides else {})})
if command == "prompt":
    if len(args) != 4:
        usage_error("Prompt requires one target and one literal text value; no modes or extra flags")
    text = args[3]
    # Native 5bd900d: app/api/agents.rs:140–207 permits working/unknown,
    # unlike Heeler's settled policy. This is terminal submission, not a turn queue.
    if not text:
        prompt_error("empty_agent_prompt", "agent prompt must not be empty")
    if agent["agent_status"] == "blocked":
        prompt_error("agent_blocked", "agent is blocked and requires interactive input")
    supported = {"pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline", "omp", "mastracode",
                 "opencode", "copilot", "kimi", "kiro", "droid", "amp", "grok", "hermes", "kilo", "qodercli",
                 "qwen", "letta", "maki", "muse"}
    if agent.get("agent") not in supported or agent.get("launch_pending", False) or not agent.get("foreground_matches", True):
        prompt_error("agent_not_ready", "agent is not ready in the foreground")
    error = state.pop("prompt_error", None)
    if error is not None:
        prompt_error(error["code"], error["message"])
    captured = agent.copy()
    prompt = {"pane_id": pane_id, "text": text}
    # Explicit assignment-test expectation, not an invented native prompt prerequisite.
    notes = None
    attempts = []
    if state.get("expect_saved_assignment", False):
        notes = json.loads((root / "state" / "workstreams.json").read_text())
        assignment_id = text.splitlines()[0].split(": ", 1)[1]
        attempts = [(r, a) for r in notes["records"] for a in r["workstream"].get("assignments", [])
                    if a["id"] == assignment_id and a["target"]["pane_id"] == pane_id and a["delivery"] == "uncertain"]
        assert attempts, "handoff must be recorded before input"
        prompt["report_path"] = attempts[0][1]["report_path"]
    state.setdefault("prompts", []).append(prompt)
    dispatch_gate("prompt")
    if state.pop("fail_prompt", False):
        fail("connection lost after possible submission")
    if state.pop("conflict_after_prompt", False):
        assert state.get("expect_saved_assignment") and notes is not None and attempts, "conflict fixture requires an assignment expectation"
        # A competing notes writer wins after the input is acknowledged.
        with (root / "state" / "workstreams.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = attempts[0][0]
            record["revision"] += 1
            record["workstream"]["next_action"] = "Competing edit"
            (root / "state" / "workstreams.json").write_text(json.dumps(notes))
    if state.pop("stall_prompt", False):
        save()
        time.sleep(30)
    output = state.pop("prompt_output", None)
    if output is not None:
        save()
        sys.stdout.buffer.write(b"\xff" if output == "invalid_utf8" else output.encode())
        sys.exit(0)
    response = state.pop("prompt_response", None)
    if response is not None:
        save()
        print(json.dumps(response))
        sys.exit(0)
    if "advance_prompt_status" in state:
        agent["agent_status"] = state.pop("advance_prompt_status")
    captured.update(state.pop("prompt_agent_overrides", {}))
    reply("agent_prompted", agent=captured)
if command == "wait":
    assert 0 < int(opts["timeout"]) <= 60000
    if state.pop("wait_stays_idle", False):
        reply("agent_info", agent=agent)
    agent["agent_status"] = "done"
    prompts = [p for p in state.get("prompts", []) if p["pane_id"] == pane_id]
    if prompts and "report_path" in prompts[-1]:
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
