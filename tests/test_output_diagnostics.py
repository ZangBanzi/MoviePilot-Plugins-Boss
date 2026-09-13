"""Displayed GIF/static reasons must agree with the actual generated image."""
import base64
import io
import sys

import pytest
from PIL import Image

from test_coverstudio import plugin, poster
from test_native_covers import origin, setup, wait_job


def assert_image_report(mime, payload, report):
    with Image.open(io.BytesIO(payload)) as image:
        actual_format = image.format.lower()
        assert report["actual_format"] == actual_format
        assert mime == "image/" + actual_format
        assert (getattr(image, "n_frames", 1) > 1) == (actual_format == "gif")
    assert report["message"]


@pytest.mark.parametrize("case, expected_reason, expected_count", [
    ("empty", "empty_library", 0),
    ("single", "single_artwork", 1),
    ("duplicate", "single_artwork", 1),
    ("bad", "no_artwork", 0),
    ("brand", "brand", 0),
    ("animated", "animated", 2),
    ("paused", "static_preview", 2),
    ("static", "static", 2),
])
def test_encode_reports_decoded_distinct_artwork_and_actual_format(
        plugin, case, expected_reason, expected_count):
    instance, _, original = plugin
    studio = instance._cover_studio
    view = dict(original)
    patch = {"animated": case != "static"}
    images = [poster("red"), poster("green")]
    if case == "empty":
        view["item_ids"], images = [], []
    elif case == "single":
        images = images[:1]
    elif case == "duplicate":
        # Different compressed bytes with identical pixels still make one slide.
        with Image.open(io.BytesIO(images[0])) as picture:
            alternate = io.BytesIO()
            picture.save(alternate, "PNG", compress_level=0)
        assert images[0] != alternate.getvalue()
        images = [images[0], alternate.getvalue()]
    elif case == "bad":
        images = [b"<html>upstream error</html>", b"not an image"]
    elif case == "brand":
        patch["source"], images = "brand", []
    requested = "png" if case in {"paused", "static"} else "gif"
    report = {}
    mime, payload = studio.encode(view, studio.options(view, patch), images, requested, report)
    assert report["reason"] == expected_reason
    assert report["artwork_count"] == expected_count
    assert report["requested_format"] == requested
    assert_image_report(mime, payload, report)


@pytest.mark.parametrize("play", [False, True])
def test_preview_reports_paused_png_or_real_gif_without_changing_mode(plugin, monkeypatch, play):
    instance, _, view = plugin
    studio = instance._cover_studio
    monkeypatch.setattr(studio, "admin_artwork", lambda *_: ([poster("red"), poster("green")], []))
    result = studio.preview(view["key"], {"animated": True}, animated=play)
    prefix, image = result["image"].split(",", 1)
    assert prefix == f"data:{result['mime']};base64"
    assert result["options"]["animated"] is True
    assert result["render_info"]["reason"] == ("animated" if play else "static_preview")
    assert result["artwork_count"] == 2
    assert_image_report(result["mime"], base64.b64decode(image), result["render_info"])


def test_generated_history_and_job_totals_preserve_each_static_reason(plugin, monkeypatch):
    instance, _, original = plugin
    studio = instance._cover_studio
    definitions = {
        "animated": (["m1", "m2"], [poster("red"), poster("green")], {}),
        "single_artwork": (["m1"], [poster("red")], {}),
        "empty_library": ([], [], {}),
        "no_artwork": (["m1", "m2"], [], {}),
        "brand": (["m1", "m2"], [], {"source": "brand"}),
        "static": (["m1", "m2"], [poster("red"), poster("green")], {"animated": False}),
    }
    instance._virtual_views = {}
    for reason, (members, _, patch) in definitions.items():
        view = dict(original, id="test-" + reason, key="fixture:" + reason,
                    name=reason, item_ids=members)
        instance._virtual_views[view["id"]] = view
        studio.save_options(view["key"], studio.options(view, {"animated": True, **patch}))
    monkeypatch.setattr(studio, "admin_artwork", lambda view, *_: (definitions[view["name"]][1], []))
    studio.start_generate()
    job = wait_job(studio)
    assert (job["done"], job["failed"], job["animated"], job["static"], job["fallback"]) == (6, 0, 1, 5, 4)
    assert len(job["results"]) == 6
    history = studio.state()["history"]
    assert len(history) == 6
    results = {row["key"]: row for row in job["results"]}
    actual_counts = {"gif": 0, "png": 0}
    for row in history:
        assert row["render_info"]["reason"] == row["name"]
        assert results[row["key"]]["render_info"] == row["render_info"]
        payload = studio._file("history", row["id"], ".image").read_bytes()
        assert_image_report(row["mime"], payload, row["render_info"])
        actual_counts[row["render_info"]["actual_format"]] += 1
    assert actual_counts == {"gif": job["animated"], "png": job["static"]}


def test_gif_encoder_failure_is_successful_png_with_visible_reason(plugin, monkeypatch):
    instance, _, view = plugin
    studio = instance._cover_studio
    studio.save_options(view["key"], studio.options(view, {"animated": True}))
    monkeypatch.setattr(studio, "admin_artwork", lambda *_: ([poster("red"), poster("green")], []))
    original_save = Image.Image.save

    def fail_only_gif(image, destination, format=None, **params):
        if str(format).upper() == "GIF":
            raise OSError("fixture GIF encoder failure")
        return original_save(image, destination, format=format, **params)

    monkeypatch.setattr(Image.Image, "save", fail_only_gif)
    preview = studio.preview(view["key"], {}, animated=True)
    assert preview["render_info"]["reason"] == "encoder_error"
    assert preview["render_info"]["requested_format"] == "gif"
    assert_image_report(preview["mime"], base64.b64decode(preview["image"].split(",", 1)[1]), preview["render_info"])
    studio.start_generate(view["key"])
    job = wait_job(studio)
    assert (job["done"], job["failed"], job["animated"], job["static"], job["fallback"]) == (1, 0, 0, 1, 1)
    assert job["results"][0]["status"] == "generated"
    assert job["results"][0]["render_info"]["reason"] == "encoder_error"
    row = studio.state()["history"][0]
    assert row["render_info"] == job["results"][0]["render_info"]
    assert_image_report(row["mime"], studio._file("history", row["id"], ".image").read_bytes(), row["render_info"])


def test_native_backup_failure_reports_its_stage_and_no_generated_result(setup, monkeypatch):
    instance, _, source = setup
    studio = instance._cover_studio
    key = studio.load_native()[0]["key"]
    original = source.current
    module = sys.modules[type(studio).__module__]
    monkeypatch.setattr(module, "MAX_ORIGINAL_IMAGE", len(original) - 1)
    studio.start_generate(key, publish=True)
    job = wait_job(studio)
    assert (job["done"], job["failed"], job["animated"], job["static"], job["fallback"]) == (1, 1, 0, 0, 0)
    assert len(job["results"]) == 1
    result = job["results"][0]
    assert result["key"] == key and result["status"] == "failed"
    assert "备份原生库旧封面" in result["notice"]
    assert result["notice"] in job["errors"][0]
    assert source.current == original and not studio.state()["history"]
    assert all(call[0] == "GET" for call in source.calls)
