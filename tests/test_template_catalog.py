"""Ten real layouts keep their identity, artwork movement and saved options."""
import base64
import copy
import importlib
import io

import pytest
from PIL import Image, ImageChops, ImageDraw

from test_coverstudio import plugin
from test_native_covers import origin, setup, wait_job
from render_template_gallery import demo_artwork

OLD_STYLES = ("stack", "diagonal", "wall", "minimal")
NEW_STYLES = ("cinema", "filmstrip", "triptych", "editorial", "disc", "mosaic")


def catalog(studio):
    return importlib.import_module(studio.__class__.__module__)


def test_catalog_thumbnails_defaults_and_artwork_budgets(plugin, monkeypatch):
    instance, _, _ = plugin
    studio = instance._cover_studio
    module = catalog(studio)
    monkeypatch.setattr(studio, "admin_artwork", lambda *_: pytest.fail("Catalog must not read a user's media"))
    state = studio.state()
    assert tuple(preset["id"] for preset in state["presets"]) == OLD_STYLES + NEW_STYLES
    decoded = []
    for preset in state["presets"]:
        options = module.normalize_options({"style": preset["id"]})
        assert options["style"] == preset["id"]
        assert all(options[key] == value for key, value in preset["defaults"].items())
        assert 1 <= studio.artwork_limit(dict(options, animated=False)) <= 6
        assert studio.artwork_limit(dict(options, animated=False)) == preset["artwork_count"]
        assert studio.artwork_limit(dict(options, animated=True)) == 6
        data = base64.b64decode(preset["thumbnail"].split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (320, 180) and image.format == "JPEG"
        decoded.append(data)
    assert len(set(decoded)) == 10
    for style in OLD_STYLES:
        assert module.normalize_options({"style": style}) == dict(module.DEFAULTS, style=style)


@pytest.mark.parametrize("style", NEW_STYLES)
def test_new_template_static_resolution_and_real_cyclic_gif(plugin, style):
    instance, _, original = plugin
    studio = instance._cover_studio
    module = catalog(studio)
    view = dict(original, name="Netflix · 综合精选（TMDB 可播）", total_count=268)
    options = module.normalize_options({"style": style, "resolution": 1280})
    mime, payload = studio.encode(view, options, list(demo_artwork()), "png")
    assert mime == "image/png"
    with Image.open(io.BytesIO(payload)) as image:
        assert image.format == "PNG" and image.size == (1280, 720)
    report = {}
    mime, payload = studio.encode(view, options, list(demo_artwork())[:3], "gif", report)
    assert mime == "image/gif" and report["reason"] == "animated"
    assert report["artwork_count"] == 3 and report["actual_format"] == "gif"
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (960, 540) and image.n_frames == 15 and image.info["loop"] == 0
        first = image.convert("RGB")
        image.seek(5)
        second = image.convert("RGB")
        changes = ImageChops.difference(first, second).convert("L")
        # A meaningful picture region changes; a tiny icon/progress strip fails.
        assert sum(changes.histogram()[20:]) > image.width * image.height * .08
        durations = []
        for index in range(image.n_frames):
            image.seek(index)
            durations.append(image.info["duration"])
        assert sum(durations) == 6000 and sum(duration >= 1600 for duration in durations) == 3


@pytest.mark.parametrize("style", NEW_STYLES)
def test_new_templates_empty_and_single_artwork_remain_honest_png(plugin, style):
    instance, _, original = plugin
    studio = instance._cover_studio
    options = catalog(studio).normalize_options({"style": style})
    for members, artwork, reason in [([], [], "empty_library"),
                                     (["m1"], list(demo_artwork())[:1], "single_artwork")]:
        report = {}
        view = dict(original, item_ids=members)
        mime, payload = studio.encode(view, options, artwork, "gif", report)
        assert mime == "image/png" and report["actual_format"] == "png" and report["reason"] == reason
        assert report["artwork_count"] == len(artwork)
        with Image.open(io.BytesIO(payload)) as image:
            assert image.format == "PNG" and getattr(image, "n_frames", 1) == 1
            assert image.size == (640, 360)


def test_new_and_legacy_saved_styles_roundtrip_without_changing_user_edits(plugin):
    instance, _, view = plugin
    studio = instance._cover_studio
    module = catalog(studio)
    for style in OLD_STYLES + NEW_STYLES:
        options = module.normalize_options({"style": style, "title": "我的独立封面", "text_y": 42,
            "title_size": 38, "foreground": "#E0B077", "background": "#122030"})
        studio.save_options(view["key"], options)
        saved = copy.deepcopy(studio.options(view))
        assert saved == options and saved["text_y"] == 42 and saved["foreground"] == "#E0B077"
        preset = studio.action({"action": "save_preset", "name": style, "options": saved})
        assert next(row for row in studio.config["presets"] if row["id"] == preset["id"])["options"] == saved
        snapshot = studio.action({"action": "backup"})["backup"]
        restored = instance._validate_studio_config(snapshot["config"])
        assert restored["cover_studio"]["overrides"][view["key"]] == saved
        assert module.normalize_options(saved) == saved
    assert view["item_ids"] == ["m1", "m2"]
    with pytest.raises(ValueError):
        module.normalize_options({"style": "not-in-catalog"})


@pytest.mark.parametrize("style", ["cinema", "editorial"])
def test_new_style_selected_batch_publishes_native_and_keeps_virtual_plan(setup, style):
    instance, virtual, source = setup
    studio = instance._cover_studio
    native = studio.load_native()[0]
    options = catalog(studio).normalize_options({"style": style, "animated": False})
    untouched_native = source.current_b
    keys = [native["key"], virtual["key"]]
    studio.action({"action": "generate", "server": studio.server_id(instance._gateway_client_cache),
                   "keys": keys, "options": options, "publish": True})
    job = wait_job(studio)
    assert (job["total"], job["done"], job["failed"]) == (2, 2, 0)
    assert {row["key"] for row in job["results"]} == set(keys)
    assert all(row["style"] == style for row in job["results"])
    assert set(source.posted) == {"/Items/libA/Images/Primary"}
    assert source.current_b == untouched_native
    generated = [row for row in studio._read_index("history")
                 if row.get("purpose") != "before_native_publish"]
    assert len(generated) == 2
    for row in generated:
        target = studio.view(row["key"])
        assert studio.options(target) == options
        artwork, _ = studio.admin_artwork(target, options)
        expected = studio.encode(target, options, artwork, "png")[1]
        assert studio._file("history", row["id"], ".image").read_bytes() == expected
    native_row = next(row for row in generated if row["native"])
    assert source.current == studio._file("history", native_row["id"], ".image").read_bytes()


@pytest.mark.parametrize("style", ["editorial", "disc", "mosaic"])
def test_narrow_templates_wrap_long_titles_with_legible_size_and_caption_spacing(plugin, monkeypatch, style):
    instance, _, original = plugin
    studio = instance._cover_studio
    title = "Netflix · 综合精选（TMDB 可播）"
    view = dict(original, name=title, total_count=128)
    options = catalog(studio).normalize_options({"style": style, "resolution": 960})
    artwork = list(demo_artwork())
    drawn = []
    draw_text = ImageDraw.ImageDraw.text

    def record_text(draw, xy, text, *args, **kwargs):
        font = kwargs.get("font")
        if font is not None:
            drawn.append({"text": text, "size": font.size,
                "box": draw.textbbox(xy, text, font=font, anchor=kwargs.get("anchor"))})
        return draw_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    image = studio.render_frame(view, options, artwork)
    assert image.size == (960, 540)
    main_lines = [row for row in drawn if row["size"] >= 28]
    assert len(main_lines) == 2
    assert "".join(row["text"] for row in main_lines).replace(" ", "") == title.replace(" ", "")
    assert any("TMDB" in row["text"] for row in main_lines)
    assert any("Netflix" in row["text"] for row in main_lines)
    assert main_lines[0]["box"][3] < main_lines[1]["box"][1]
    subtitle = next(row for row in drawn if row["text"] == "VIRTUAL COLLECTION")
    count = next(row for row in drawn if row["text"] == "128 部 · 持续更新")
    assert main_lines[-1]["box"][3] < subtitle["box"][1]
    assert subtitle["box"][3] < count["box"][1]
    for row in [*main_lines, subtitle, count]:
        x0, y0, x1, y1 = row["box"]
        assert 0 <= x0 < x1 <= 960 and 0 <= y0 < y1 <= 540


def test_catalog_state_survives_missing_pillow(plugin, monkeypatch):
    import builtins
    instance, _, _ = plugin
    original_import = builtins.__import__

    def without_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ModuleNotFoundError("Pillow unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pillow)
    state = instance._cover_studio.state()
    assert len(state["presets"]) == 10
    assert all(preset["thumbnail"] == "" for preset in state["presets"])
    assert state["version"] == "4.6.0" and state["config"]
