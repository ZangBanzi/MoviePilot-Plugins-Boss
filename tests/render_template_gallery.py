"""Reproducible cover gallery using original procedural artwork, never NAS images.

Run from the repository with Python and the test dependencies installed. The
posters are original geometric travel illustrations made solely for this demo.
They are not runtime fallbacks and are never mixed into a user's library.
"""
from __future__ import annotations

import io
import json
import math
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = ROOT / "plugins.v2/mediaarchiver/fonts/NotoSansSC.ttf"


def font(size):
    return ImageFont.truetype(str(FONT), size)


def gradient(top, bottom, size=(600, 900)):
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(size[1]):
        t = y / max(1, size[1] - 1)
        draw.line((0, y, size[0], y), fill=tuple(round(a * (1 - t) + b * t)
                                                 for a, b in zip(top, bottom)))
    return image


@lru_cache(maxsize=1)
def demo_artwork() -> tuple[bytes, ...]:
    """Six distinct PNG posters: alpine, coast, city, desert, forest, night."""
    palettes = [((27, 53, 87), (242, 177, 119)),
                ((21, 101, 116), (204, 238, 207)),
                ((46, 36, 73), (196, 123, 125)),
                ((78, 45, 57), (235, 181, 109)),
                ((20, 58, 62), (180, 205, 171)),
                ((18, 28, 67), (61, 93, 132))]
    names = [("远山", "ALPINE LIGHT"), ("海岸", "THE QUIET COAST"),
             ("城际", "AFTER HOURS"), ("沙海", "ENDLESS DUNES"),
             ("森林", "INTO THE GREEN"), ("星夜", "UNDER ONE SKY")]
    results = []
    for scene, (top, bottom) in enumerate(palettes):
        image = gradient(top, bottom)
        draw = ImageDraw.Draw(image)
        if scene == 0:
            draw.ellipse((350, 182, 482, 314), fill="#FFE3BD")
            draw.polygon([(0, 620), (150, 329), (218, 407), (344, 281),
                          (600, 605), (600, 900), (0, 900)], fill="#6B7F88")
            draw.polygon([(244, 411), (344, 281), (450, 415), (351, 364),
                          (316, 397), (286, 367)], fill="#F1EAD9")
            draw.polygon([(0, 653), (111, 538), (218, 610), (405, 455),
                          (600, 605), (600, 900), (0, 900)], fill="#314E64")
            draw.polygon([(0, 790), (209, 654), (330, 732), (600, 584),
                          (600, 900), (0, 900)], fill="#193348")
            draw.line([(45, 900), (173, 802), (268, 762), (223, 744)], fill="#C69D7E", width=9)
        elif scene == 1:
            draw.ellipse((370, 204, 480, 314), fill="#F5F1D5")
            draw.rectangle((0, 384, 600, 900), fill="#236977")
            for y in range(406, 855, 29):
                draw.line((int(220 + 52 * math.sin(y)), y, 600, y), fill="#599BA0", width=2)
            draw.polygon([(0, 303), (100, 347), (209, 492), (162, 601),
                          (229, 671), (166, 900), (0, 900)], fill="#BCAA7C")
            draw.polygon([(0, 282), (92, 340), (184, 481), (139, 598),
                          (191, 661), (120, 900), (0, 900)], fill="#466954")
            draw.polygon([(277, 570), (354, 570), (332, 588), (294, 588)], fill="#E2CEAB")
            draw.polygon([(320, 505), (320, 561), (282, 561)], fill="#F4EDCF")
            draw.line((322, 501, 322, 572), fill="#F4EDCF", width=3)
        elif scene == 2:
            draw.ellipse((386, 199, 479, 292), fill="#DCA6A4")
            heights = [470, 365, 533, 430, 338, 522, 393, 451, 313]
            for n, height in enumerate(heights):
                x = n * 72 - 26
                draw.rectangle((x, height, x + 65, 900), fill=["#3A344F", "#292C45", "#473E57"][n % 3])
                for wx in range(x + 12, x + 57, 15):
                    for wy in range(height + 17, 806, 25):
                        if (wx * 7 + wy * 3 + n) % 5:
                            draw.rectangle((wx, wy, wx + 5, wy + 10), fill="#D7A777" if wy % 3 else "#716C85")
            draw.polygon([(210, 900), (322, 677), (344, 677), (488, 900)], fill="#BC8581")
            draw.line((334, 736, 350, 789), fill="#E5C7A0", width=4)
            draw.line((363, 820, 383, 899), fill="#E5C7A0", width=5)
        elif scene == 3:
            draw.ellipse((361, 207, 499, 345), fill="#F9D290")
            for y, color, phase in [(470, "#D7A16B", 0), (560, "#B97D57", 1.5),
                                    (695, "#925B45", 3.3), (790, "#613E39", 4.8)]:
                points = [(x, y + 65 * math.sin(x / 159 + phase)) for x in range(-10, 611, 10)]
                draw.polygon(points + [(600, 900), (0, 900)], fill=color)
            for x, y in [(332, 561), (341, 575), (353, 590), (367, 605)]:
                draw.ellipse((x, y, x + 3, y + 7), fill="#735042")
        elif scene == 4:
            draw.ellipse((359, 206, 477, 324), fill="#D9DEB7")
            for row, y, color in [(0, 440, "#6C9181"), (1, 555, "#3A6B62"),
                                  (2, 698, "#184C49")]:
                for x in range(-75 + row * 24, 650, 95):
                    height = 198 + 45 * math.sin(x * .03)
                    draw.polygon([(x, y + 185), (x + 51, y - height),
                                  (x + 111, y + 185)], fill=color)
            draw.polygon([(218, 900), (326, 615), (345, 609), (421, 900)], fill="#96B7A0")
        else:
            for n in range(80):
                x, y = (n * 137 + 37) % 600, (n * 73 + 177) % 657
                radius = 2 if n % 7 == 0 else 1
                draw.ellipse((x, y, x + radius, y + radius), fill="#C4D4D9")
            draw.ellipse((367, 191, 491, 315), fill="#E9E2C1")
            draw.ellipse((390, 180, 506, 297), fill="#21375B")
            draw.polygon([(0, 667), (132, 501), (283, 615), (399, 438),
                          (600, 665), (600, 900), (0, 900)], fill="#29485D")
            draw.polygon([(0, 786), (152, 653), (335, 721), (490, 576),
                          (600, 643), (600, 900), (0, 900)], fill="#152E43")
            draw.polygon([(230, 858), (288, 760), (346, 858)], fill="#E0A06A")
            draw.polygon([(273, 858), (288, 788), (311, 858)], fill="#9B634B")
        title, subtitle = names[scene]
        draw.text((40, 45), title, font=font(58), fill="#F3EFE5")
        draw.text((44, 129), subtitle, font=font(15), fill="#E2DEDA")
        draw.line((44, 171, 106, 171), fill="#DED7C6", width=2)
        output = io.BytesIO()
        image.save(output, "PNG", optimize=True)
        results.append(output.getvalue())
    return tuple(results)


def render_gallery():
    from test_coverstudio import make_plugin
    from importlib import import_module
    output = ROOT / "docs/previews"
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mediaarchiver-template-gallery-") as directory:
        plugin, _, original = make_plugin(Path(directory))
        try:
            studio = plugin._cover_studio
            module = import_module(studio.__class__.__module__)
            view = dict(original, name="私人影院", total_count=128)
            cards, details = [], []
            for preset in module.PRESETS:
                options = module.normalize_options({"style": preset["id"], "resolution": 960,
                    "subtitle": "THE CINEMA COLLECTION", "source": "Primary"})
                image = studio.render_frame(view, options, list(demo_artwork()))
                filename = f"template-{preset['id']}.png"
                image.save(output / filename, "PNG", optimize=True)
                card = Image.new("RGB", (660, 438), "#121C2B")
                card.paste(image.resize((636, 358), Image.Resampling.LANCZOS), (12, 12))
                draw = ImageDraw.Draw(card)
                draw.text((17, 383), preset["name"], font=font(24), fill="#F2F3F5")
                draw.text((116, 390), preset["description"], font=font(16), fill="#A3B1C3")
                cards.append(card)
                details.append({"id": preset["id"], "name": preset["name"], "file": filename})
            gallery = Image.new("RGB", (1380, 2410), "#091019")
            draw = ImageDraw.Draw(gallery)
            draw.text((28, 30), "每一种片库，都有自己的开场。", font=font(39), fill="#F2F3F5")
            draw.text((31, 91), "媒体虚拟库 4.6.0 · 10 款真实渲染 · 原创示例素材，与用户媒体无关", font=font(20), fill="#95A9BF")
            for index, card in enumerate(cards):
                gallery.paste(card, (20 + index % 2 * 680, 150 + index // 2 * 450))
            gallery.save(output / "template-gallery.jpg", "JPEG", quality=93, optimize=True)
            long_titles = Image.new("RGB", (1300, 1280), "#091019")
            draw = ImageDraw.Draw(long_titles)
            draw.text((25, 22), "长媒体库名称 · 实际 640 × 360 渲染", font=font(27), fill="#F2F3F5")
            long_view = dict(view, name="Netflix · 综合精选（TMDB 可播）")
            for index, preset in enumerate(module.PRESETS[4:]):
                opts = module.normalize_options({"style": preset["id"], "resolution": 640})
                long_titles.paste(studio.render_frame(long_view, opts, list(demo_artwork())),
                                  (10 + index % 2 * 640, 90 + index // 2 * 397))
                draw.text((23 + index % 2 * 640, 452 + index // 2 * 397),
                          preset["name"], font=font(18), fill="#A3B1C3")
            long_titles.save(output / "template-long-titles.jpg", "JPEG", quality=93, optimize=True)
            for style in ("cinema", "filmstrip"):
                opts = module.normalize_options({"style": style, "resolution": 640, "subtitle": "THE CINEMA COLLECTION"})
                mime, payload = studio.encode(view, opts, list(demo_artwork())[:3], "gif")
                assert mime == "image/gif"
                (output / f"template-{style}.gif").write_bytes(payload)
            (output / "template-gallery.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Rendered {len(cards)} templates and 2 GIFs to {output}")
        finally:
            plugin.stop_service()


if __name__ == "__main__":
    render_gallery()
