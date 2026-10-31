"""Ledger 3.0-U01: a directory fsync the filesystem can't do mustn't stop a paid request after the proof is safely
written; a real I/O error still raises."""
import errno
import json
import os

import pytest

from agent_referee_client import FileProofStore


@pytest.mark.skipif(os.name != "posix", reason="the directory fsync only runs on POSIX")
@pytest.mark.parametrize("code", [errno.EINVAL, errno.EACCES, errno.EBADF])
def test_unsupported_directory_sync_is_skipped_and_the_proof_is_kept(tmp_path, monkeypatch, code):
    store = FileProofStore(str(tmp_path / "proofs"))
    real_fsync = os.fsync

    def fsync(fd):
        if os.path.isdir(os.readlink(f"/proc/self/fd/{fd}")) if os.path.exists(f"/proc/self/fd/{fd}") else False:
            raise OSError(code, os.strerror(code))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    store.save("k1", "proof-1", {"match_id": "m1", "seat": "alpha"})
    files = list((tmp_path / "proofs").iterdir())
    assert len(files) == 1
    assert json.loads(files[0].read_text())["proof"] == "proof-1"


@pytest.mark.skipif(os.name != "posix", reason="the directory fsync only runs on POSIX")
def test_a_real_io_error_on_the_directory_still_raises(tmp_path, monkeypatch):
    store = FileProofStore(str(tmp_path / "proofs"))
    real_fsync = os.fsync

    def fsync(fd):
        if os.path.exists(f"/proc/self/fd/{fd}") and os.path.isdir(os.readlink(f"/proc/self/fd/{fd}")):
            raise OSError(errno.EIO, "I/O error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError):
        store.save("k1", "proof-1")
