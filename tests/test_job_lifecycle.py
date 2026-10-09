"""Cover jobs cannot outlive reloads or expose mutable administration snapshots."""
import copy
import sys
import threading

import pytest

from test_coverstudio import make_plugin, poster
from test_native_covers import origin, setup, wait_job


def fake_encode(*args):
    args[-1].update(requested_format="png", artwork_count=0)
    return "image/png", poster()


@pytest.mark.parametrize("transition", ["stop", "reload"])
@pytest.mark.parametrize("stage", ["artwork", "encode", "backup", "dispatch"])
def test_lifecycle_change_prevents_native_post(setup, monkeypatch, transition, stage):
    plugin, _, source = setup
    studio = plugin._cover_studio
    native = studio.load_native()[0]
    gateway = plugin._gateway_client_cache
    monkeypatch.setattr(plugin, "_create_clients", lambda: [(gateway, "本地替身")])
    options = studio.options(native, {"source": "brand", "animated": False})
    entered, release = threading.Event(), threading.Event()
    method = {"artwork": "admin_artwork", "encode": "encode",
              "backup": "_admin_request", "dispatch": "_admin_request"}[stage]
    original = getattr(studio, method)

    def pause(*args, **kwargs):
        if stage == "dispatch":
            if args[0] == "POST":
                entered.set()
                assert release.wait(5)
            return original(*args, **kwargs)
        value = original(*args, **kwargs)
        if stage != "backup" or args[:2] == ("GET", "/Items/libA/Images/Primary"):
            entered.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(studio, method, pause)
    studio.start_generate(native["key"], publish=True, options=options)
    try:
        assert entered.wait(3)
        if transition == "stop":
            plugin.stop_service()
        else:
            cfg = copy.deepcopy(plugin._saved_config)
            cfg.update(enabled=False, virtual_enabled=False, ranking_enabled=False, auto_sync=False)
            plugin.init_plugin(cfg)
    finally:
        release.set()
    job = wait_job(studio)
    assert not source.posted
    assert job["message"] == "已停止" and job["done"] == 0 and job["failed"] == 0
    assert not studio.job_lock.locked()
    if transition == "reload":
        studio.start_generate(native["key"], publish=True, options=options)
        assert wait_job(studio)["failed"] == 0
        assert source.posted
    else:
        with pytest.raises(ValueError, match="停止|重载"):
            studio.start_generate(native["key"], options=options)


def test_normal_cancel_finishes_current_cover_only(tmp_path, monkeypatch):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    second = dict(view, key="fixture:second", id="second")
    plugin._virtual_views["second"] = second
    entered, release = threading.Event(), threading.Event()

    def artwork(*_):
        entered.set()
        assert release.wait(5)
        return [], []

    monkeypatch.setattr(studio, "admin_artwork", artwork)
    monkeypatch.setattr(studio, "encode", fake_encode)
    studio.start_generate()
    try:
        assert entered.wait(3)
        studio.action({"action": "cancel"})
    finally:
        release.set()
    job = wait_job(studio)
    assert (job["done"], job["total"], job["failed"]) == (1, 2, 0)
    assert job["message"] == "已停止"
    assert len(studio._read_index("history")) == 1


def test_save_race_rechecks_exclusive_operation(tmp_path, monkeypatch):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    module = sys.modules[type(studio).__module__]
    normalize = module.normalize_options
    normalizing, resume_save, rendering, resume_render = [threading.Event() for _ in range(4)]
    errors = []
    options = studio.options(view, {"title": "并发修改"})

    def pause_normalize(value):
        if threading.current_thread().name == "fixture-save":
            normalizing.set()
            assert resume_save.wait(5)
        return normalize(value)

    def save():
        try:
            studio.save_options(view["key"], options)
        except ValueError as exc:
            errors.append(str(exc))

    def artwork(*_):
        rendering.set()
        assert resume_render.wait(5)
        return [], []

    monkeypatch.setattr(module, "normalize_options", pause_normalize)
    monkeypatch.setattr(studio, "admin_artwork", artwork)
    monkeypatch.setattr(studio, "encode", fake_encode)
    worker = threading.Thread(target=save, name="fixture-save")
    worker.start()
    try:
        assert normalizing.wait(3)
        studio.start_generate(view["key"])
        assert rendering.wait(3)
        resume_save.set()
        worker.join(3)
        assert not worker.is_alive()
        assert errors and "生成" in errors[0]
        assert studio.options(view)["title"] != "并发修改"
    finally:
        resume_save.set()
        resume_render.set()
        worker.join(3)
        wait_job(studio)


def test_reset_cannot_change_active_job_plan(tmp_path):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    studio.save_options(view["key"], studio.options(view, {"title": "保留"}))
    assert studio.job_lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match="生成"):
            studio.action({"action": "reset_override", "key": view["key"]})
        assert studio.options(view)["title"] == "保留"
    finally:
        studio.job_lock.release()


@pytest.mark.parametrize("light", [True, False])
def test_state_returns_detached_nested_job(tmp_path, light):
    plugin, _, _ = make_plugin(tmp_path)
    studio = plugin._cover_studio
    studio.job.update(results=[{"render_info": {"reason": "original"}}], errors=["original"])
    snapshot = plugin.studio_state(light=light).data["job"]
    studio.job["results"][0]["render_info"]["reason"] = "changed"
    studio.job["errors"].append("changed")
    assert snapshot["results"][0]["render_info"]["reason"] == "original"
    assert snapshot["errors"] == ["original"]


@pytest.mark.parametrize("initial", [True, False])
def test_history_preference_is_frozen_for_batch(tmp_path, monkeypatch, initial):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    plugin._saved_config.setdefault("cover_studio", {})["history_enabled"] = initial
    entered, release = threading.Event(), threading.Event()

    def artwork(*_):
        entered.set()
        assert release.wait(5)
        return [], []

    monkeypatch.setattr(studio, "admin_artwork", artwork)
    monkeypatch.setattr(studio, "encode", fake_encode)
    studio.start_generate(view["key"])
    try:
        assert entered.wait(3)
        studio.config["history_enabled"] = not initial
    finally:
        release.set()
    assert wait_job(studio)["failed"] == 0
    assert len(studio._read_index("history")) == int(initial)


def test_reload_during_target_validation_cannot_start_old_job(setup, monkeypatch):
    plugin, _, source = setup
    studio = plugin._cover_studio
    native = studio.load_native()[0]
    gateway = plugin._gateway_client_cache
    monkeypatch.setattr(plugin, "_create_clients", lambda: [(gateway, "替身")])
    entered, release = threading.Event(), threading.Event()
    client = studio.client
    errors = []

    def pause_client():
        value = client()
        entered.set()
        assert release.wait(5)
        return value

    def start():
        try:
            studio.start_generate(native["key"], publish=True)
        except ValueError as exc:
            errors.append(str(exc))

    monkeypatch.setattr(studio, "client", pause_client)
    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert entered.wait(3)
        cfg = copy.deepcopy(plugin._saved_config)
        cfg.update(enabled=False, virtual_enabled=False, ranking_enabled=False, auto_sync=False)
        plugin.init_plugin(cfg)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and errors and "重载" in errors[0]
    assert not studio.job["running"] and not studio.job_lock.locked() and not source.posted


@pytest.mark.parametrize("transition", ["stop", "reload"])
def test_native_restore_also_rejects_lifecycle_change(setup, monkeypatch, transition):
    plugin, _, source = setup
    studio = plugin._cover_studio
    native = studio.load_native()[0]
    gateway = plugin._gateway_client_cache
    monkeypatch.setattr(plugin, "_create_clients", lambda: [(gateway, "替身")])
    row = studio._history_entry(native, studio.options(native), "image/png", poster(), "fixture")
    studio._write_index("history", [row])
    entered, release = threading.Event(), threading.Event()
    request = studio._admin_request
    errors = []

    def pause(*args, **kwargs):
        result = request(*args, **kwargs)
        if args[:2] == ("GET", "/Items/libA/Images/Primary"):
            entered.set()
            assert release.wait(5)
        return result

    def restore():
        try:
            studio.action({"action": "restore_native_image", "id": row["id"]})
        except ValueError as exc:
            errors.append(str(exc))

    monkeypatch.setattr(studio, "_admin_request", pause)
    worker = threading.Thread(target=restore)
    worker.start()
    try:
        assert entered.wait(3)
        if transition == "stop":
            plugin.stop_service()
        else:
            cfg = copy.deepcopy(plugin._saved_config)
            cfg.update(enabled=False, virtual_enabled=False, ranking_enabled=False, auto_sync=False)
            plugin.init_plugin(cfg)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and errors and "重载" in errors[0]
    assert not source.posted and not studio.job_lock.locked()


def test_stop_keeps_already_completed_history(tmp_path, monkeypatch):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    plugin._virtual_views["second"] = dict(view, key="fixture:second", id="second")
    entered, release = threading.Event(), threading.Event()

    def artwork(target, *_):
        if target["key"] == "fixture:second":
            entered.set()
            assert release.wait(5)
        return [], []

    monkeypatch.setattr(studio, "admin_artwork", artwork)
    monkeypatch.setattr(studio, "encode", fake_encode)
    studio.start_generate()
    try:
        assert entered.wait(3)
        plugin.stop_service()
    finally:
        release.set()
    job = wait_job(studio)
    assert job["done"] == 1 and job["failed"] == 0 and job["message"] == "已停止"
    assert [row["key"] for row in studio._read_index("history")] == [view["key"]]


def test_thread_start_failure_releases_operation_for_retry(tmp_path, monkeypatch):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    start = threading.Thread.start

    def fail(_):
        raise RuntimeError("fixture cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError):
        studio.start_generate(view["key"])
    assert not studio.job_snapshot()["running"] and not studio.job_lock.locked()
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(studio, "admin_artwork", lambda *_: ([], []))
    monkeypatch.setattr(studio, "encode", fake_encode)
    response = studio.start_generate(view["key"])
    assert wait_job(studio)["done"] == 1
    assert response["running"] and response["done"] == 0
