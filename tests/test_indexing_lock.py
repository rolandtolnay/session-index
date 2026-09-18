"""Artifact writers and deletion must serialize only within the same session."""

import fcntl

import pytest

from indexing_lock import indexing_lock


def test_session_lock_excludes_same_owner_allows_others_and_releases_on_error(tmp_path, monkeypatch):
    # Probe real OS lock contention without letting a broken test hang. Only
    # the blocking mode changes; lock selection and release stay real.
    flock = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda fd, operation: flock(fd, operation | fcntl.LOCK_NB))

    with pytest.raises(RuntimeError, match="interrupted write"):
        with indexing_lock(tmp_path, "pi:one"):
            with pytest.raises(BlockingIOError):
                with indexing_lock(tmp_path, "pi:one"):
                    pytest.fail("same-session operations must not overlap")
            with indexing_lock(tmp_path, "pi:two"):
                pass
            raise RuntimeError("interrupted write")

    with indexing_lock(tmp_path, "pi:one"):
        pass
