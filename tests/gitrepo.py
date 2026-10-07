"""A throwaway git repository for tests that need real `git diff` output.

Hand-written patches only prove the parser agrees with whoever wrote them.
These tests make the same patch a session makes, with the same command, so
a format detail the parser gets wrong fails here rather than on a live fix.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class Repo:
    """One repository under a test's tmp_path, isolated from the machine's
    git config: a global setting such as commit signing or `diff.noprefix`
    would otherwise change what the tests see."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        self.git("init", "-q")

    def git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", *args], cwd=self.root, env=self.env, check=True, capture_output=True
        ).stdout

    def write(self, path: str, data: bytes | str, *, executable: bool = False) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        target.chmod(0o755 if executable else 0o644)

    def read(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def remove(self, path: str) -> None:
        (self.root / path).unlink()

    def move(self, old: str, new: str) -> None:
        target = self.root / new
        target.parent.mkdir(parents=True, exist_ok=True)
        (self.root / old).rename(target)

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        return self.git("rev-parse", "HEAD").decode().strip()

    def blob(self, rev: str, path: str) -> bytes:
        return self.git("cat-file", "blob", f"{rev}:{path}")

    def tree(self, rev: str) -> dict[str, tuple[str, str, int]]:
        """Every blob entry at `rev`: path -> (mode, sha, size)."""
        out = {}
        for line in self.git("ls-tree", "-r", "-l", "-z", rev).split(b"\0"):
            if not line:
                continue
            meta, path = line.split(b"\t", 1)
            mode, _kind, sha, size = meta.split()
            out[path.decode()] = (mode.decode(), sha.decode(), int(size))
        return out

    def patch(self, base: str, *, binary: bool = True, irreversible_delete: bool = True) -> bytes:
        """The patch the fix phase sends: every untracked file added with
        intent-to-add so it appears, then the diff against `base`."""
        self.git("add", "-N", ".")
        args = ["diff"]
        if binary:
            args.append("--binary")
        if irreversible_delete:
            args.append("-D")
        return self.git(*args, base)
