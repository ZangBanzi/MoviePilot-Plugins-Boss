"""History deletion is local, complete, serialized, and recoverable after disk errors."""
import copy
import threading
import uuid
from pathlib import Path

import pytest

from test_coverstudio import make_plugin, poster
from test_native_covers import origin, setup, wait_job


@pytest.fixture
def studio(tmp_path):
    plugin, _, view = make_plugin(tmp_path)
    yield plugin._cover_studio, view
    plugin.stop_service()


def seed(studio, view, count=2):
    payload = poster()
    options = studio.options(view)
    rows = [studio._history_entry(view, options, "image/png", payload, uuid.uuid4().hex)
            for _ in range(count)]
    studio._write_index("history", rows)
    return rows


def test_clear_entire_index_beyond_visible_sixty_preserves_other_data(studio):
    s, view = studio
    rows = seed(s, view, 73)
    original_config = copy.deepcopy(s.plugin._saved_config)
    protected = [s.root / "backups" / "keep.json", s.root / "fonts" / "keep.ttf",
                 s.root / "history" / "notes.txt", s.root / "history" / "unknown.image"]
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    orphan = s._file("history", uuid.uuid4().hex, ".image")
    orphan.write_bytes(poster())
    assert len(s.state()["history"]) == 60
    result = s.action({"action": "clear_history", "confirm": True, "server": "removed-server"})
    assert result["deleted"] == 73 and result["remaining"] == 0 and result["failed"] == 0
    assert result["orphan_deleted"] == 1 and not orphan.exists()
    assert "1 项残留文件" in result["message"]
    assert s._read_index("history") == []
    assert all(not s._file("history", row["id"], suffix).exists()
               for row in rows for suffix in (".image", ".jpg"))
    assert all(path.read_bytes() == b"keep" for path in protected)
    assert s.plugin._saved_config == original_config


def test_delete_one_exact_card_works_without_its_original_server(studio):
    s, view = studio
    rows = seed(s, view)
    result = s.action({"action": "delete_history", "id": rows[0]["id"], "server": "removed-server"})
    assert result["deleted"] == 1 and result["remaining"] == 1 and result["failed"] == 0
    assert s._read_index("history") == [rows[1]]
    for suffix in (".image", ".jpg"):
        assert not s._file("history", rows[0]["id"], suffix).exists()
        assert s._file("history", rows[1]["id"], suffix).is_file()


@pytest.mark.parametrize("identifier", [None, "../outside", "0" * 32])
def test_missing_and_invalid_ids_leave_history_untouched(studio, identifier):
    s, view = studio
    rows = seed(s, view)
    with pytest.raises(ValueError):
        s.action({"action": "delete_history", "id": identifier})
    assert s._read_index("history") == rows
    assert all(s._file("history", row["id"], ".image").exists() for row in rows)
    assert not s.job_lock.locked()


@pytest.mark.parametrize("confirmation", [None, False, "true"])
def test_clear_requires_explicit_confirmation(studio, confirmation):
    s, view = studio
    rows = seed(s, view)
    with pytest.raises(ValueError, match="确认"):
        s.action({"action": "clear_history", "confirm": confirmation})
    assert s._read_index("history") == rows


def test_malicious_index_path_is_rejected_before_index_commit(studio, tmp_path):
    s, view = studio
    rows = seed(s, view)
    outside = tmp_path.parent / (uuid.uuid4().hex + ".image")
    outside.write_bytes(b"outside")
    try:
        bad = dict(rows[0], id="../" + outside.stem)
        s._write_index("history", [bad, rows[1]])
        with pytest.raises(ValueError, match="文件编号"):
            s.delete_history(clear_all=True)
        assert s._read_index("history") == [bad, rows[1]]
        assert outside.read_bytes() == b"outside"
        assert s._file("history", rows[1]["id"], ".image").is_file()
    finally:
        outside.unlink()


def test_cleanup_is_rejected_while_generation_holds_lock_and_cannot_resurrect(studio, monkeypatch):
    s, view = studio
    seed(s, view)
    entered, release = threading.Event(), threading.Event()

    def artwork(*_):
        entered.set()
        assert release.wait(5)
        return [], []

    monkeypatch.setattr(s, "admin_artwork", artwork)
    s.start_generate(view["key"])
    assert entered.wait(3)
    try:
        with pytest.raises(ValueError, match="生成或恢复"):
            s.delete_history(clear_all=True)
    finally:
        release.set()
    assert wait_job(s)["failed"] == 0
    result = s.delete_history(clear_all=True)
    assert result["deleted"] == 3 and s._read_index("history") == []
    assert not list((s.root / "history").glob("*.image"))


def test_resolved_file_alias_cannot_delete_another_history_record(studio, monkeypatch):
    s, view = studio
    rows = seed(s, view)
    real_file = s._file

    def alias(folder, identifier, suffix):
        return real_file(folder, rows[1]["id"] if identifier == rows[0]["id"] else identifier, suffix)

    # Exercise the result of a resolved symlink without requiring Windows symlink privileges.
    monkeypatch.setattr(s, "_file", alias)
    with pytest.raises(ValueError, match="指向其他记录"):
        s.delete_history(rows[0]["id"])
    assert s._read_index("history") == rows
    assert all(real_file("history", row["id"], ".image").is_file() for row in rows)


def test_initial_index_write_failure_never_deletes_files(studio, monkeypatch):
    s, view = studio
    rows = seed(s, view)

    def failed_write(*_):
        raise OSError("fixture disk full")

    monkeypatch.setattr(s, "_write_index", failed_write)
    with pytest.raises(OSError):
        s.delete_history(clear_all=True)
    assert s._read_index("history") == rows
    assert all(s._file("history", row["id"], suffix).is_file()
               for row in rows for suffix in (".image", ".jpg"))
    assert not s.job_lock.locked()


def test_partial_file_failure_retains_retryable_card_and_truthful_result(studio, monkeypatch):
    s, view = studio
    rows = seed(s, view)
    blocked = s._file("history", rows[0]["id"], ".jpg")
    real_unlink = Path.unlink

    def fail_thumbnail(path, *args, **kwargs):
        if path.suffix in {".image", ".jpg"}:
            assert s._read_index("history") == []  # Index removal precedes every delete.
        if path == blocked:
            raise PermissionError("fixture file busy")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_thumbnail)
    result = s.delete_history(clear_all=True)
    assert result["deleted"] == 1 and result["failed"] == 1 and result["remaining"] == 1
    assert "未能删除" in result["message"]
    assert s._read_index("history") == [rows[0]]
    assert s._file("history", rows[0]["id"], ".image").is_file()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert s.delete_history(rows[0]["id"])["deleted"] == 1
    assert s._read_index("history") == []


def test_failed_reindex_reports_recovery_and_clear_all_removes_orphans(studio, monkeypatch):
    s, view = studio
    rows = seed(s, view)
    blocked = s._file("history", rows[0]["id"], ".jpg")
    real_unlink, real_write = Path.unlink, s._write_index
    writes = 0

    def fail_thumbnail(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("fixture file busy")
        return real_unlink(path, *args, **kwargs)

    def fail_recovery(name, value):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("fixture index recovery failed")
        return real_write(name, value)

    monkeypatch.setattr(Path, "unlink", fail_thumbnail)
    monkeypatch.setattr(s, "_write_index", fail_recovery)
    with pytest.raises(ValueError, match="索引恢复失败"):
        s.delete_history(clear_all=True)
    assert not s.job_lock.locked()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    monkeypatch.setattr(s, "_write_index", real_write)
    result = s.delete_history(clear_all=True)
    assert result["orphan_deleted"] == 1 and result["failed"] == 0
    assert not list((s.root / "history").glob("*.image"))


def test_clearing_native_backups_never_changes_emby_or_options(setup, monkeypatch):
    plugin, view, source = setup
    s = plugin._cover_studio
    native = s.load_native()[0]
    rows = seed(s, native)
    rows[0]["purpose"] = "before_native_publish"
    s._write_index("history", rows)
    original, calls, config = source.current, copy.deepcopy(source.calls), copy.deepcopy(plugin._saved_config)

    def forbid_server(*_):
        raise AssertionError("Local cleanup must not resolve or connect to Emby")

    monkeypatch.setattr(s, "on_server", forbid_server)
    assert s.action({"action": "clear_history", "confirm": True})["deleted"] == 2
    assert source.current == original and source.calls == calls
    assert plugin._saved_config == config and plugin._virtual_views[view["id"]]["item_ids"] == view["item_ids"]
