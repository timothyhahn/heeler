#!/usr/bin/env python3
"""Static export/archive/default MCP checks, isolated notes and fake native calls only."""
from contextlib import contextmanager
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("heeler_package", SOURCE / "scripts/package.py")
assert spec is not None and spec.loader is not None
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def check_static(binary, root):
    lead = (SOURCE / "roles/lead/AGENTS.md").read_bytes()
    header = (SOURCE / "assets/heeler-lead-skill-header.md").read_bytes()
    skill = header + b"\n" + lead
    assert b"disable-model-invocation: true" in header and b"Pi-specific" in header
    assert b"explicit human request" in header and b"not authorization" in header
    assert b"../../" not in skill and skill.count(lead) == 1
    poison = root / "poison"
    poison.write_bytes(b"unchanged")
    spy = root / "spy"
    calls = root / "native-calls"
    spy.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(calls)!r}).touch()\nraise SystemExit(99)\n")
    spy.chmod(0o700)
    empty_path = root / "empty-path"
    empty_path.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "HEELER_"))}
    env.update(HOME=str(root / "home"), XDG_DATA_HOME=str(root / "data"),
               HEELER_STATE_DIR=str(poison / "state"), HEELER_HERDR_BIN=str(spy), PATH=str(empty_path))
    for args, expected in [(["export", "lead"], lead), (["export", "skill"], skill),
                           (["--state-dir", str(poison / "override"), "--herdr-bin", str(spy), "export", "lead"], lead)]:
        result = subprocess.run([binary, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                cwd=root, env=env, timeout=5)
        assert result.returncode == 0 and result.stdout == expected and not result.stderr, result
    closed = subprocess.run([binary], stdin=subprocess.DEVNULL, capture_output=True,
                            cwd=root, env=env, timeout=5)
    assert not closed.stdout and len(closed.stderr) < 8192
    for args in [["--help"], ["--version"], ["export", "--help"]]:
        result = subprocess.run([binary, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                cwd=root, env=env, timeout=5)
        assert result.returncode == 0 and 0 < len(result.stdout) < 8192 and not result.stderr, result
        assert lead not in result.stdout
    for args in [["export"], ["export", "bogus"], ["export", "lead", "extra"],
                 ["export", "skill", "--output", "anything"], ["--bogus"],
                 ["install"], ["export", "lead", "--state-dir", "anywhere"],
                 ["export", "invalid-" + "x" * 20000]]:
        result = subprocess.run([binary, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                cwd=root, env=env, timeout=5)
        assert result.returncode != 0 and result.stdout == b"" and 0 < len(result.stderr) < 8192, result
        assert lead not in result.stderr
    # A missing native executable is also irrelevant to static exports.
    env["HEELER_HERDR_BIN"] = str(root / "missing-herdr")
    assert subprocess.run([binary, "export", "lead"], env=env, cwd=root,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=5).stdout == lead
    assert poison.read_bytes() == b"unchanged" and not calls.exists()
    assert not (root / "home").exists() and not (root / "data").exists()
    return lead, skill, env


def expect_failure(function, *args):
    try:
        function(*args)
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    raise AssertionError("expected rejection")


def check_publication_failures(binary, output, archive_name):
    def artifact(host):
        return output / archive_name.replace("-test-host.tar.gz", f"-{host}.tar.gz")

    original_open = Path.open
    sentinel = output / "unrelated-sentinel"
    sentinel.write_bytes(b"KEEP UNRELATED OUTPUT")

    # Real EEXIST after another producer replaces the archive namespace entry.
    raced = artifact("replacement-host")
    raced_digest = raced.with_name(raced.name + ".sha256")
    moved = output / "moved-original-archive"
    foreign_archive = b"OTHER PRODUCER ARCHIVE - PRESERVE EXACTLY"
    foreign_digest = b"OTHER PRODUCER DIGEST - PRESERVE EXACTLY"
    owned_bytes = []
    def replace_before_digest(path, *args, **kwargs):
        if path == raced_digest and args and args[0] == "x":
            owned_bytes.append(raced.read_bytes())
            raced.rename(moved)
            with original_open(raced, "xb") as stream:
                stream.write(foreign_archive)
            with original_open(raced_digest, "xb") as stream:
                stream.write(foreign_digest)
        return original_open(path, *args, **kwargs)
    try:
        with patch.object(Path, "open", replace_before_digest):
            package.build(Path(binary), "replacement-host", output)
    except FileExistsError as error:
        assert error.errno == errno.EEXIST
    else:
        raise AssertionError("expected real exclusive digest EEXIST")
    assert len(owned_bytes) == 1 and owned_bytes[0]
    assert raced.read_bytes() == foreign_archive and raced_digest.read_bytes() == foreign_digest
    assert moved.read_bytes() == owned_bytes[0]

    # Digest-open failure retains the completed archive; there is no rollback or retry.
    failed = artifact("digest-open-host")
    failed_digest = failed.with_name(failed.name + ".sha256")
    def fail_digest(path, *args, **kwargs):
        if path == failed_digest:
            raise OSError(errno.ENOSPC, "injected digest-open failure")
        return original_open(path, *args, **kwargs)
    with patch.object(Path, "open", fail_digest):
        expect_failure(package.build, Path(binary), "digest-open-host", output)
    assert failed.is_file() and not failed_digest.exists()

    # An actual created digest with a partial write must also remain for manual inspection.
    partial = artifact("partial-digest-host")
    partial_digest = partial.with_name(partial.name + ".sha256")
    writes = []
    class PartialDigest:
        def __init__(self, stream):
            self.stream = stream
        def write(self, text):
            writes.append(text[:13])
            self.stream.write(writes[-1])
            self.stream.flush()
            raise OSError(errno.ENOSPC, "injected partial digest write")
    @contextmanager
    def partial_digest_writer(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as stream:
            yield PartialDigest(stream)
    def partial_digest_open(path, *args, **kwargs):
        if path == partial_digest and args and args[0] == "x":
            return partial_digest_writer(path, *args, **kwargs)
        return original_open(path, *args, **kwargs)
    with patch.object(Path, "open", partial_digest_open):
        expect_failure(package.build, Path(binary), "partial-digest-host", output)
    assert len(writes) == 1 and partial_digest.read_text() == writes[0]
    assert writes[0] == hashlib.sha256(partial.read_bytes()).hexdigest()[:13]
    partial_bytes = partial.read_bytes()
    expect_failure(package.build, Path(binary), "partial-digest-host", output)
    assert partial.read_bytes() == partial_bytes and partial_digest.read_text() == writes[0]

    # Failure during the public copy leaves only the exact partial archive, not a digest.
    copied = []
    def fail_copy(source, destination):
        copied.append(source.read(23))
        destination.write(copied[-1])
        destination.flush()
        raise OSError(errno.ENOSPC, "injected partial archive copy")
    with patch.object(package.shutil, "copyfileobj", fail_copy):
        expect_failure(package.build, Path(binary), "partial-copy-host", output)
    partial_copy = artifact("partial-copy-host")
    assert len(copied) == 1 and len(copied[0]) == 23 and partial_copy.read_bytes() == copied[0]
    assert not partial_copy.with_name(partial_copy.name + ".sha256").exists()

    # Real CLI invocation: failed copy is nonzero/stderr-only and explicitly warns about leftovers.
    injection = """
import errno, runpy, shutil, sys

def fail_copy(source, destination):
    destination.write(b'CLI PARTIAL ARCHIVE')
    destination.flush()
    raise OSError(errno.ENOSPC, 'injected CLI copy failure')

shutil.copyfileobj = fail_copy
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    command = [sys.executable, "-c", injection, str(SOURCE / "scripts/package.py"),
               "--binary", binary, "--host", "cli-failure-host", "--output-dir", str(output)]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    assert result.returncode == 1 and not result.stdout and b"Traceback" not in result.stderr
    assert b"packaging failed:" in result.stderr and b"injected CLI copy failure" in result.stderr
    assert b"partial or complete" in result.stderr and b"Inspect them manually" in result.stderr
    assert b"do not retry automatically" in result.stderr
    cli_partial = artifact("cli-failure-host")
    assert cli_partial.read_bytes() == b"CLI PARTIAL ARCHIVE"
    assert not cli_partial.with_name(cli_partial.name + ".sha256").exists()
    assert sentinel.read_bytes() == b"KEEP UNRELATED OUTPUT"
    assert raced.read_bytes() == foreign_archive and raced_digest.read_bytes() == foreign_digest
    assert moved.read_bytes() == owned_bytes[0]


def check_archive(binary, root, lead, skill):
    output = root / "artifacts"
    output.mkdir()
    for host in ["../escape", "/absolute", "has/slash", "bad\\name", "", ".", "two words"]:
        expect_failure(package.build, Path(binary), host, output)
    for name in ["../escape", "/absolute", "a/../b", "a/./b", "a//b", "a\\b"]:
        expect_failure(package.safe_name, name)
    linked = root / "linked"
    linked.symlink_to(binary)
    expect_failure(package.build, linked, "test-host", output)
    expect_failure(package.regular_bytes, output)
    directory_link = root / "output-link"
    directory_link.symlink_to(output, target_is_directory=True)
    expect_failure(package.build, Path(binary), "test-host", directory_link)
    assert not list(output.iterdir())
    archive = package.build(Path(binary), "test-host", output)
    archive_before = archive.read_bytes()
    external_digest = archive.with_name(archive.name + ".sha256")
    assert external_digest.read_text() == f"{hashlib.sha256(archive_before).hexdigest()}  {archive.name}\n"
    expect_failure(package.build, Path(binary), "test-host", output)
    assert archive.read_bytes() == archive_before
    with tarfile.open(archive) as opened:
        members = opened.getmembers()
        top = archive.name.removesuffix(".tar.gz")
        expected = [f"{top}/{name}" for name in (*package.PAYLOAD, "SHA256SUMS")]
        assert [m.name for m in members] == expected and len(set(expected)) == len(expected)
        data = {}
        for member in members:
            assert member.isfile() and not member.issym() and not member.islnk()
            relative = member.name[len(top) + 1:]
            assert member.mode == (0o755 if relative == "bin/heeler" else 0o644)
            assert member.uid == member.gid == 0 and member.mtime == 0
            content = opened.extractfile(member)
            assert content is not None
            data[relative] = content.read()
        manifest = data.pop("SHA256SUMS").decode()
        assert manifest == "".join(f"{hashlib.sha256(value).hexdigest()}  {name}\n" for name, value in data.items())
        assert data["roles/lead/AGENTS.md"] == lead and data["skills/heeler-lead/SKILL.md"] == skill
        assert data["bin/heeler"] == Path(binary).read_bytes()
        extracted = root / "extracted"
        extracted.mkdir()
        # Extract only after the explicit member/type/path checks above; no real install location.
        for member in members:
            path = extracted / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = opened.extractfile(member)
            assert content is not None
            path.write_bytes(content.read())
            path.chmod(member.mode)
    copied = root / "standalone-skill" / "SKILL.md"
    copied.parent.mkdir()
    shutil.copyfile(extracted / top / "skills/heeler-lead/SKILL.md", copied)
    assert copied.read_bytes() == skill and list(copied.parent.iterdir()) == [copied]

    # A preexisting digest, including a dangling symlink, must never be replaced.
    collision = output / (archive.name.replace("test-host", "collision-host") + ".sha256")
    collision.symlink_to(root / "absent-target")
    expect_failure(package.build, Path(binary), "collision-host", output)
    assert collision.is_symlink() and not (output / collision.name.removesuffix(".sha256")).exists()
    check_publication_failures(binary, output, archive.name)
    assert archive.read_bytes() == archive_before
    assert external_digest.read_text() == f"{hashlib.sha256(archive_before).hexdigest()}  {archive.name}\n"
    with patch.object(package, "run_binary", return_value=b"not heeler\n"):
        expect_failure(package.build, Path(binary), "bad-version", output)
    with patch.object(package, "run_binary", side_effect=subprocess.TimeoutExpired("fake", 10)):
        expect_failure(package.build, Path(binary), "timeout", output)
    assert not list(output.glob("*bad-version*")) and not list(output.glob("*timeout*"))
    failing = root / "failing-binary"
    failing.write_text(f"#!{sys.executable}\nraise SystemExit(7)\n")
    failing.chmod(0o700)
    expect_failure(package.build, failing, "failed-command", output)
    failing.write_text(f"#!{sys.executable}\nprint('x' * (128 * 1024 + 1))\n")
    expect_failure(package.run_binary, failing, "export", "lead")
    assert not list(output.glob("*failed-command*"))
    # Exercise the maintainer CLI too, including clean collision diagnostics.
    cli_output = root / "cli-artifacts"
    cli_output.mkdir()
    command = [sys.executable, str(SOURCE / "scripts/package.py"), "--binary", binary,
               "--host", "cli-host", "--output-dir", str(cli_output)]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    assert result.returncode == 0 and not result.stderr and len(list(cli_output.iterdir())) == 2
    collision_result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    assert collision_result.returncode != 0 and not collision_result.stdout
    assert b"refusing overwrite" in collision_result.stderr and b"Traceback" not in collision_result.stderr
    assert b"Inspect them manually" in collision_result.stderr and len(list(cli_output.iterdir())) == 2
    return extracted / top / "bin/heeler"


def check_default_mcp(binary, root, env):
    from stdio_smoke import Client, structured
    root.mkdir()
    fake = root / "herdr"
    fake.write_text(f"#!{sys.executable}\n" + (SOURCE / "tests/fake_herdr.py").read_text())
    fake.chmod(0o700)
    fixture = json.loads((SOURCE / "tests/fixtures/herdr-0.9.5-snapshot.json").read_text())
    (root / "native.json").write_text(json.dumps({"snapshot": fixture["result"]["snapshot"]}))
    state = root / "env-state"
    env.update(HEELER_STATE_DIR=str(state), HEELER_HERDR_BIN=str(fake))
    client = Client(str(binary), root, extra_env=env, args=[], cwd=root)
    try:
        instructions = client.initialized["instructions"]
        assert "does not authorize" in instructions and "export lead" in instructions
        assert "Read the project's" not in instructions and "# You are the lead" not in instructions
        assert len(client.call("tools/list", {})["tools"]) == 21
        assert structured(client.tool("workstream_list"))["workstreams"] == []
        assert structured(client.tool("session_snapshot", {"session": "default"}))["snapshot"]["version"] == "0.9.5"
    finally:
        client.close()
    assert state.is_dir() and not (root / "state").exists()
    # Explicit legacy root flags must still override the poisoned environment.
    env.update(HEELER_STATE_DIR=str(root / "never-state"), HEELER_HERDR_BIN=str(root / "absent"))
    client = Client(str(binary), root, extra_env=env, cwd=root)
    try:
        assert structured(client.tool("workstream_list"))["workstreams"] == []
        assert structured(client.tool("session_snapshot", {"session": "default"}))["snapshot"]["version"] == "0.9.5"
    finally:
        client.close()
    assert (root / "state").is_dir() and not (root / "never-state").exists()


def check_packaging(binary):
    binary = str(Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix="heeler-packaging-") as directory:
        root = Path(directory)
        lead, skill, env = check_static(binary, root)
        extracted_binary = check_archive(binary, root, lead, skill)
        cwd = root / "unrelated-empty-cwd"
        cwd.mkdir()
        for kind, expected in [("lead", lead), ("skill", skill)]:
            result = subprocess.run([str(extracted_binary), "export", kind], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            assert result.returncode == 0 and result.stdout == expected and not result.stderr
        assert not list(cwd.iterdir())
        check_default_mcp(extracted_binary, root / "isolated-mcp", env)
    print("packaging smoke: exact static exports/no-call/errors, archive allowlist/digests/modes/rejections, real EEXIST replacement preservation, partial-output/CLI failures, standalone skill, extracted absolute binary and default MCP/env precedence passed")


if __name__ == "__main__":
    check_packaging(sys.argv[1])
