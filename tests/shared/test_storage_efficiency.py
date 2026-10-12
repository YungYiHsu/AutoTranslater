from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from core.shared.checkpoint import CheckpointStore
from core.shared.exceptions import CheckpointError
from core.shared.storage import atomic_text
from tests.shared.test_checkpoint import make_job, translated


def test_atomic_failure_preserves_original_and_cleans_temp(tmp_path):
    target = tmp_path / "target.txt"
    atomic_text(target, "original")
    with (
        patch("core.shared.storage.os.fsync", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        atomic_text(target, "replacement")
    assert target.read_text() == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_parallel_writers_have_unique_temporary_files(tmp_path):
    target = tmp_path / "parallel.txt"
    values = [str(i) * 2000 for i in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda text: atomic_text(target, text), values))
    assert target.read_text() in values
    assert list(tmp_path.iterdir()) == [target]


def test_checkpoint_writer_validates_prefix_once_per_run(tmp_path):
    store, job = CheckpointStore(tmp_path), make_job()
    with patch.object(store, "load", wraps=store.load) as load:
        writer = store.writer(job)
        for index in range(3):
            writer.save(translated(job, index))
        assert load.call_count == 1
    assert len(store.load(job)) == 3


def test_checkpoint_writer_rejects_external_changes(tmp_path):
    store, job = CheckpointStore(tmp_path), make_job()
    writer = store.writer(job)
    writer.save(translated(job, 0))
    target = store.path_for(job) / "chunks/0000.json"
    target.write_text("{}")
    with pytest.raises(CheckpointError, match="changed"):
        writer.save(translated(job, 1))
    with pytest.raises(CheckpointError):
        store.writer(job)


def test_failed_append_does_not_advance_writer(tmp_path):
    store, job = CheckpointStore(tmp_path), make_job()
    writer = store.writer(job)
    with (
        patch.object(store, "_atomic_write", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        writer.save(translated(job, 0))
    assert writer.completed == ()
