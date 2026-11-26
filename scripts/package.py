#!/usr/bin/env python3
"""Package a trusted, already-built host binary. No builds, downloads or installation."""
import argparse
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

SOURCE = Path(__file__).resolve().parent.parent
PAYLOAD = ("bin/heeler", "LICENSE", "README.md", "roles/lead/AGENTS.md", "skills/heeler-lead/SKILL.md")


def regular_bytes(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"not a regular non-symlink input: {path.name}")
    return path.read_bytes()


def run_binary(binary, *args):
    # The maintainer explicitly trusts this executable. Bound time/output; do not inherit MCP state hints.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "HEELER_"))}
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run([str(binary), *args], stdin=subprocess.DEVNULL,
                                stdout=output, stderr=errors, env=env, timeout=10, check=False)
        output.seek(0)
        data = output.read(128 * 1024 + 1)
        errors.seek(0)
        diagnostic = errors.read(4097)
    if result.returncode or diagnostic or len(data) > 128 * 1024:
        raise ValueError(f"binary command failed or exceeded output bounds: {' '.join(args)}")
    return data


def safe_name(name):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError("unsafe archive member name")
    return name


def build(binary, host, output_dir):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", host):
        raise ValueError("host must be a simple host label (not a path)")
    # Inspect before resolve so an explicitly supplied symlink is refused.
    binary_bytes = regular_bytes(binary)
    binary = binary.resolve(strict=True)
    if not os.access(binary, os.X_OK):
        raise ValueError("binary must be executable")
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ValueError("output directory must already exist and not be a symlink")
    version_output = run_binary(binary, "--version").decode("utf-8")
    match = re.fullmatch(r"heeler ([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?)\n", version_output)
    if not match:
        raise ValueError("unexpected binary version")
    top = f"heeler-{match[1]}-{host}"
    archive_path = output_dir / f"{top}.tar.gz"
    digest_path = output_dir / f"{top}.tar.gz.sha256"
    if os.path.lexists(archive_path) or os.path.lexists(digest_path):
        raise ValueError("artifact or digest already exists; refusing overwrite")
    payload = {
        "bin/heeler": binary_bytes,
        "LICENSE": regular_bytes(SOURCE / "LICENSE"),
        "README.md": regular_bytes(SOURCE / "README.md"),
        "roles/lead/AGENTS.md": run_binary(binary, "export", "lead"),
        "skills/heeler-lead/SKILL.md": run_binary(binary, "export", "skill"),
    }
    if tuple(payload) != PAYLOAD or len(set(payload)) != len(PAYLOAD):
        raise ValueError("archive allowlist mismatch")
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in payload.items())
    payload["SHA256SUMS"] = manifest.encode("utf-8")
    # Public names can change owners after creation. Never roll them back, even on failure.
    # Only the private temporary handle is cleaned up; publication is not an atomic pair.
    with tempfile.TemporaryFile() as temporary:
        with tarfile.open(fileobj=temporary, mode="w:gz") as archive:
            for name, data in payload.items():
                info = tarfile.TarInfo(safe_name(f"{top}/{name}"))
                info.size = len(data)
                info.mode = 0o755 if name == "bin/heeler" else 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
        temporary.seek(0)
        with archive_path.open("xb") as output:
            shutil.copyfileobj(temporary, output)
        digest = hashlib.sha256()
        temporary.seek(0)
        for chunk in iter(lambda: temporary.read(1024 * 1024), b""):
            digest.update(chunk)
        with digest_path.open("x", encoding="utf-8") as output:
            output.write(f"{digest.hexdigest()}  {archive_path.name}\n")
    return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True, help="trusted already-built native executable")
    parser.add_argument("--host", required=True, help="maintainer-supplied host label; not verified target identity")
    parser.add_argument("--output-dir", type=Path, required=True, help="existing directory; outputs never overwritten")
    args = parser.parse_args()
    try:
        print(build(args.binary, args.host, args.output_dir))
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"packaging failed: {error}\n"
                    "Public archive/digest outputs may be partial or complete; no rollback was attempted. "
                    "Inspect them manually before recovery; do not retry automatically.\n")


if __name__ == "__main__":
    main()
