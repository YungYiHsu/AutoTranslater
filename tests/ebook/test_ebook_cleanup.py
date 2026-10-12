"""Cleanup tests use temporary files only, never real OCR records."""

from unittest.mock import patch

import pytest

from core.ebook.ocr import session


def test_clear_only_session_children(tmp_path):
    records = tmp_path / "ebook-sessions"
    (records / "one" / "crops").mkdir(parents=True)
    (records / "one" / "crops" / "line.png").write_bytes(b"synthetic")
    (records / "extra.txt").write_text("test")
    (tmp_path / "works").mkdir()
    (tmp_path / "works" / "work.json").write_text("{}")
    with patch.object(session, "PRIVATE", tmp_path), patch.object(session, "SESSIONS", records):
        assert session.clear_sessions() == (2, [])
        assert list(records.iterdir()) == []
        assert (tmp_path / "works" / "work.json").exists()
        assert session.clear_sessions() == (0, [])


def test_reject_wrong_target(tmp_path):
    with (patch.object(session, "PRIVATE", tmp_path),
          patch.object(session, "SESSIONS", tmp_path), pytest.raises(ValueError)):
        session.clear_sessions()


def test_partial_failure_reported(tmp_path):
    records = tmp_path / "ebook-sessions"
    (records / "locked").mkdir(parents=True)
    with (patch.object(session, "PRIVATE", tmp_path),
          patch.object(session, "SESSIONS", records),
          patch.object(session.shutil, "rmtree", side_effect=PermissionError("locked"))):
        count, errors = session.clear_sessions()
        assert count == 0 and len(errors) == 1
        assert (records / "locked").exists()
