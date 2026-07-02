"""Tests for prompt file attachments — Path A (spec: prompt file attachments).

Covers the pure storage/sanitization helpers in ``lotsa.attachments`` and the
race-safe metadata-append primitive ``TaskDB.append_attachment``.

Security-first (spec requirement 7 / AC 7): filename traversal is sanitized,
the per-file size cap and per-task count cap hold, and same-name collisions are
suffixed rather than overwriting existing bytes.

These tests are written before the implementation exists, so importing
``lotsa.attachments`` fails until the module lands — that ImportError is the
expected "red" for this step.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from lotsa.attachments import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_TASK,
    attachments_root,
    materialize_into_worktree,
    sanitize_filename,
    write_attachment,
)
from lotsa.db import TaskDB


@pytest.fixture()
def _loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def run(_loop):
    return _loop.run_until_complete


@pytest.fixture()
def db(tmp_path, run):
    database = TaskDB(tmp_path / "lotsa.db")
    run(database.initialize())
    yield database
    run(database.close())


# ── Caps are the values the spec pins ──────────────────────────────────


class TestCaps:
    def test_size_cap_is_25_mb(self):
        assert MAX_FILE_BYTES == 25 * 1024 * 1024

    def test_count_cap_is_10(self):
        assert MAX_FILES_PER_TASK == 10


# ── Filename sanitization (security) ───────────────────────────────────


class TestSanitizeFilename:
    def test_strips_parent_traversal_to_basename(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"

    def test_strips_absolute_path_to_basename(self):
        assert sanitize_filename("/abs/path/shot.png") == "shot.png"

    def test_strips_backslash_components(self):
        assert sanitize_filename("a\\b\\c.png") == "c.png"

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError):
            sanitize_filename("bug\x00.png")

    def test_rejects_dotdot_only(self):
        with pytest.raises(ValueError):
            sanitize_filename("..")

    def test_rejects_dot_only(self):
        with pytest.raises(ValueError):
            sanitize_filename(".")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            sanitize_filename("")

    def test_keeps_a_plain_name(self):
        assert sanitize_filename("data.csv") == "data.csv"


# ── On-disk storage + collision suffixing ──────────────────────────────


class TestWriteAttachment:
    def test_writes_bytes_under_task_dir(self, tmp_path):
        data = b"\x89PNG\r\n\x1a\nhello"
        record = write_attachment(
            data_dir=tmp_path,
            project_id="default",
            task_id="abcd1234",
            raw_filename="shot.png",
            data=data,
            existing_names=set(),
            mime="image/png",
        )
        assert record["filename"] == "shot.png"
        assert record["rel_path"] == ".lotsa/attachments/shot.png"
        assert record["mime"] == "image/png"
        assert record["size_bytes"] == len(data)
        assert record["created_at"]

        stored = attachments_root(tmp_path, "default", "abcd1234") / "shot.png"
        assert stored.exists()
        assert stored.read_bytes() == data

    def test_traversal_filename_stays_inside_task_dir(self, tmp_path):
        record = write_attachment(
            data_dir=tmp_path,
            project_id="default",
            task_id="abcd1234",
            raw_filename="../../evil.sh",
            data=b"#!/bin/sh\n",
            existing_names=set(),
            mime="text/x-sh",
        )
        # Sanitized to a basename and stored strictly under the task's dir.
        assert record["filename"] == "evil.sh"
        root = attachments_root(tmp_path, "default", "abcd1234")
        assert (root / "evil.sh").exists()
        # Nothing escaped to a parent directory.
        assert not (tmp_path / "evil.sh").exists()
        assert not (tmp_path.parent / "evil.sh").exists()

    def test_collision_is_suffixed_not_overwritten(self, tmp_path):
        first = write_attachment(
            data_dir=tmp_path,
            project_id="default",
            task_id="abcd1234",
            raw_filename="bug.png",
            data=b"AAAA",
            existing_names=set(),
            mime="image/png",
        )
        second = write_attachment(
            data_dir=tmp_path,
            project_id="default",
            task_id="abcd1234",
            raw_filename="bug.png",
            data=b"BBBB",
            existing_names={first["filename"]},
            mime="image/png",
        )
        assert first["filename"] == "bug.png"
        assert second["filename"] == "bug (1).png"

        root = attachments_root(tmp_path, "default", "abcd1234")
        # The original bytes survive — the collision did not overwrite them.
        assert (root / "bug.png").read_bytes() == b"AAAA"
        assert (root / "bug (1).png").read_bytes() == b"BBBB"


# ── Materialization into the worktree ──────────────────────────────────


class TestMaterializeIntoWorktree:
    def _seed(self, data_dir, task_id="abcd1234", name="bug.png", data=b"PNGDATA"):
        return write_attachment(
            data_dir=data_dir,
            project_id="default",
            task_id=task_id,
            raw_filename=name,
            data=data,
            existing_names=set(),
            mime="image/png",
        )

    def test_copies_files_and_writes_gitignore(self, tmp_path):
        data_dir = tmp_path / "data"
        work_dir = tmp_path / "wt"
        work_dir.mkdir()
        record = self._seed(data_dir)

        rel_paths = materialize_into_worktree(
            records=[record],
            data_dir=data_dir,
            project_id="default",
            task_id="abcd1234",
            work_dir=work_dir,
        )

        assert rel_paths == [".lotsa/attachments/bug.png"]
        copied = work_dir / ".lotsa" / "attachments" / "bug.png"
        assert copied.exists()
        assert copied.read_bytes() == b"PNGDATA"
        # The managed ignore makes everything under .lotsa/ untracked.
        gitignore = work_dir / ".lotsa" / ".gitignore"
        assert gitignore.exists()
        assert "*" in gitignore.read_text()

    def test_idempotent_re_materialization(self, tmp_path):
        data_dir = tmp_path / "data"
        work_dir = tmp_path / "wt"
        work_dir.mkdir()
        record = self._seed(data_dir)

        first = materialize_into_worktree(
            records=[record],
            data_dir=data_dir,
            project_id="default",
            task_id="abcd1234",
            work_dir=work_dir,
        )
        second = materialize_into_worktree(
            records=[record],
            data_dir=data_dir,
            project_id="default",
            task_id="abcd1234",
            work_dir=work_dir,
        )
        assert first == second
        attach_dir = work_dir / ".lotsa" / "attachments"
        # Re-running did not duplicate the file.
        assert [p.name for p in attach_dir.iterdir()] == ["bug.png"]

    def test_materialized_attachments_are_git_ignored(self, tmp_path):
        """AC 5 — attachments are never committed to the PR branch.

        Materialize into a real git repo, ``git add -A``, and assert the
        attachment directory is ignored (absent from ``git status``).
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

        data_dir = tmp_path / "data"
        record = self._seed(data_dir)
        materialize_into_worktree(
            records=[record],
            data_dir=data_dir,
            project_id="default",
            task_id="abcd1234",
            work_dir=repo,
        )

        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert ".lotsa" not in status
        assert "bug.png" not in status


# ── Race-safe metadata append (TaskDB.append_attachment) ───────────────


class TestAppendAttachment:
    def _record(self, name):
        return {
            "filename": name,
            "rel_path": f".lotsa/attachments/{name}",
            "mime": "image/png",
            "size_bytes": 4,
            "created_at": "2026-07-02T00:00:00+00:00",
        }

    def test_appends_to_empty_metadata(self, db, run):
        task = run(db.create_task("Attach", project_id="default"))
        ok = run(db.append_attachment(task.id, self._record("a.png"), cap=MAX_FILES_PER_TASK))
        assert ok is True

        fresh = run(db.get_task(task.id))
        assert fresh is not None
        assert [a["filename"] for a in fresh.metadata["attachments"]] == ["a.png"]

    def test_appends_preserve_existing_metadata_keys(self, db, run):
        task = run(db.create_task("Attach", project_id="default", metadata={"session_id": "s-1"}))
        run(db.append_attachment(task.id, self._record("a.png"), cap=MAX_FILES_PER_TASK))

        fresh = run(db.get_task(task.id))
        assert fresh is not None
        assert fresh.metadata["session_id"] == "s-1"
        assert len(fresh.metadata["attachments"]) == 1

    def test_cap_enforced_at_the_append(self, db, run):
        task = run(db.create_task("Attach", project_id="default"))
        for i in range(MAX_FILES_PER_TASK):
            ok = run(db.append_attachment(task.id, self._record(f"f{i}.png"), cap=MAX_FILES_PER_TASK))
            assert ok is True
        # The (cap+1)th append is rejected by the WHERE-clause guard.
        overflow = run(db.append_attachment(task.id, self._record("over.png"), cap=MAX_FILES_PER_TASK))
        assert overflow is False

        fresh = run(db.get_task(task.id))
        assert fresh is not None
        assert len(fresh.metadata["attachments"]) == MAX_FILES_PER_TASK
