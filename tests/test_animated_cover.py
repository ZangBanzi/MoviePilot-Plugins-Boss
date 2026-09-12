"""真实 GIF 多帧、缓存与静态回退；不连接 NAS。"""
import io
from pathlib import Path
from PIL import Image
from test_virtual_library import _load_module, _FakeRequest

p = _load_module(Path(__file__).resolve().parents[1]).MediaArchiver()
view = {"key": "attribute:remux", "name": "Remux专区", "item_ids": ["1", "2"]}
view["cover_tag"] = p._cover_tag(view["key"], view["item_ids"])
response = p._virtual_cover_response(_FakeRequest("/cover"), view)
with Image.open(io.BytesIO(response.body)) as image:
    assert image.format == "GIF" and image.n_frames == 12
    assert image.size == (640, 360) and image.info["loop"] == 0
    first = image.convert("RGB").tobytes()
    image.seek(6)
    assert first != image.convert("RGB").tobytes()
p._render_cover_animated = lambda _: (_ for _ in ()).throw(AssertionError("重复绘图"))
assert p._virtual_cover_response(_FakeRequest("/cover"), view).body == response.body
assert p._virtual_cover_response(
    _FakeRequest("/cover", headers={"If-None-Match": response.headers["ETag"]}), view
).status_code == 304
assert p._cover_tag(view["key"], ["1"]) != view["cover_tag"]
p = _load_module(Path(__file__).resolve().parents[1]).MediaArchiver()
p._render_cover_png = lambda _: b"invalid-for-gif"
assert p._render_cover_animated(view) == ("image/png", b"invalid-for-gif")
print("PASS: 12帧GIF、画面变化、640x360、循环、缓存、304、成员变化、动画失败回退")
