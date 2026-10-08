"""Parsing and applying the patch a fix phase sends, against real git output."""

import os

import pytest

from receiver import patch
from receiver.patch import PatchError, apply_change, blob_sha, parse_patch
from tests.gitrepo import Repo

LIMIT = 48 * 1024 * 1024


def lines(count: int, *, changed: dict[int, str] | None = None) -> str:
    changed = changed or {}
    return "".join(changed.get(i, f"line {i}") + "\n" for i in range(count))


def binary(size: int, seed: int = 0) -> bytes:
    """Content git treats as binary, which it decides by finding a NUL
    byte. Random bytes can lack one and arrive as a text hunk instead."""
    return b"\0" + bytes((i * 131 + seed * 7) % 256 for i in range(size - 1))


def parse(data: bytes):
    return parse_patch(data, max_binary_bytes=LIMIT)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


def applied(repo: Repo, base: str, change) -> bytes:
    """What the receiver would write for `change`, given the base blob."""
    old = repo.blob(base, change.old_path) if change.old_path else b""
    return apply_change(change, old)


def only(changes):
    assert len(changes) == 1
    return changes[0]


def test_blob_sha_matches_git(repo):
    repo.write("a.txt", "hello\n")
    base = repo.commit()

    assert blob_sha(b"hello\n") == repo.tree(base)["a.txt"][1]
    assert blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_a_modified_file_applies_to_exactly_the_new_content(repo):
    repo.write("src/cart.py", lines(40))
    base = repo.commit()
    repo.write("src/cart.py", lines(40, changed={3: "total = 0", 30: "return total"}))

    change = only(parse(repo.patch(base)))

    assert change.old_path == change.new_path == "src/cart.py"
    assert change.old_mode == change.new_mode == "100644"
    assert len(change.hunks) == 2
    assert applied(repo, base, change) == repo.read("src/cart.py")


def test_a_new_file_needs_no_base_content(repo):
    repo.write("src/cart.py", "x = 1\n")
    base = repo.commit()
    repo.write("tests/test_cart.py", "def test_total():\n    assert True\n")

    change = only(parse(repo.patch(base)))

    assert change.old_path is None
    assert change.new_path == "tests/test_cart.py"
    assert change.new_mode == "100644"
    assert apply_change(change, b"") == repo.read("tests/test_cart.py")


def test_an_empty_new_file_parses_without_a_body(repo):
    repo.write("src/cart.py", "x = 1\n")
    base = repo.commit()
    repo.write("src/__init__.py", "")

    change = only(parse(repo.patch(base)))

    assert change.new_path == "src/__init__.py"
    assert change.hunks == () and change.binary is None
    assert apply_change(change, b"") == b""


@pytest.mark.parametrize("irreversible", [True, False])
def test_a_deleted_file_names_only_its_old_path(repo, irreversible):
    repo.write("src/legacy.py", lines(10))
    repo.write("src/cart.py", "x = 1\n")
    base = repo.commit()
    repo.remove("src/legacy.py")

    change = only(parse(repo.patch(base, irreversible_delete=irreversible)))

    assert change.old_path == "src/legacy.py"
    assert change.new_path is None
    assert change.old_mode == "100644"
    assert repo.tree(base)["src/legacy.py"][1].startswith(change.old_blob)


def test_a_pure_rename_carries_no_content(repo):
    repo.write("src/old.py", lines(20))
    base = repo.commit()
    repo.move("src/old.py", "src/new.py")

    change = only(parse(repo.patch(base)))

    assert (change.old_path, change.new_path) == ("src/old.py", "src/new.py")
    assert change.new_blob is None
    assert change.hunks == ()


def test_a_rename_with_an_edit_applies_to_the_old_content(repo):
    repo.write("src/old.py", lines(40))
    base = repo.commit()
    repo.move("src/old.py", "src/new.py")
    repo.write("src/new.py", lines(40, changed={5: "edited"}))

    change = only(parse(repo.patch(base)))

    assert (change.old_path, change.new_path) == ("src/old.py", "src/new.py")
    assert applied(repo, base, change) == repo.read("src/new.py")


def test_a_mode_change_names_both_modes(repo):
    repo.write("bin/run.sh", "echo hi\n")
    base = repo.commit()
    (repo.root / "bin/run.sh").chmod(0o755)

    change = only(parse(repo.patch(base)))

    assert (change.old_mode, change.new_mode) == ("100644", "100755")
    assert change.new_blob is None


@pytest.mark.parametrize(
    ("before", "after"),
    [("a\nb", "a\nc"), ("a\nb\n", "a\nb"), ("a\nb", "a\nb\nc\n"), ("", "only\n")],
)
def test_a_missing_final_newline_survives_either_way(repo, before, after):
    repo.write("f.txt", before)
    base = repo.commit()
    repo.write("f.txt", after)

    change = only(parse(repo.patch(base)))

    assert applied(repo, base, change) == after.encode()


def test_carriage_returns_are_content_not_line_breaks(repo):
    repo.write("win.txt", b"one\r\ntwo\r\nthree\rstill three\r\n")
    base = repo.commit()
    repo.write("win.txt", b"one\r\nTWO\r\nthree\rstill three\r\n")

    change = only(parse(repo.patch(base)))

    assert applied(repo, base, change) == repo.read("win.txt")


def test_a_small_change_to_a_large_file_is_a_small_patch(repo):
    big = lines(200_000)
    repo.write("fixtures/demo.json", big)
    base = repo.commit()
    repo.write("fixtures/demo.json", lines(200_000, changed={1000: "x", 150_000: "y"}))

    data = repo.patch(base)
    change = only(parse(data))

    assert len(data) < 2_000 < len(big)
    assert applied(repo, base, change) == repo.read("fixtures/demo.json")


def test_a_new_binary_file_arrives_as_a_literal(repo):
    repo.write("src/cart.py", "x = 1\n")
    base = repo.commit()
    repo.write("assets/icon.bin", bytes(range(256)) * 3)

    change = only(parse(repo.patch(base)))

    assert change.binary is not None
    assert apply_change(change, b"") == repo.read("assets/icon.bin")


def test_a_modified_binary_file_applies_its_delta(repo):
    original = bytes((i * 7) % 251 for i in range(20_000))
    repo.write("assets/blob.bin", original)
    base = repo.commit()
    edited = bytearray(original)
    edited[500:504] = b"\x00\x00\x00\x00"
    repo.write("assets/blob.bin", bytes(edited))

    data = repo.patch(base)
    change = only(parse(data))

    assert b"delta " in data
    assert applied(repo, base, change) == bytes(edited)


def test_quoted_and_spaced_paths_are_unquoted(repo):
    repo.write("docs/old name.md", lines(20))
    base = repo.commit()
    repo.move("docs/old name.md", "docs/café menu.md")
    repo.write("docs/café menu.md", lines(20, changed={2: "edited"}))

    change = only(parse(repo.patch(base)))

    assert change.old_path == "docs/old name.md"
    assert change.new_path == "docs/café menu.md"
    assert applied(repo, base, change) == repo.read("docs/café menu.md")


def test_a_symlink_parses_with_its_mode_so_policy_can_refuse_it(repo):
    repo.write("src/cart.py", "x = 1\n")
    base = repo.commit()
    os.symlink("src/cart.py", repo.root / "link")

    change = only(parse(repo.patch(base)))

    assert change.new_mode == "120000"


def test_several_files_parse_in_the_order_git_printed_them(repo):
    repo.write("a.py", "a\n")
    repo.write("b.py", "b\n")
    base = repo.commit()
    repo.write("a.py", "A\n")
    repo.write("b.py", "B\n")
    repo.write("c.py", "C\n")

    changes = parse(repo.patch(base))

    assert [c.new_path for c in changes] == ["a.py", "b.py", "c.py"]


def test_paths_lists_both_sides_of_a_rename_once_each(repo):
    repo.write("src/old.py", lines(20))
    repo.write("src/keep.py", "x\n")
    base = repo.commit()
    repo.move("src/old.py", "src/new.py")
    repo.write("src/keep.py", "y\n")

    changes = parse(repo.patch(base))

    assert [c.paths for c in changes] == [("src/keep.py",), ("src/old.py", "src/new.py")]


# --- Refusals --------------------------------------------------------------


def test_a_hunk_that_does_not_match_the_base_is_refused(repo):
    repo.write("src/cart.py", lines(40))
    base = repo.commit()
    repo.write("src/cart.py", lines(40, changed={10: "fixed"}))
    change = only(parse(repo.patch(base)))

    with pytest.raises(PatchError, match="src/cart.py.*does not match"):
        apply_change(change, lines(40, changed={9: "drifted"}).encode())


def test_a_base_that_differs_outside_the_hunks_fails_the_blob_check(repo):
    """The hunks apply, but the result is not the file the session had, so
    the receiver refuses rather than shipping something nobody tested."""
    repo.write("src/cart.py", lines(40))
    base = repo.commit()
    repo.write("src/cart.py", lines(40, changed={10: "fixed"}))
    change = only(parse(repo.patch(base)))

    with pytest.raises(PatchError, match="does not reproduce"):
        apply_change(change, (lines(40) + "trailing\n").encode())


def test_a_binary_change_without_binary_is_refused(repo):
    repo.write("assets/icon.bin", bytes(range(256)))
    base = repo.commit()
    repo.write("assets/icon.bin", bytes(range(255, -1, -1)))

    with pytest.raises(PatchError, match="without --binary"):
        parse(repo.patch(base, binary=False))


def test_abbreviated_blob_ids_still_verify(repo):
    repo.write("src/cart.py", lines(10))
    base = repo.commit()
    repo.write("src/cart.py", lines(10, changed={2: "x"}))
    change = only(parse(repo.patch(base)))

    assert len(change.old_blob) < 40
    assert applied(repo, base, change) == repo.read("src/cart.py")
    assert patch.blob_matches(blob_sha(repo.blob(base, "src/cart.py")), change.old_blob)


SIMPLE = (
    b"diff --git a/f.txt b/f.txt\n"
    b"index 7898192..6178079 100644\n"
    b"--- a/f.txt\n"
    b"+++ b/f.txt\n"
    b"@@ -1 +1 @@\n"
    b"-a\n"
    b"+b\n"
)


@pytest.mark.parametrize(
    ("data", "rule"),
    [
        (b"", "empty"),
        (b"From abc Mon Sep 17 00:00:00 2001\n" + SIMPLE, "does not start with"),
        (SIMPLE[:-1], "end with a newline"),
        (SIMPLE.replace(b"@@ -1 +1 @@", b"@@ -1,2 +1 @@"), "ends inside a hunk"),
        (SIMPLE.replace(b"-a\n", b"*a\n"), "malformed hunk line"),
        (SIMPLE.replace(b"index 7898192..6178079 100644\n", b"index zz..yy\n"), "index line"),
        (SIMPLE.replace(b"index", b"copy from g.txt\ncopy to f.txt\nindex"), "copies"),
        (SIMPLE.replace(b"index", b"weird header\nindex"), "unrecognized"),
        (SIMPLE + b"trailing junk\n", "unexpected line"),
        (SIMPLE.replace(b"--- a/f.txt", b"--- f.txt"), "a/ prefix"),
        (SIMPLE.replace(b"+++ b/f.txt", b"+++ b/g.txt"), "names"),
        (SIMPLE.replace(b"index 7898192..6178079 100644\n", b""), "index line"),
    ],
)
def test_a_malformed_patch_is_refused_with_its_rule(data, rule):
    with pytest.raises(PatchError, match=rule):
        parse(data)


def test_a_binary_hunk_larger_than_the_limit_is_refused_before_inflating(repo):
    repo.write("src/cart.py", "x = 1\n")
    base = repo.commit()
    repo.write("assets/big.bin", binary(4096))

    with pytest.raises(PatchError, match="larger than"):
        parse_patch(repo.patch(base), max_binary_bytes=1024)


def test_the_binary_limit_is_a_budget_for_the_whole_patch(repo):
    """Each hunk alone fits; together they would not."""
    repo.write("src/cart.py", "x = 1\n")
    base = repo.commit()
    for i in range(3):
        repo.write(f"assets/{i}.bin", binary(600, seed=i))

    parse_patch(repo.patch(base), max_binary_bytes=1800)
    with pytest.raises(PatchError, match="1700 bytes"):
        parse_patch(repo.patch(base), max_binary_bytes=1700)
