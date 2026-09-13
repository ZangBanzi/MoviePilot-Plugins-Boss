"""Selected cover plans stay selected through persistence, rendering and publishing."""
import copy
import io
import threading

import pytest
from PIL import Image

from test_native_covers import origin, setup, wait_job


def plan(studio, view, style="stack"):
    return studio.options(view, {"style": style, "animated": False, "sort": "name"})


def generated_history(studio):
    return [row for row in studio._read_index("history")
            if row.get("purpose") != "before_native_publish"]


def test_selected_diagonal_plan_is_published_and_saved_without_touching_unselected(setup, monkeypatch):
    plugin, virtual, source = setup
    studio = plugin._cover_studio
    native = studio.load_native()
    untouched = plugin._make_virtual_view("fixture:unselected", "未选片库", "attribute",
        list(virtual["item_ids"]), plugin._proxy_item_index, "2026-09-13")
    plugin._virtual_views[untouched["id"]] = untouched
    for view in [*native, virtual, untouched]:
        studio.save_options(view["key"], plan(studio, view))
    unselected_options = studio.options(untouched)
    unselected_tag = plugin._virtual_views[untouched["id"]]["cover_tag"]
    unselected_art, _ = studio.admin_artwork(untouched, unselected_options)
    unselected_image = studio.encode(untouched, unselected_options, unselected_art)[1]
    original_b = source.current_b
    selected = [native[0]["key"], virtual["key"]]
    writes = []
    monkeypatch.setattr(plugin, "update_config", lambda value: writes.append(copy.deepcopy(value)))

    studio.action({"action": "generate", "server": studio.server_id(plugin._gateway_client_cache),
                   "keys": selected, "options": plan(studio, virtual, "diagonal"), "publish": True})
    job = wait_job(studio)

    assert (job["total"], job["done"], job["failed"]) == (2, 2, 0)
    assert len(writes) == 1
    assert {row["key"] for row in job["results"]} == set(selected)
    assert all(row["style"] == "diagonal" for row in job["results"])
    rows = generated_history(studio)
    assert {row["key"] for row in rows} == set(selected)
    for row in rows:
        assert row["options"]["style"] == "diagonal"
        assert studio.options({"key": row["key"]})["style"] == "diagonal"
        target = copy.deepcopy(studio.view(row["key"]))
        art, _ = studio.admin_artwork(target, row["options"])
        expected = studio.encode(target, row["options"], art)[1]
        actual = studio._file("history", row["id"], ".image").read_bytes()
        assert actual == expected
        with Image.open(io.BytesIO(actual)) as image:
            assert image.format == "PNG" and image.size == (640, 360)
    assert set(source.posted) == {"/Items/libA/Images/Primary"}
    native_result = next(row for row in rows if row["native"])
    assert source.current == studio._file("history", native_result["id"], ".image").read_bytes()
    assert source.current_b == original_b
    assert studio.options(native[1])["style"] == "stack"
    assert studio.options(untouched) == unselected_options
    assert plugin._virtual_views[untouched["id"]]["cover_tag"] == unselected_tag
    assert studio.encode(untouched, studio.options(untouched), unselected_art)[1] == unselected_image


def test_single_generate_uses_explicit_plan_instead_of_saved_override_or_default(setup):
    plugin, virtual, source = setup
    studio = plugin._cover_studio
    studio.save_options("", plan(studio, virtual, "wall"), global_scope=True)
    studio.save_options(virtual["key"], plan(studio, virtual, "stack"))
    studio.action({"action": "generate", "key": virtual["key"],
                   "options": plan(studio, virtual, "diagonal")})
    job = wait_job(studio)
    assert (job["total"], job["failed"]) == (1, 0)
    assert job["results"][0]["style"] == "diagonal"
    assert generated_history(studio)[0]["options"]["style"] == "diagonal"
    assert studio.options(virtual)["style"] == "diagonal"
    assert studio.config["defaults"]["style"] == "wall"
    assert not source.posted


@pytest.mark.parametrize("explicit", [False, True])
def test_batch_freezes_every_library_plan_before_rendering(setup, monkeypatch, explicit):
    plugin, virtual, _ = setup
    studio = plugin._cover_studio
    native = studio.load_native()[0]
    targets = [native, virtual]
    expected = ["diagonal", "diagonal" if explicit else "wall"]
    for target, style in zip(targets, ["diagonal", "wall"]):
        studio.save_options(target["key"], plan(studio, target, style))
    entered, release = threading.Event(), threading.Event()
    original_artwork = studio.admin_artwork
    seen = []

    def pause_first(target, options):
        seen.append((target["key"], options["style"]))
        if len(seen) == 1:
            entered.set()
            assert release.wait(5), "test did not release first library render"
        return original_artwork(target, options)

    monkeypatch.setattr(studio, "admin_artwork", pause_first)
    data = {"action": "generate", "server": studio.server_id(plugin._gateway_client_cache),
            "keys": [target["key"] for target in targets]}
    if explicit:
        data["options"] = plan(studio, virtual, "diagonal")
    studio.action(data)
    try:
        assert entered.wait(3)
        changed = copy.deepcopy(plugin._saved_config)
        changed["cover_studio"]["defaults"] = plan(studio, virtual, "minimal")
        for target in targets:
            changed["cover_studio"]["overrides"][target["key"]] = plan(studio, target, "minimal")
        plugin._saved_config = changed
    finally:
        release.set()
    job = wait_job(studio)
    assert job["failed"] == 0
    assert seen == [(target["key"], style) for target, style in zip(targets, expected)]
    assert [row["style"] for row in job["results"]] == expected
    by_key = {row["key"]: row["options"]["style"] for row in generated_history(studio)}
    assert by_key == dict(seen)


@pytest.mark.parametrize("case", ["empty", "null", "string", "non_string", "blank",
                                  "stale", "missing_server", "invalid_options"])
def test_invalid_batch_rejects_whole_selection_before_writes(setup, monkeypatch, case):
    plugin, virtual, source = setup
    studio = plugin._cover_studio
    native = studio.load_native()[0]
    data = {"action": "generate", "server": studio.server_id(plugin._gateway_client_cache),
            "keys": [native["key"], virtual["key"]], "options": plan(studio, virtual, "diagonal"),
            "publish": True}
    if case == "empty": data["keys"] = []
    elif case == "null": data["keys"] = None
    elif case == "string": data["keys"] = native["key"]
    elif case == "non_string": data["keys"] = [native["key"], 12]
    elif case == "blank": data["keys"] = [native["key"], ""]
    elif case == "stale": source.libraries = source.libraries[1:]
    elif case == "missing_server": data.pop("server")
    elif case == "invalid_options": data["options"] = {"style": "missing-scheme"}
    before = copy.deepcopy(plugin._saved_config)
    writes = []
    monkeypatch.setattr(plugin, "update_config", lambda value: writes.append(value))
    with pytest.raises(ValueError):
        studio.action(data)
    assert plugin._saved_config == before and writes == []
    assert not studio.job["running"] and not studio.job_lock.locked()
    assert not source.posted and not studio._read_index("history")


def test_foreign_native_or_virtual_keys_reject_without_publishing_on_either_server(setup, monkeypatch):
    plugin, virtual, first = setup
    studio = plugin._cover_studio
    gateway = plugin._gateway_client_cache
    first_key = studio.load_native()[0]["key"]
    second_fixture = origin.__wrapped__()
    second, address = next(second_fixture)
    try:
        alternate = type(gateway)(address, "admin", 5)
        monkeypatch.setattr(plugin, "_create_clients", lambda: [(gateway, "主服务器"), (alternate, "第二服务器")])
        second_server = studio.server_id(alternate)
        studio.action({"action": "load_native", "server": second_server})
        second_key = next(row["key"] for row in studio.native_views if row["server"] == second_server)
        before = copy.deepcopy(plugin._saved_config)
        writes = []
        monkeypatch.setattr(plugin, "update_config", lambda value: writes.append(value))
        for server, selected in [(studio.server_id(gateway), [first_key, second_key]),
                                 (second_server, [second_key, virtual["key"]])]:
            with pytest.raises(ValueError):
                studio.action({"action": "generate", "server": server, "keys": selected,
                    "options": plan(studio, virtual, "diagonal"), "publish": True})
            assert plugin._saved_config == before and writes == []
            assert not studio.job["running"] and not studio.job_lock.locked()
            assert not studio._read_index("history")
        assert not first.posted and not second.posted
        assert plugin._gateway_client_cache is gateway
    finally:
        second_fixture.close()


def test_duplicate_selection_generates_and_publishes_each_library_once(setup):
    plugin, virtual, source = setup
    studio = plugin._cover_studio
    key = studio.load_native()[0]["key"]
    studio.action({"action": "generate", "server": studio.server_id(plugin._gateway_client_cache),
        "keys": [key, virtual["key"], key, virtual["key"]], "publish": True,
        "options": plan(studio, virtual, "diagonal")})
    job = wait_job(studio)
    assert (job["total"], job["done"], job["failed"]) == (2, 2, 0)
    assert [row["key"] for row in job["results"]] == [key, virtual["key"]]
    assert len(generated_history(studio)) == 2
    assert len([call for call in source.calls if call[0] == "POST"]) == 1


def test_persistence_failure_does_not_start_job_or_leave_generation_locked(setup, monkeypatch):
    plugin, virtual, source = setup
    studio = plugin._cover_studio
    before_config, before_job = copy.deepcopy(plugin._saved_config), dict(studio.job)
    save = plugin.update_config

    def disk_failure(_):
        raise OSError("fixture configuration disk failure")

    monkeypatch.setattr(plugin, "update_config", disk_failure)
    data = {"action": "generate", "server": studio.server_id(plugin._gateway_client_cache),
            "keys": [virtual["key"]], "options": plan(studio, virtual, "diagonal")}
    with pytest.raises(OSError, match="configuration disk failure"):
        studio.action(data)
    assert plugin._saved_config == before_config and studio.job == before_job
    assert not studio.job_lock.locked() and not source.posted
    assert not studio._read_index("history")
    monkeypatch.setattr(plugin, "update_config", save)
    studio.action(data)
    assert wait_job(studio)["failed"] == 0
    assert generated_history(studio)[0]["options"]["style"] == "diagonal"
