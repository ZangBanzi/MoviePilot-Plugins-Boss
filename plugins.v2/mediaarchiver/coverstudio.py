"""Native/virtual library cover studio. No media-file writes.

Layout inspiration: justzerock/MoviePilot-Plugins (Yahaha Cover Studio).
Local persistence stays in the plugin data directory. Explicit native publishing
only updates a verified library's Primary image through Emby's documented API.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import ipaddress
import json
import math
import re
import socket
import threading
import time
import uuid
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


PRESETS = (
    {"id": "stack", "name": "叠影", "description": "旋转海报 · 柔和层次"},
    {"id": "diagonal", "name": "光幕", "description": "斜切画面 · 影院氛围"},
    {"id": "wall", "name": "映墙", "description": "多幅海报 · 丰富片库"},
    {"id": "minimal", "name": "留白", "description": "居中标题 · 纯粹表达"},
)
DEFAULTS = {
    "style": "stack", "animated": True, "resolution": 640,
    "source": "Backdrop", "sort": "random", "seed": 0,
    "title": "", "subtitle": "VIRTUAL COLLECTION", "text": "{count} 部 · 持续更新",
    "title_font": "default", "subtitle_font": "default", "text_font": "default",
    "title_size": 66, "subtitle_size": 20, "text_size": 17,
    "text_x": 7, "text_y": 38, "image_x": 58, "image_y": 16,
    "image_scale": 100, "blur": 28, "overlay": 52,
    "accent": "", "background": "", "foreground": "#F5F7FF", "show_count": True,
}
NUMBERS = {
    "resolution": (640, 1920), "seed": (0, 999999),
    "title_size": (24, 110), "subtitle_size": (10, 50), "text_size": (10, 36),
    "text_x": (2, 75), "text_y": (10, 70), "image_x": (5, 70), "image_y": (2, 50),
    "image_scale": (50, 125), "blur": (0, 60), "overlay": (15, 90),
}
MAX_IMAGE = 16 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
MAX_ORIGINAL_IMAGE = 64 * 1024 * 1024
MAX_FONT = 24 * 1024 * 1024


def normalize_options(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("封面参数必须是对象")
    result = dict(DEFAULTS)
    for key, value in raw.items():
        if key not in DEFAULTS:
            continue
        if key in NUMBERS:
            try:
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                raise ValueError(f"{key} 必须是有效数字") from None
            low, high = NUMBERS[key]
            result[key] = round(max(low, min(high, number)))
        elif isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是开关值")
            result[key] = value
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是文本")
            result[key] = value.strip()[:160]
    if result["style"] not in {preset["id"] for preset in PRESETS}:
        raise ValueError("未知封面方案")
    if result["source"] not in {"Backdrop", "Primary", "brand"}:
        raise ValueError("未知海报来源")
    if result["sort"] not in {"random", "latest", "name"}:
        raise ValueError("未知素材排序")
    if result["resolution"] not in {640, 960, 1280, 1920}:
        raise ValueError("分辨率请选择 360p / 540p / 720p / 1080p")
    for key in ("accent", "background", "foreground"):
        if result[key] and not re.fullmatch(r"#[0-9a-fA-F]{6}", result[key]):
            raise ValueError("颜色需要使用 #RRGGBB")
    for key in ("title_font", "subtitle_font", "text_font"):
        if result[key] != "default" and not re.fullmatch(r"[0-9a-f]{32}", result[key]):
            raise ValueError("无效字体编号")
    return result


def data_uri(payload: bytes, mime: str) -> str:
    return "data:" + mime + ";base64," + base64.b64encode(payload).decode("ascii")


def decode_image(payload: bytes):
    from PIL import Image
    if len(payload) > MAX_IMAGE:
        raise ValueError(f"海报超过 {MAX_IMAGE // (1024 * 1024)} MiB")
    with Image.open(io.BytesIO(payload)) as source:
        if source.width * source.height > 16_000_000:
            raise ValueError("海报像素过大")
        source.thumbnail((960, 960))
        return source.convert("RGB")


def normalize_artwork(payload: bytes) -> bytes:
    # Cache a bounded still image, not a potentially large upstream GIF/PNG.
    # Original library backups must never pass through this conversion.
    picture = decode_image(payload)
    output = io.BytesIO()
    picture.save(output, "JPEG", quality=90)
    return output.getvalue()


class CoverStudio:
    engine_version = "4.5.1"
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.RLock()
        self.render_lock = threading.Lock()
        self.job_lock = threading.Lock()
        self.cancel = threading.Event()
        self.job = {"running": False, "done": 0, "total": 0, "message": "尚未生成"}
        self.art_cache: dict = {}
        self.font_cache: dict = {}
        self.glyph_cache: dict = {}
        self.thumbs: dict = {}
        self.native_views: list = []
        self.native_owner = ""
        self._client_context = threading.local()

    @staticmethod
    def server_id(client) -> str:
        return hashlib.sha256(client.api_root.encode()).hexdigest()[:16]

    def clients(self) -> list:
        try:
            return self.plugin._create_clients()
        except Exception:
            cached = self.plugin._gateway_client_cache
            return [(cached, self.plugin._gateway_server_name or "Emby")] if cached else []

    def client(self):
        return getattr(self._client_context, "client", None) or self.plugin._gateway_client()

    @contextmanager
    def on_server(self, identifier: str = "", client=None):
        previous = getattr(self._client_context, "client", None)
        if client is None and identifier:
            client = next((c for c, _ in self.clients() if self.server_id(c) == identifier), None)
            if client is None:
                raise ValueError("Emby 服务器已移除或连接配置不可用，请刷新服务器列表")
        self._client_context.client = client or previous
        try:
            yield
        finally:
            self._client_context.client = previous

    def servers(self) -> list[dict]:
        gateway = self.plugin._gateway_client_cache
        pairs = self.clients()
        gateway = gateway or (pairs[0][0] if pairs else None)
        return [{"id": self.server_id(c), "name": name, "gateway": bool(gateway and c.api_root == gateway.api_root)}
                for c, name in pairs]

    @property
    def root(self) -> Path:
        override = getattr(self.plugin, "_studio_data_dir", None)
        return Path(override) if override else Path(self.plugin.get_data_path()) / "cover_studio"

    @property
    def config(self) -> dict:
        value = self.plugin._saved_config.get("cover_studio") or {}
        return value if isinstance(value, dict) else {}

    def options(self, view: Mapping, patch: dict | None = None) -> dict:
        cfg = self.config
        opts = dict(cfg.get("defaults") or {})
        opts.update((cfg.get("overrides") or {}).get(str(view.get("key")), {}))
        if patch is not None:
            if not isinstance(patch, dict):
                raise ValueError("封面参数必须是对象")
            opts.update(patch)
        return normalize_options(opts)

    def fingerprint(self, key: str, options: dict | None = None) -> str:
        opts = options if options is not None else self.options({"key": key})
        return hashlib.sha256(json.dumps(opts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]

    def _file(self, folder: str, identifier: str, suffix: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", str(identifier)):
            raise ValueError("无效文件编号")
        root = (self.root / folder).resolve()
        target = (root / (identifier + suffix)).resolve()
        if target.parent != root:
            raise ValueError("文件超出插件数据目录")
        return target

    @staticmethod
    def _atomic(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_index(self, name: str) -> list:
        path = self.root / (name + ".json")
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("封面工坊索引损坏，请从备份恢复")
        return value

    def _write_index(self, name: str, value: list) -> None:
        self._atomic(self.root / (name + ".json"), json.dumps(value, ensure_ascii=False).encode())

    def _save_studio(self, cfg: dict) -> None:
        merged = copy.deepcopy(self.plugin._saved_config)
        merged["cover_studio"] = cfg
        self.plugin.update_config(merged)
        self.plugin._saved_config = merged
        self.plugin._refresh_cover_tags()

    def save_options(self, key: str, options: dict, global_scope: bool = False) -> dict:
        if self.job_lock.locked():
            raise ValueError("封面正在生成，完成或停止后再保存方案")
        options = normalize_options(options)
        with self.lock:
            cfg = copy.deepcopy(self.config)
            if global_scope:
                cfg["defaults"] = options
            else:
                self.view(key)
                overrides = cfg.setdefault("overrides", {})
                overrides[key] = options
            self._save_studio(cfg)
        return options

    def font_path(self, identifier: str) -> Path:
        if identifier != "default":
            path = self._file("fonts", identifier, ".ttf")
            if path.is_file():
                return path
        return Path(__file__).parent / "fonts" / "NotoSansSC.ttf"

    def font(self, identifier: str, size: int):
        from PIL import ImageFont
        key = (identifier, size)
        with self.lock:
            if key in self.font_cache:
                return self.font_cache[key]
        path = self.font_path(identifier)
        try:
            result = ImageFont.truetype(str(path), size=size)
            if identifier == "default":
                result.set_variation_by_axes([700 if size >= 32 else 500])
        except OSError:
            result = self.plugin._pillow_font(ImageFont, size)
        with self.lock:
            if len(self.font_cache) >= 128:
                self.font_cache.pop(next(iter(self.font_cache)))
            self.font_cache[key] = result
        return result

    def text_font_id(self, identifier: str, text: str) -> str:
        if identifier == "default":
            return identifier
        try:
            from fontTools.ttLib import TTFont
            with self.lock:
                if identifier not in self.glyph_cache:
                    with TTFont(str(self.font_path(identifier)), fontNumber=0, lazy=True) as font:
                        if len(self.glyph_cache) >= 20:
                            self.glyph_cache.pop(next(iter(self.glyph_cache)))
                        self.glyph_cache[identifier] = set(font.getBestCmap() or {})
                supported = self.glyph_cache[identifier]
            return identifier if all(char.isspace() or ord(char) in supported for char in text) else "default"
        except Exception:
            return "default"

    def render_frame(self, view: Mapping, options: dict, artwork: list[bytes] | None = None,
                     phase: float = 0):
        from PIL import Image, ImageDraw, ImageFilter, ImageOps
        # Draw at one reference size, then resize once. Layout coordinates are percentages.
        width, height = 960, 540
        theme = self.plugin._cover_theme(view)
        accent = self.plugin._hex_rgb(options["accent"] or theme["accent"])
        background = self.plugin._hex_rgb(options["background"] or theme["bg2"])
        foreground = self.plugin._hex_rgb(options["foreground"])
        images = []
        for payload in (artwork or [])[:9]:
            try:
                images.append(payload if isinstance(payload, Image.Image) else decode_image(payload))
            except Exception:
                continue
        base = Image.new("RGB", (width, height), background)
        if images:
            base = ImageOps.fit(images[0], (width, height)).filter(ImageFilter.GaussianBlur(options["blur"]))
            base = Image.blend(base, Image.new("RGB", base.size, background), .30)
        else:
            draw = ImageDraw.Draw(base)
            for y in range(height):
                factor = y / height
                color = tuple(int(c * (.42 + .35 * factor)) for c in background)
                draw.line((0, y, width, y), fill=color)
            glow = Image.new("RGB", (240, 135))
            gd = ImageDraw.Draw(glow)
            gd.ellipse((110, -60, 275, 130), fill=tuple(int(c * .55) for c in accent))
            glow = glow.filter(ImageFilter.GaussianBlur(32)).resize(base.size)
            base = Image.blend(base, glow, .5)
        veil = Image.new("RGB", base.size, (5, 10, 18))
        base = Image.blend(base, veil, options["overlay"] / 130)
        base = base.convert("RGBA")
        style = options["style"]

        def poster(index: int, size: tuple[int, int]):
            if images:
                return ImageOps.fit(images[index % len(images)], size).convert("RGBA")
            # Intentional branded artwork when the library has no usable images.
            p = Image.new("RGB", size, background)
            pd = ImageDraw.Draw(p)
            sw, sh = size
            for row in range(sh):
                blend = row / max(sh - 1, 1)
                color = tuple(int(background[i] * (1 - blend) + accent[i] * .50 * blend) for i in range(3))
                pd.line((0, row, sw, row), fill=color)
            pd.ellipse((sw*.12, sh*.13, sw*.88, sh*.89), outline=accent, width=max(1, sw//140))
            pd.ellipse((sw*.24, sh*.25, sw*.76, sh*.77), outline=accent, width=max(1, sw//180))
            pd.line((sw*.12, sh*.80, sw*.88, sh*.20), fill=accent, width=max(1, sw//140))
            return p.convert("RGBA")

        def card(picture, x, y, angle=0, radius=20, opacity=1):
            mask = Image.new("L", picture.size)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, picture.width-1, picture.height-1), radius, fill=round(255*opacity))
            picture = picture.copy()
            picture.putalpha(mask)
            picture = picture.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
            shadow = Image.new("RGBA", picture.size, (0, 0, 0, 0))
            shadow.putalpha(picture.getchannel("A").point(lambda a: round(a * .45)))
            shadow = shadow.filter(ImageFilter.GaussianBlur(12))
            base.alpha_composite(shadow, (round(x+10), round(y+16)))
            base.alpha_composite(picture, (round(x), round(y)))

        x, y = options["image_x"] * 9.6, options["image_y"] * 5.4
        scale = options["image_scale"] / 100
        if style == "stack":
            side = round(302 * scale)
            card(poster(2, (side, side)), x-34, y-26, -17, 26, .28)
            card(poster(1, (side, side)), x-13, y-12, -8, 26, .55)
            card(poster(0, (side, side)), x, y, 0, 26)
        elif style == "diagonal":
            image = poster(0, (width, height))
            mask = Image.new("L", (width, height))
            left = min(800, max(200, x-45))
            ImageDraw.Draw(mask).polygon([(left+80, 0), (width, 0), (width, height), (left-70, height)], fill=235)
            image.putalpha(mask)
            base.alpha_composite(image)
            ImageDraw.Draw(base).line((left+80, 0, left-70, height), fill=(*accent, 220), width=3)
        elif style == "wall":
            pw, ph = round(143*scale), round(208*scale)
            for col in range(3):
                for row in range(3):
                    card(poster(row+col*3, (pw, ph)), x + col*(pw+14) - row*32,
                         y-180 + row*(ph+20), -11, 14)
            fade = Image.new("RGBA", (width, height))
            fd = ImageDraw.Draw(fade)
            for fx in range(round(x+100)):
                fd.line((fx, 0, fx, height), fill=(5, 10, 18, round(240*(1-fx/(x+100)))))
            base.alpha_composite(fade)
        else:
            # Centered typography, subtle outline and cinematic lighting.
            ImageDraw.Draw(base).rounded_rectangle((32, 32, 928, 508), radius=26,
                                                   outline=(*accent, 100), width=1)

        draw = ImageDraw.Draw(base)
        title = options["title"] or str(view.get("name") or "媒体虚拟库")
        subtitle = options["subtitle"]
        count = view.get("total_count", len(view.get("item_ids") or []))
        text = options["text"].replace("{count}", str(count)).replace("{name}", str(view.get("name") or ""))
        title_x = round(options["text_x"] * 9.6)
        title_y = round(options["text_y"] * 5.4)
        centered = style == "minimal"
        max_width = min(850 if centered else 475, 920-title_x)
        if centered:
            title_x = width//2
        anchor = "mt" if centered else "lt"

        def text_line(value, ypos, size, font_id, color, limit=max_width):
            if not value:
                return 0
            font_id = self.text_font_id(font_id, value)
            # Shrink long titles before applying an ellipsis; never draw past the safe text area.
            font = self.font(font_id, size)
            while draw.textlength(value, font=font) > limit and size > 18:
                size -= 2
                font = self.font(font_id, size)
            if draw.textlength(value, font=font) > limit:
                while value and draw.textlength(value + "…", font=font) > limit:
                    value = value[:-1]
                value += "…"
            draw.text((title_x, ypos), value, font=font, fill=color, anchor=anchor,
                      stroke_width=0)
            return size

        label = "BOSS / MEDIA LIBRARY" if view.get("native") else "BOSS / VIRTUAL LIBRARY"
        text_line(label, max(24, title_y-58), 13, "default", (*accent, 255))
        used = text_line(title, title_y, options["title_size"], options["title_font"], foreground)
        text_line(subtitle, title_y+used+25, options["subtitle_size"], options["subtitle_font"], (*accent, 255))
        if options["show_count"]:
            text_line(text, min(476, title_y+used+98), options["text_size"], options["text_font"], (196, 208, 226))
        result = base.convert("RGB")
        target_width = options["resolution"]
        if target_width != width:
            result = result.resize((target_width, target_width*9//16), Image.Resampling.LANCZOS)
        return result

    @staticmethod
    def output_info(view, options, count, requested, actual, gif_error=False, format_error=False):
        if actual == "gif":
            reason, message = "animated", f"使用 {min(count, 6)} 幅不同的本库海报循环播放。"
        elif gif_error:
            reason, message = "encoder_error", "GIF 编码未完成，本次已回退 PNG；可降低分辨率后重试。"
        elif options["source"] == "brand":
            reason, message = "brand", "已选择品牌画面，输出静态封面；切换为本库海报后可生成轮播。"
        elif not count and not view.get("total_count", len(view.get("item_ids", []))):
            reason, message = "empty_library", "当前库 0 部影片，没有本库海报可轮播，已生成静态品牌封面；需先匹配到本库影片。"
        elif not count:
            reason, message = "no_artwork", "本库有影片，但未取得可解码海报，已回退静态品牌封面；请检查海报、连接和访问权限后重试。"
        elif options["animated"] and count == 1:
            reason, message = "single_artwork", "仅取得 1 幅不同的本库海报，已生成静态封面；GIF 轮播至少需要 2 幅不同海报。"
        elif format_error:
            reason, message = "format_fallback", "所选图片格式编码未完成，已回退 PNG。"
        elif options["animated"]:
            reason, message = "static_preview", f"已取得 {count} 幅不同的本库海报，当前为静态预览，可播放 GIF。"
        else:
            reason, message = "static", "当前方案使用静态模式。"
        return {"requested_format": requested, "actual_format": actual, "artwork_count": count,
                "reason": reason, "message": message}

    def encode(self, view: Mapping, options: dict, artwork=None, image_format="png", report=None) -> tuple[str, bytes]:
        from PIL import Image
        # Serialize expensive cold encodes without holding the synchronization or gateway lock.
        with self.render_lock:
            output = io.BytesIO()
            requested, gif_error, format_error = image_format, False, False
            def finish(fmt):
                if report is not None:
                    report.update(self.output_info(view, options, len(pictures), requested, fmt, gif_error, format_error))
                return "image/" + fmt, output.getvalue()
            # Only distinct, decodable library pictures count as slides. An empty
            # library or one picture is honestly static, never a synthetic loading bar.
            pictures, seen = [], set()
            for payload in (artwork or [])[:9]:
                try:
                    picture = decode_image(payload)
                    digest = hashlib.sha256(picture.resize((32, 32)).tobytes()).digest()
                    if digest not in seen:
                        pictures.append(picture)
                        seen.add(digest)
                except Exception:
                    continue
            if image_format == "gif" and len(pictures) > 1:
                try:
                    # Render each composition once, then dissolve between them.
                    # Four 80 ms transition frames + 1680 ms hold = two seconds
                    # per picture, including the last-to-first transition.
                    animated_opts = dict(options, resolution=min(960, options["resolution"]))
                    slides = [self.render_frame(view, animated_opts, pictures[i:]+pictures[:i])
                              for i in range(min(6, len(pictures)))]
                    # One palette across the cycle prevents color flicker at slide boundaries.
                    atlas = Image.new("RGB", (160*len(slides), 90))
                    for i, slide in enumerate(slides):
                        atlas.paste(slide.resize((160, 90)), (160*i, 0))
                    palette = atlas.quantize(colors=256)
                    frames, durations = [], []
                    for i, slide in enumerate(slides):
                        frames.append(slide.quantize(palette=palette, dither=Image.Dither.NONE))
                        durations.append(1680)
                        for alpha in (.104, .352, .648, .896):
                            frames.append(Image.blend(slide, slides[(i+1) % len(slides)], alpha)
                                          .quantize(palette=palette, dither=Image.Dither.NONE))
                            durations.append(80)
                    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                                   duration=durations, loop=0, disposal=2, optimize=False)
                    return finish("gif")
                except Exception:
                    gif_error = True
                    output = io.BytesIO()
                    image_format = "png"
            fmt = image_format if image_format in {"png", "jpeg", "webp"} else "png"
            base = self.render_frame(view, options, pictures)
            try:
                base.save(output, format=fmt.upper(), quality=88, optimize=True)
            except Exception:
                format_error = True
                output = io.BytesIO()
                base.save(output, format="PNG")
                fmt = "png"
            return finish(fmt)

    def select_ids(self, view: Mapping, options: dict) -> list[str]:
        ids = list(dict.fromkeys(str(x) for x in view.get("item_ids", [])))
        index = view.get("_items") or self.plugin._proxy_item_index
        if options["sort"] == "latest":
            ids.sort(key=lambda x: str(index.get(x, {}).get("DateCreated") or ""), reverse=True)
        elif options["sort"] == "name":
            ids.sort(key=lambda x: str(index.get(x, {}).get("SortName") or index.get(x, {}).get("Name") or x))
        else:
            ids.sort(key=lambda x: hashlib.sha256(f"{options['seed']}|{x}".encode()).digest())
        # Prefer known artwork across the entire membership before limiting API work.
        ids.sort(key=lambda x: self.image_priority(index.get(x, {}), options))
        return ids[:48]

    @staticmethod
    def image_priority(item: Mapping, options: dict) -> int:
        if options["source"] == "Backdrop" and item.get("BackdropImageTags"):
            return 0
        if (item.get("ImageTags") or {}).get("Primary"):
            return 1
        return 2

    @staticmethod
    def image_kinds(options: dict) -> list[str]:
        return ["Backdrop", "Primary"] if options["source"] == "Backdrop" else ["Primary"]

    def _admin_request(self, method: str, path: str, query=None, payload=None, mime=None,
                       max_bytes=None, purpose="读取 Emby 元数据"):
        import httpx
        client = self.client()
        headers = {"X-Emby-Token": client.api_key}
        if mime:
            headers["Content-Type"] = mime
        limit = MAX_JSON if max_bytes is None else max_bytes
        try:
            with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as http:
                with http.stream(method, client.api_root.rstrip("/")+path, params=query,
                                 content=payload, headers=headers) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64*1024):
                        if len(body) + len(chunk) > limit:
                            raise ValueError(f"{purpose}：Emby 返回内容超过 {limit / (1024 * 1024):g} MiB，已停止操作")
                        body.extend(chunk)
                    return response.status_code, response.headers.get("content-type", "").split(";")[0].strip().lower(), bytes(body)
        except httpx.HTTPError:
            raise ValueError(f"{purpose}：Emby 请求未完成，请检查连接和服务器状态") from None

    def _admin_json(self, path: str, query=None):
        status, _, body = self._admin_request("GET", path, query)
        if status != 200:
            raise ValueError(f"读取 Emby 元数据失败：HTTP {status}")
        value = json.loads(body)
        if not isinstance(value, dict) or not isinstance(value.get("Items"), list):
            raise ValueError("Emby 元数据格式无效")
        return value

    def load_native(self) -> list[dict]:
        owner = self.server_id(self.client())
        try:
            return self._read_native()
        except Exception:
            # Failed refreshes invalidate targets; successful refreshes replace
            # the snapshot atomically so a concurrent preview never sees an empty list.
            with self.lock:
                self.native_views = [v for v in self.native_views if v.get("server") != owner]
            raise

    def _read_native(self) -> list[dict]:
        client = self.client()
        owner = hashlib.sha256(client.api_root.encode()).hexdigest()[:16]
        rows, start, legacy = [], 0, False
        for _ in range(20):
            status, _, body = self._admin_request("GET", "/Library/VirtualFolders/Query", {"StartIndex": start, "Limit": 100})
            if status in {404, 405} and start == 0:
                status, _, body = self._admin_request("GET", "/Library/VirtualFolders")
                legacy = True
            if status != 200:
                raise ValueError(f"读取原生媒体库失败：HTTP {status}")
            payload = json.loads(body)
            items = payload if isinstance(payload, list) else payload.get("Items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("原生媒体库列表格式无效")
            for item in items:
                if not isinstance(item, dict):
                    continue
                identifier = str(item.get("Id") or item.get("ItemId") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
                    continue
                rows.append({"key": f"native:{owner}:{identifier}", "id": identifier, "native": True,
                             "name": str(item.get("Name") or identifier)[:160], "item_ids": [], "server": owner})
            start += len(items)
            if legacy or isinstance(payload, list) or not items or start >= int(payload.get("TotalRecordCount", start)):
                break
        else:
            raise ValueError("原生媒体库列表超过分页上限，请缩小服务器范围")
        rows = list({row["key"]: row for row in rows}.values())
        with self.lock:
            self.native_views = [v for v in self.native_views if v.get("server") != owner] + rows
            self.native_owner = owner
        return rows

    def native_items(self, view: Mapping, options: dict) -> dict:
        sort = {"latest": "DateCreated", "name": "SortName", "random": "Random"}[options["sort"]]
        query = {"ParentId": view["id"], "Recursive": "true", "IncludeItemTypes": "Movie,Series,Video",
                 "SortBy": sort, "SortOrder": "Ascending" if options["sort"] == "name" else "Descending",
                 "Fields": "DateCreated,SortName", "EnableImages": "true", "Limit": 48}
        payload = self._admin_json("/Items", query)
        items = [item for item in payload["Items"] if isinstance(item, dict) and item.get("Id")]
        total = max(0, int(payload.get("TotalRecordCount", len(items))))
        # Ask Emby for images across the library, so missing posters at the front
        # of a large library do not cause a false brand fallback.
        for kind in self.image_kinds(options) if options["source"] != "brand" else []:
            extra = self._admin_json("/Items", dict(query, ImageTypes=kind))
            items.extend(item for item in extra["Items"] if isinstance(item, dict) and item.get("Id"))
        index = {str(item["Id"]): item for item in items}
        return dict(view, item_ids=list(index), _items=index, total_count=total)

    def _upload_native(self, view: Mapping, mime: str, payload: bytes, batch: str = "") -> None:
        # Revalidate the exact target against the configured server; names never identify libraries.
        target = next((v for v in self.load_native() if v["key"] == view["key"]), None)
        if target is None:
            raise ValueError("原生媒体库已移除或服务器已变更，未上传")
        path = f"/Items/{target['id']}/Images/Primary"
        status, old_mime, old = self._admin_request("GET", path, max_bytes=MAX_ORIGINAL_IMAGE,
                                                  purpose="备份原生库旧封面（失败时不上传）")
        if status == 200:
            # Validate the first frame without resizing/re-encoding the backup.
            # GIF bytes (including all frames, loop and durations) stay exact.
            from PIL import Image
            try:
                with Image.open(io.BytesIO(old)) as source:
                    old_mime = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif",
                                "WEBP": "image/webp"}.get(source.format)
                    if not old_mime or source.width * source.height > 16_000_000:
                        raise ValueError()
                    source.load()
            except Exception:
                raise ValueError("备份原生库旧封面：图片格式或像素无效，未上传") from None
            with self.lock:
                row = self._history_entry(target, self.options(target), old_mime, old, batch or uuid.uuid4().hex)
                row["purpose"] = "before_native_publish"
                self._commit_history([row]+self._read_index("history"))
        elif status != 404:
            raise ValueError(f"无法备份原生库旧封面：HTTP {status}，未上传")
        status, _, _ = self._admin_request("POST", path, payload=base64.b64encode(payload), mime=mime,
                                          purpose="上传原生库封面")
        if status not in {200, 204}:
            raise ValueError(f"原生库封面上传失败：HTTP {status}；原图备份可从历史下载")

    def admin_artwork(self, view: Mapping, options: dict) -> tuple[list[bytes], list[str]]:
        if view.get("native"):
            view.update(self.native_items(view, options))
        if options["source"] == "brand" or not view.get("item_ids"):
            return [], []
        import httpx
        client = self.client()
        cache_key = ("admin", client.api_root, hashlib.sha256(client.api_key.encode()).hexdigest(),
                     str(view.get("key")), self.plugin._cover_tag(str(view.get("key")), view.get("item_ids", []), options))
        with self.lock:
            cached = self.art_cache.get(cache_key)
            if cached and time.monotonic()-cached[0] < 60:
                return list(cached[1]), list(cached[2])
        ids = self.select_ids(view, options)
        index = view.get("_items") or self.plugin._proxy_item_index
        if not view.get("native"):
            try:
                metadata = self._admin_json("/Items", {"Ids": ",".join(ids), "Limit": len(ids),
                    "Fields": "DateCreated,SortName", "EnableImages": "true"})
                index = {str(item.get("Id")): item for item in metadata["Items"] if isinstance(item, dict)}
            except ValueError:
                pass  # Old Emby installations may omit image metadata; image GET remains authoritative.
        ids.sort(key=lambda identifier: self.image_priority(index.get(identifier, {}), options))
        images, notices, seen, failures = [], [], set(), set()
        wanted = self.artwork_limit(options)
        deadline = time.monotonic()+12
        with httpx.Client(timeout=3, follow_redirects=False, trust_env=False,
                          headers={"X-Emby-Token": client.api_key}) as http:
            for item_id in ids:
                if time.monotonic() > deadline or len(images) >= wanted:
                    break
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item_id):
                    continue
                for kind in self.image_kinds(options):
                    try:
                        url = client.api_root.rstrip("/")+f"/Items/{item_id}/Images/{kind}"
                        with http.stream("GET", url, params={"MaxWidth": 720, "MaxHeight": 720, "Format": "jpg"}) as response:
                            if response.status_code != 200:
                                if response.status_code != 404:
                                    failures.add(f"海报接口 HTTP {response.status_code}")
                                continue
                            chunks = bytearray()
                            for chunk in response.iter_bytes(chunk_size=64*1024):
                                if len(chunks) + len(chunk) > MAX_IMAGE:
                                    raise ValueError(f"海报超过 {MAX_IMAGE // (1024 * 1024)} MiB")
                                if time.monotonic() > deadline:
                                    raise TimeoutError()
                                chunks.extend(chunk)
                        picture = normalize_artwork(bytes(chunks))
                        digest = hashlib.sha256(picture).digest()
                        if digest not in seen:
                            images.append(picture)
                            seen.add(digest)
                        break
                    except (TimeoutError, httpx.TimeoutException):
                        failures.add("海报读取超时")
                    except ValueError as exc:
                        failures.add(str(exc))
                    except Exception:
                        failures.add("海报读取或解码失败")
        if len(images) < min(2, wanted) and failures:
            notices.append("部分素材不可用：" + "；".join(sorted(failures)))
        if not images:
            notices.append("未取得可用 Emby 海报，已使用品牌画面。")
        with self.lock:
            if len(self.art_cache) >= 32:
                self.art_cache.pop(next(iter(self.art_cache)))
            self.art_cache[cache_key] = (time.monotonic(), images, notices)
        return images, notices

    @staticmethod
    def artwork_limit(options: dict) -> int:
        return 6 if options["animated"] else {"wall": 6, "stack": 3}.get(options["style"], 1)

    def view(self, key: str) -> dict:
        if key.startswith("native:"):
            client = self.client()
            owner = hashlib.sha256(client.api_root.encode()).hexdigest()[:16]
            with self.lock:
                native = next((dict(v) for v in self.native_views if v["key"] == key), None)
            if native is None or not key.startswith(f"native:{owner}:"):
                raise ValueError("请先读取原生媒体库并选择有效目标")
            return native
        context = getattr(self._client_context, "client", None)
        if key and context and context.api_root != self.plugin._gateway_client().api_root:
            raise ValueError("虚拟库属于当前播放网关服务器，不能跨服务器取材")
        with self.plugin._proxy_lock:
            view = next((dict(v) for v in self.plugin._virtual_views.values() if v.get("key") == key), None)
        if view is None:
            if key:
                raise ValueError("虚拟库已不存在，请刷新页面")
            view = {"key": "attribute:remux", "name": "Remux 专区", "item_ids": []}
        return view

    def preview(self, key: str, patch: dict, animated=False) -> dict:
        view = self.view(key)
        options = self.options(view, patch)
        art, notices = self.admin_artwork(view, options)
        report = {}
        mime, payload = self.encode(view, options, art, "gif" if animated and options["animated"] else "png", report)
        return {"image": data_uri(payload, mime), "mime": mime, "notices": notices,
                "artwork_count": report["artwork_count"], "render_info": report,
                "options": options, "sample": not bool(key),
                "total_count": view.get("total_count", len(view.get("item_ids", [])))}

    def _history_entry(self, view: dict, options: dict, mime: str, payload: bytes, batch: str) -> dict:
        identifier = uuid.uuid4().hex
        self._atomic(self._file("history", identifier, ".image"), payload)
        from PIL import Image
        with Image.open(io.BytesIO(payload)) as source:
            thumb = source.convert("RGB")
            thumb.thumbnail((320, 180))
            output = io.BytesIO()
            thumb.save(output, "JPEG", quality=75)
        self._atomic(self._file("history", identifier, ".jpg"), output.getvalue())
        return {"id": identifier, "batch": batch, "key": view["key"], "name": view["name"], "native": bool(view.get("native")),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "mime": mime,
                "size": len(payload), "options": options}

    def _trim_history(self, entries: list) -> list:
        limit = max(1, min(100, int(self.config.get("history_limit", 30))))
        batches = list(dict.fromkeys(row["batch"] for row in entries))[:limit]
        keep, size = [], 0
        for row in entries:
            size += row["size"]
            if row["batch"] in batches and size <= 256*1024*1024:
                keep.append(row)
        return keep

    def _commit_history(self, entries: list) -> None:
        keep = self._trim_history(entries)
        self._write_index("history", keep)
        retained_ids = {row["id"] for row in keep}
        for row in entries:
            if row["id"] not in retained_ids:
                for suffix in (".image", ".jpg"):
                    self._file("history", row["id"], suffix).unlink(missing_ok=True)

    def start_generate(self, key: str = "", publish: bool = False, server: str = "") -> dict:
        if publish and not server and not key.startswith("native:"):
            raise ValueError("更新原生库封面必须指定一个已读取的原生媒体库")
        client = self.client() if server or key.startswith("native:") else None
        if server:
            views = [dict(v) for v in self.load_native()]
            gateway = self.plugin._gateway_client_cache
            if gateway and gateway.api_root == client.api_root:
                with self.plugin._proxy_lock:
                    views.extend(dict(v) for v in self.plugin._virtual_views.values())
        elif key:
            views = [self.view(key)]
        else:
            with self.plugin._proxy_lock:
                views = [dict(v) for v in self.plugin._virtual_views.values()]
        if not views:
            raise ValueError("没有可生成的虚拟库，请先保存配置并一键重建")
        if not self.job_lock.acquire(blocking=False):
            raise ValueError("封面生成正在进行中")
        self.cancel.clear()
        self.job = {"running": True, "done": 0, "total": len(views), "failed": 0,
                    "animated": 0, "static": 0, "fallback": 0, "message": "准备生成"}

        def work():
            self._client_context.client = client
            batch = uuid.uuid4().hex
            rows = []
            try:
                for view in views:
                    if self.cancel.is_set() or self.plugin._stopping:
                        break
                    try:
                        options = self.options(view)
                        art, notices = self.admin_artwork(view, options)
                        report = {}
                        mime, payload = self.encode(view, options, art, "gif" if options["animated"] else "png", report)
                        if publish and view.get("native"):
                            if options["source"] != "brand" and not art:
                                raise ValueError("未匹配到海报，保留原生库现有封面；可先预览或选择纯品牌画面")
                            self._upload_native(view, mime, payload, batch)
                        if self.config.get("history_enabled", True):
                            with self.lock:
                                row = self._history_entry(view, options, mime, payload, batch)
                                row["render_info"] = report
                                rows.append(row)
                        self.job["animated" if mime == "image/gif" else "static"] += 1
                        if report["requested_format"] == "gif" and mime != "image/gif":
                            self.job["fallback"] += 1
                        self.job.setdefault("results", []).append({"key": view["key"], "name": view["name"],
                            "native": bool(view.get("native")), "artwork_count": report["artwork_count"], "mime": mime,
                            "render_info": report,
                            "status": "published" if publish and view.get("native") else "generated",
                            "notice": "；".join(notices)})
                        self.job["message"] = view["name"] + (" · 已生成 GIF" if mime == "image/gif" else " · 已生成静态封面")
                    except Exception as exc:
                        self.job["failed"] += 1
                        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                        self.job.setdefault("errors", []).append(f"{view['name']} · {detail}")
                        self.job.setdefault("results", []).append({"key": view["key"], "name": view["name"],
                            "native": bool(view.get("native")), "status": "failed", "notice": detail})
                        self.job["message"] = f"{view['name']} · 生成失败"
                    self.job["done"] += 1
                if rows:
                    with self.lock:
                        self._commit_history(rows + self._read_index("history"))
                self.job["message"] = ("已停止" if self.cancel.is_set() else
                    f"{'服务器封面' if server else '原生封面更新' if publish else '生成'}完成：{self.job['animated']} GIF，{self.job['static']} 静态，{self.job['failed']} 失败")
            except Exception as exc:
                self.job["message"] = f"保存历史失败（{type(exc).__name__}）"
                self.job["failed"] = max(1, self.job["failed"])
            finally:
                self._client_context.client = None
                self.job["running"] = False
                self.job_lock.release()
        threading.Thread(target=work, name="MediaArchiverCoverStudio", daemon=True).start()
        return dict(self.job)

    def state(self) -> dict:
        with self.plugin._proxy_lock:
            views = [{"key": v["key"], "id": v["id"], "name": v["name"],
                      "count": len(v.get("item_ids") or []), "options": self.options(v),
                      "customized": v["key"] in (self.config.get("overrides") or {})}
                     for v in self.plugin._virtual_views.values()]
        with self.lock:
            history = self._read_index("history")
            fonts = self._read_index("fonts")
            backups = self._read_index("backups")
            native_views = [dict(v, count=None, options=self.options(v),
                customized=v["key"] in (self.config.get("overrides") or {})) for v in self.native_views]
        # History thumbnails are only returned by authenticated administration endpoints.
        recent = []
        for row in history[:60]:
            value = dict(row)
            path = self._file("history", row["id"], ".jpg")
            value["thumbnail"] = data_uri(path.read_bytes(), "image/jpeg") if path.is_file() else ""
            recent.append(value)
        presets = []
        for preset in PRESETS:
            identifier = preset["id"]
            if identifier not in self.thumbs:
                try:
                    image = self.render_frame({"name": "私人影院", "key": "attribute:4k", "item_ids": []},
                                              normalize_options({"style": identifier, "source": "brand"}))
                    image.thumbnail((320, 180))
                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=80)
                    self.thumbs[identifier] = data_uri(output.getvalue(), "image/jpeg")
                except Exception:
                    self.thumbs[identifier] = ""
            presets.append(dict(preset, thumbnail=self.thumbs[identifier]))
        return {"cover_servers": self.servers(), "version": self.plugin.plugin_version, "views": views, "presets": presets,
                "custom_presets": self.config.get("presets", []), "options": self.options({}),
                "fonts": [{"id": "default", "name": "思源风格 · Noto Sans SC（内置）"}] + fonts,
                "history": recent, "history_count": len(history), "backups": backups, "native_views": native_views,
                "job": dict(self.job), "studio_config": copy.deepcopy(self.config),
                "config": copy.deepcopy(self.plugin._saved_config),
                "defaults": self.plugin.get_form()[1], "servers": self.plugin._server_options(),
                "rules": [{"key": key, "name": rule["name"], "hint": rule["hint"]}
                          for key, rule in self.plugin.ATTRIBUTE_RULES.items()]}

    def upload_font(self, raw: dict) -> dict:
        from fontTools.ttLib import TTFont
        name = str(raw.get("name") or "字体").replace("\\", "/").rsplit("/", 1)[-1][:100]
        encoded = raw.get("data")
        if not isinstance(encoded, str) or len(encoded) > MAX_FONT*4//3+8:
            raise ValueError("字体文件不能超过 24 MiB")
        try:
            payload = base64.b64decode(encoded, validate=True)
            font = TTFont(io.BytesIO(payload), fontNumber=0)
            if "glyf" not in font and "CFF " not in font and "CFF2" not in font:
                raise ValueError()
            font.flavor = None
            output = io.BytesIO()
            font.save(output)
            font.close()
            payload = output.getvalue()
            from PIL import ImageFont
            ImageFont.truetype(io.BytesIO(payload), 24)
        except Exception:
            raise ValueError("字体无法读取，请上传有效的 TTF / OTF / TTC / WOFF / WOFF2") from None
        if len(payload) > MAX_FONT:
            raise ValueError("解压后的字体超过 24 MiB")
        identifier = hashlib.sha256(payload).hexdigest()[:32]
        row = {"id": identifier, "name": name, "size": len(payload)}
        with self.lock:
            fonts = self._read_index("fonts")
            if len(fonts) >= 20 and not any(f["id"] == identifier for f in fonts):
                raise ValueError("最多保存 20 个自定义字体")
            self._atomic(self._file("fonts", identifier, ".ttf"), payload)
            self._write_index("fonts", [f for f in fonts if f["id"] != identifier]+[row])
        return row

    def import_font_url(self, raw: dict) -> dict:
        """Administrator-only public HTTPS imports, without cookies or redirects."""
        import httpx
        url = str(raw.get("url") or "").strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("网络字体仅支持不带账户信息的 HTTPS 公网直链")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("网络字体地址必须是公网地址；本地字体请使用上传")
        payload = bytearray()
        # Pin the connection to the validated address, retaining TLS hostname verification.
        target = str(httpx.URL(url).copy_with(host=addresses[0][4][0]))
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", target, headers={"Host": parsed.hostname},
                               extensions={"sni_hostname": parsed.hostname}) as response:
                if response.status_code != 200:
                    raise ValueError("字体下载失败；请使用直接返回字体文件的 HTTPS 链接")
                deadline = time.monotonic() + 20
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_FONT or time.monotonic() > deadline:
                        raise ValueError("字体下载超过大小或时间限制")
        return self.upload_font({"name": urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) or "网络字体",
                                 "data": base64.b64encode(payload).decode("ascii")})

    def history_image(self, identifier: str) -> dict:
        with self.lock:
            row = next((r for r in self._read_index("history") if r["id"] == identifier), None)
            if row is None:
                raise ValueError("历史封面已不存在")
            payload = self._file("history", identifier, ".image").read_bytes()
        return {"image": data_uri(payload, row["mime"]), "mime": row["mime"], "name": row["name"]}

    def action(self, data: dict) -> dict:
        key = str(data.get("key") or "")
        if data.get("action") in {"restore_native_image", "restore_history"}:
            with self.lock:
                row = next((r for r in self._read_index("history") if r["id"] == data.get("id")), {})
            key = str(row.get("key") or "")
        server = key.split(":")[1] if key.startswith("native:") else str(data.get("server") or "")
        with self.on_server(server):
            return self._action(data)

    def _action(self, data: dict) -> dict:
        action = data.get("action")
        if action == "preview":
            return self.preview(str(data.get("key") or ""), data.get("options") or {}, bool(data.get("animated")))
        if action == "save_options":
            return {"options": self.save_options(str(data.get("key") or ""), data.get("options"), data.get("scope") == "global")}
        if action == "generate":
            return self.start_generate(str(data.get("key") or ""), data.get("publish") is True, str(data.get("server") or "") if data.get("all_server") is True else "")
        if action == "load_native":
            return {"count": len(self.load_native())}
        if action == "restore_native_image":
            if not self.job_lock.acquire(blocking=False):
                raise ValueError("封面生成中，请等待完成后恢复原生库图片")
            try:
                with self.lock:
                    row = next((r for r in self._read_index("history") if r["id"] == data.get("id")), None)
                    if row is None or not str(row.get("key", "")).startswith("native:"):
                        raise ValueError("请选择原生媒体库的历史图片")
                    payload = self._file("history", row["id"], ".image").read_bytes()
                self._upload_native(row, row["mime"], payload)
                return {"message": "原生媒体库图片已恢复"}
            finally:
                self.job_lock.release()
        if action == "cancel":
            self.cancel.set()
            return {"message": "将在当前封面处理结束后停止"}
        if action == "upload_font":
            return self.upload_font(data)
        if action == "import_font_url":
            return self.import_font_url(data)
        if action == "history_image":
            return self.history_image(str(data.get("id")))
        with self.lock:
            if action == "save_preset":
                cfg = copy.deepcopy(self.config)
                presets = cfg.setdefault("presets", [])
                if len(presets) >= 20:
                    raise ValueError("最多保存 20 个自定义方案")
                preset = {"id": uuid.uuid4().hex, "name": str(data.get("name") or "我的方案")[:40],
                          "options": normalize_options(data.get("options"))}
                presets.append(preset)
                self._save_studio(cfg)
                return preset
            if action == "delete_preset":
                cfg = copy.deepcopy(self.config)
                cfg["presets"] = [p for p in cfg.get("presets", []) if p["id"] != data.get("id")]
                self._save_studio(cfg)
                return {}
            if action == "reset_override":
                cfg = copy.deepcopy(self.config)
                cfg.setdefault("overrides", {}).pop(str(data.get("key")), None)
                self._save_studio(cfg)
                return {}
            if action in {"restore_history", "delete_history"}:
                rows = self._read_index("history")
                row = next((r for r in rows if r["id"] == data.get("id")), None)
                if row is None:
                    raise ValueError("历史封面已不存在")
                if action == "restore_history":
                    return {"options": self.save_options(row["key"], row["options"])}
                self._write_index("history", [r for r in rows if r["id"] != row["id"]])
                for suffix in (".image", ".jpg"):
                    self._file("history", row["id"], suffix).unlink(missing_ok=True)
                return {}
            if action == "backup":
                identifier = uuid.uuid4().hex
                snapshot = {"format": "mediaarchiver-config-v1", "version": self.plugin.plugin_version,
                            "config": copy.deepcopy(self.plugin._saved_config)}
                self._atomic(self._file("backups", identifier, ".json"), json.dumps(snapshot, ensure_ascii=False).encode())
                row = {"id": identifier, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
                rows = [row] + self._read_index("backups")
                self._write_index("backups", rows[:30])
                for old in rows[30:]:
                    self._file("backups", old["id"], ".json").unlink(missing_ok=True)
                return {"backup": snapshot, "id": identifier}
            if action == "read_backup":
                snapshot = json.loads(self._file("backups", str(data.get("id")), ".json").read_text(encoding="utf-8"))
                return {"backup": snapshot}
            if action == "clean_images":
                self.art_cache.clear()
                self.thumbs.clear()
                self.plugin._cover_cache = {}
                return {"message": "已清理图片缓存，历史封面保留"}
            if action == "clean_fonts":
                self.font_cache.clear()
                self.glyph_cache.clear()
                return {"message": "已清理字体缓存，上传字体保留"}
        raise ValueError("不支持的封面工坊操作")
