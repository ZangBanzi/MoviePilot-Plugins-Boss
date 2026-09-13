"""Large real images and metadata must not share the old 3 MiB ceiling.

All requests use the local Emby-shaped origin. Native originals are backed up
without transcoding; source posters may be resized by the cover renderer.
"""
import asyncio
import base64
import io
import random
import sys

from PIL import Image
import pytest

from test_coverstudio import poster
from test_native_covers import origin, setup, wait_job
from test_virtual_library import _FakeRequest


MIB = 1024 * 1024


@pytest.fixture(scope="module")
def large_original_gif():
    random_pixels = random.Random(451)
    frames = []
    palette = [channel for value in range(256) for channel in (value, 255 - value, value // 2)]
    for _ in range(6):
        frame = Image.frombytes("P", (960, 540), random_pixels.randbytes(960 * 540))
        frame.putpalette(palette)
        frames.append(frame)
    output = io.BytesIO()
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:],
                   duration=[300, 400, 500, 600, 700, 800], loop=0,
                   disposal=2, optimize=False)
    payload = output.getvalue()
    assert 3 * MIB < len(payload) < 16 * MIB
    with Image.open(io.BytesIO(payload)) as image:
        assert image.n_frames == 6 and image.info["loop"] == 0
    return payload


@pytest.fixture(scope="module")
def large_poster_png():
    image = Image.frombytes("RGB", (1100, 1100), random.Random(452).randbytes(1100 * 1100 * 3))
    output = io.BytesIO()
    image.save(output, "PNG", compress_level=0)
    payload = output.getvalue()
    assert 3 * MIB < len(payload) < 16 * MIB
    return payload


def test_large_native_gif_is_backed_up_before_publish_and_restored_exactly(
        setup, large_original_gif, monkeypatch):
    plugin, _, source = setup
    studio = plugin._cover_studio
    source.current, source.current_mime = large_original_gif, "image/gif"
    key = studio.load_native()[0]["key"]
    studio.save_options(key, studio.options({}, {"animated": True, "sort": "name"}))
    backups_at_upload = []
    original_post = source.do_POST

    def observe_post(handler):
        # This observation runs at the real HTTP origin, before any image changes.
        rows = [row for row in studio._read_index("history")
                if row.get("purpose") == "before_native_publish" and row["key"] == key]
        backups_at_upload.append([
            (row["mime"], studio._file("history", row["id"], ".image").read_bytes())
            for row in rows
        ])
        return original_post(handler)

    monkeypatch.setattr(source, "do_POST", observe_post)
    studio.start_generate(key, publish=True)
    assert wait_job(studio)["failed"] == 0, studio.job
    assert source.current != large_original_gif and source.current_mime == "image/gif"
    assert backups_at_upload == [[("image/gif", large_original_gif)]]
    backup = next(row for row in studio._read_index("history")
                  if row.get("purpose") == "before_native_publish")
    assert backup["size"] == len(large_original_gif)

    studio.action({"action": "restore_native_image", "id": backup["id"]})
    assert source.current == large_original_gif and source.current_mime == "image/gif"
    assert len(backups_at_upload) == 2
    with Image.open(io.BytesIO(source.current)) as restored:
        assert restored.n_frames == 6 and restored.info["loop"] == 0
        durations = []
        for frame in range(restored.n_frames):
            restored.seek(frame)
            restored.load()
            durations.append(restored.info["duration"])
        assert durations == [300, 400, 500, 600, 700, 800]


@pytest.mark.parametrize("native", [False, True])
def test_large_poster_still_contributes_to_real_gif(setup, large_poster_png, native):
    plugin, virtual, source = setup
    studio = plugin._cover_studio
    source.art = {"good": large_poster_png, "other": poster("green")}
    key = studio.load_native()[0]["key"] if native else virtual["key"]
    result = studio.preview(key, {"animated": True, "sort": "name"}, animated=True)
    assert result["artwork_count"] == 2, result.get("notices")
    assert result["mime"] == "image/gif", result.get("notices")
    with Image.open(io.BytesIO(base64.b64decode(result["image"].split(",", 1)[1]))) as image:
        assert image.n_frames > 1 and image.info["loop"] == 0
    cached_posters = [image for cache in studio.art_cache.values() for image in cache[1]]
    assert len(cached_posters) == 2
    for payload in cached_posters:
        assert len(payload) < 3 * MIB
        with Image.open(io.BytesIO(payload)) as image:
            assert image.width <= 960 and image.height <= 960


def test_gateway_large_poster_still_contributes_to_real_gif(setup, large_poster_png):
    plugin, view, source = setup
    studio = plugin._cover_studio
    source.art = {"good": large_poster_png, "other": poster("green")}
    studio.save_options(view["key"], studio.options(view, {"animated": True, "sort": "name"}))

    async def request_cover():
        return await plugin._studio_gateway_cover(
            _FakeRequest("/cover", headers={"X-Emby-Token": "alice"}), view)

    response = asyncio.run(request_cover())
    assert response.status_code == 200 and response.headers["content-type"] == "image/gif"
    with Image.open(io.BytesIO(response.body)) as image:
        assert image.n_frames > 1
    assert all(len(image) < 3 * MIB
               for cache in studio.art_cache.values() for image in cache[1])
    assert all(call[0] == "GET" for call in source.calls)


def test_original_cover_limit_preserves_image_and_never_posts(setup, monkeypatch):
    plugin, _, source = setup
    studio = plugin._cover_studio
    original = source.current
    key = studio.load_native()[0]["key"]
    module = sys.modules[type(studio).__module__]
    # Exercise the bounded transfer without creating a 65 MiB test image.
    monkeypatch.setattr(module, "MAX_ORIGINAL_IMAGE", len(original) - 1)
    studio.start_generate(key, publish=True)
    result = wait_job(studio)
    assert result["failed"] == 1 and result["errors"]
    assert source.current == original
    assert not [call for call in source.calls if call[0] == "POST"]
    assert not studio._read_index("history")


def test_native_metadata_above_three_mib_is_read_before_generating(setup):
    plugin, _, source = setup
    studio = plugin._cover_studio
    # Some Emby/proxy installations include unrequested metadata fields.
    source.items = [dict(source.items[0], Overview="x" * (3 * MIB + 1024)),
                    *source.items[-2:]]
    key = studio.load_native()[0]["key"]
    result = studio.preview(key, {"animated": True, "sort": "name"}, animated=True)
    assert result["total_count"] == 3 and result["artwork_count"] == 2
    assert result["mime"] == "image/gif"
    assert all(call[0] == "GET" for call in source.calls)
