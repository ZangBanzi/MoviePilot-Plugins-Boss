"""媒体虚拟库 v4.3.9 离线回归测试。

用本地伪 Emby 验证首页 View 注入、原 ItemId 列表与 302 透传；
不连接真实 MoviePilot、Emby、TMDB 或榜单站点。
"""
from __future__ import annotations

import copy
import asyncio
import gzip
import http.client
import http.server
import importlib.util
import json
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class _Response:
    def __init__(self, success=True, message="", data=None):
        self.success = success
        self.message = message
        self.data = data


class _PluginBase:
    def __init__(self):
        self._test_data = {}

    def get_data(self, key):
        return self._test_data.get(key)

    def save_data(self, key, value):
        self._test_data[key] = copy.deepcopy(value)

    def update_config(self, value):
        self._updated_config = copy.deepcopy(value)


class _EventManager:
    @staticmethod
    def register(_event_type):
        def decorator(func):
            return func
        return decorator


class _MediaServerHelper:
    def get_configs(self):
        return {}

    def get_services(self, **kwargs):
        return {}


class _ASGIResponse:
    def __init__(self, content=b"", status_code=200, headers=None, media_type=None, **_kwargs):
        self.body = content if isinstance(content, bytes) else str(content).encode()
        self.status_code = status_code
        self.headers = dict(headers or {})
        if media_type:
            self.headers.setdefault("content-type", media_type)


class _StreamingResponse(_ASGIResponse):
    def __init__(self, content, status_code=200, headers=None, **kwargs):
        super().__init__(b"", status_code=status_code, headers=headers, **kwargs)
        self.body_iterator = content


class _APIRoute:
    def matches(self, scope):
        return _Match.FULL, {}


class _Match:
    NONE = 0
    FULL = 2


class _FakeRoute:
    def __init__(self, path, name):
        self.path = path
        self.name = name


class _FakeApp:
    def __init__(self):
        self.routes = []
        self.router = self
        self.openapi_schema = None

    def add_api_route(self, path, endpoint, name="", **_kwargs):
        self.routes.append(_FakeRoute(path, name))

    def add_api_websocket_route(self, path, endpoint, name="", **_kwargs):
        self.routes.append(_FakeRoute(path, name))


class _FakeRequest:
    def __init__(self, path, query="", method="GET", headers=None, body=b""):
        self.url = types.SimpleNamespace(path=path, query=query, scheme="http")
        self.method = method
        self.headers = dict(headers or {})
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self._body = body

    async def body(self):
        return self._body


def _install_moviepilot_stubs():
    app = types.ModuleType("app")
    app.schemas = types.SimpleNamespace(Response=_Response)
    sys.modules["app"] = app
    modules = {
        "app.core": types.ModuleType("app.core"),
        "app.core.config": types.ModuleType("app.core.config"),
        "app.core.event": types.ModuleType("app.core.event"),
        "app.helper": types.ModuleType("app.helper"),
        "app.helper.mediaserver": types.ModuleType("app.helper.mediaserver"),
        "app.log": types.ModuleType("app.log"),
        "app.plugins": types.ModuleType("app.plugins"),
        "app.schemas": types.ModuleType("app.schemas"),
        "app.schemas.types": types.ModuleType("app.schemas.types"),
        "app.factory": types.ModuleType("app.factory"),
        "fastapi": types.ModuleType("fastapi"),
        "fastapi.routing": types.ModuleType("fastapi.routing"),
        "starlette": types.ModuleType("starlette"),
        "starlette.responses": types.ModuleType("starlette.responses"),
        "starlette.routing": types.ModuleType("starlette.routing"),
        "starlette.websockets": types.ModuleType("starlette.websockets"),
    }
    sys.modules.update(modules)
    modules["app.core.config"].settings = types.SimpleNamespace(
        API_TOKEN="test-token", TMDB_API_KEY="", TMDB_API_DOMAIN="api.themoviedb.org",
        PORT=3334, TZ="Asia/Shanghai",
    )
    modules["app.core.event"].Event = object
    modules["app.core.event"].eventmanager = _EventManager()
    modules["app.helper.mediaserver"].MediaServerHelper = _MediaServerHelper
    modules["app.log"].logger = _Logger()
    modules["app.plugins"]._PluginBase = _PluginBase
    modules["app.schemas.types"].EventType = types.SimpleNamespace(WebhookMessage="webhook")
    modules["app.factory"].app = _FakeApp()
    modules["fastapi"].Request = _FakeRequest
    modules["fastapi"].Response = _ASGIResponse
    modules["fastapi"].WebSocket = object
    modules["fastapi.routing"].APIRoute = _APIRoute
    modules["starlette.responses"].StreamingResponse = _StreamingResponse
    modules["starlette.routing"].Match = _Match
    modules["starlette.websockets"].WebSocketDisconnect = RuntimeError


def _walk_ui(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_ui(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_ui(value)


def _plugin_path(root: Path):
    candidate = root / "plugins.v2" / "mediaarchiver" / "__init__.py"
    return candidate if candidate.exists() else root / "deliverables" / "__init__.py"


def _load_module(root: Path):
    _install_moviepilot_stubs()
    path = _plugin_path(root)
    spec = importlib.util.spec_from_file_location("virtual_library_plugin", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.httpx = None  # 此组保留标准库回退验证；真实 HTTPX 在独立集成测试覆盖
    return module


class _FakeScanEmby:
    def __init__(self, items):
        self.api_root = "http://fake-emby:8096"
        self._items = {item["Id"]: copy.deepcopy(item) for item in items}

    def library_items(self):
        values = [copy.deepcopy(item) for item in self._items.values()]
        return values + ([copy.deepcopy(values[0])] if values else [])


class _FakeOriginHandler(http.server.BaseHTTPRequestHandler):
    items = {}
    views_encoding = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, value, status=200, encoding=""):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if encoding == "gzip":
            body = gzip.compress(body)
        elif encoding == "deflate":
            body = zlib.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path[5:] if parsed.path.startswith("/emby/") else parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if route == "/System/Info":
            self._json({"ServerName": "Fake", "Version": "4.9"})
            return
        if route == "/Users/u/Views":
            self._json({"Items": [{
                "Id": "real-library", "Name": "电影", "Type": "CollectionFolder",
                "CollectionType": "movies", "ParentId": "1", "ServerId": "server-1",
            }], "TotalRecordCount": 1}, encoding=type(self).views_encoding)
            return
        if route in {"/Users/u/Items", "/Items"}:
            ids = (query.get("Ids") or [""])[0].split(",")
            values = [copy.deepcopy(self.items[item_id]) for item_id in ids if item_id in self.items]
            self._json({"Items": values, "TotalRecordCount": len(values)})
            return
        if route == "/Videos/m1/stream":
            self.send_response(302)
            self.send_header("Location", "https://cdn.invalid/original-m1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route.startswith("/Items/") and route.endswith("/Images/Primary"):
            body = b"poster"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"path": route}, 200)

    do_HEAD = do_GET


def main():
    root = Path(__file__).resolve().parent
    if root.name == "tests":
        root = root.parent
    module = _load_module(root)
    items = [
        {
            "Id": "m1", "ServerId": "server-1", "Type": "Movie", "Name": "甲电影",
            "SortName": "A", "OriginalTitle": "Movie A", "ProductionYear": 2026,
            "DateCreated": "2026-08-30T10:00:00Z",
            "ProviderIds": {"Tmdb": "100", "Imdb": "tt0000100"},
            "MediaSources": [{"Path": "/电影/A.2026.REMUX.mkv", "MediaStreams": [
                {"Type": "Video", "Width": 1920, "Height": 1080},
            ]}],
        },
        {
            "Id": "m2", "ServerId": "server-1", "Type": "Movie", "Name": "乙电影",
            "SortName": "B", "ProductionYear": 2025,
            "DateCreated": "2026-08-31T10:00:00Z", "ProviderIds": {"Tmdb": "200"},
            "MediaSources": [{"Path": "/电影/B.mkv", "MediaStreams": [
                {"Type": "Video", "Width": 3840, "Height": 2160,
                 "VideoRangeType": "DolbyVision", "DVProfile": 8},
                {"Type": "Audio", "Codec": "eac3", "CodecTag": "EAC3-JOC",
                 "Title": "Dolby Atmos"},
            ]}],
        },
        {
            "Id": "s1", "ServerId": "server-1", "Type": "Series", "Name": "甲剧集",
            "SortName": "Series A", "ProductionYear": 2026,
            "Overview": "TVB 港剧测试条目", "Studios": [{"Name": "无线电视"}],
            "ProviderIds": {"Tvdb": "900"}, "MediaSources": [],
        },
        {
            "Id": "s2", "ServerId": "server-1", "Type": "Series", "Name": "限制级剧集",
            "SortName": "Series B", "ProductionYear": 2024,
            "OfficialRating": "R18", "ProviderIds": {"Tvdb": "901"},
            "MediaSources": [],
        },
        {
            "Id": "m3", "ServerId": "server-1", "Type": "Movie", "Name": "普通AV1测试片",
            "SortName": "C", "ProductionYear": 2023,
            "MediaSources": [{"Path": "/电影/C.AV1.mkv", "MediaStreams": [
                {"Type": "Video", "Codec": "av1", "Width": 1920, "Height": 1080},
            ]}],
        },
        {
            "Id": "m4", "ServerId": "server-1", "Type": "Movie", "Name": "成人题材电影",
            "SortName": "D", "ProductionYear": 2022,
            "Tags": [{"Name": "伦理"}],
            "MediaSources": [{"Path": "/电影/D.mkv", "MediaStreams": []}],
        },
    ]
    untouched = copy.deepcopy(items)
    fake = _FakeScanEmby(items)
    plugin = module.MediaArchiver()
    plugin._enabled = True
    plugin._attribute_enabled = True
    plugin._ranking_enabled = True
    plugin._enabled_rules = set(plugin.ATTRIBUTE_RULES)
    plugin._selected_rankings = {
        "netflix_movie", "netflix_series", "apple_tv_movie", "disney_plus_series",
    }
    assert plugin._normalize_sync_cron(" 0   4,16 * * * ") == (
        "0 4,16 * * *", ""
    ), "Cron 应压缩多余空格并保留标准五段表达式"
    invalid_cron, cron_error = plugin._normalize_sync_cron("0 4 * *")
    assert invalid_cron == "0 4 * * *" and cron_error
    multi_version = {
        "Id": "multi", "Type": "Movie", "Name": "多版本电影",
        "MediaSources": [
            {"Path": "/电影/Movie.WEB-DL.mkv", "MediaStreams": []},
            {"Path": "/电影/Movie.REMUX.2160p.DV.mkv", "MediaStreams": [
                {"Type": "Video", "Width": 3840, "Height": 2160,
                 "VideoRangeType": "DolbyVision"},
                {"Type": "Audio", "Codec": "truehd", "Title": "Dolby Atmos"},
            ]},
        ],
    }
    assert plugin._classify(multi_version) == {
        "remux", "4k", "dolby_vision", "hdr", "atmos",
    }, "多版本电影应遍历全部MediaSources，但最终仍按一个ItemId归类"
    assert "adult" not in plugin._classify(items[4]), "AV1 编码不能误判为成人内容"
    identity = f"Emby-A|{fake.api_root}"
    old_view_key = "ranking:apple_tv_movie"
    old_view = plugin._make_virtual_view(
        old_view_key, "Apple TV+ · 电影榜", "ranking", {"m1"},
        {item["Id"]: item for item in items}, "2026-08-30T00:00:00Z",
    )
    plugin._state["servers"] = {identity: {
        "name": "Emby-A", "api_root": fake.api_root, "active": True,
        "virtual_views": {old_view_key: old_view},
    }}
    results = {
        "netflix_movie": module.RankingResult(True, {
            module.RankEntry(media_type="Movie", tmdb="100"),
            module.RankEntry(media_type="Movie", tmdb="200"),
        }, "test"),
        "netflix_series": module.RankingResult(True, {
            module.RankEntry(media_type="Series", tvdb="900"),
        }, "test"),
        "apple_tv_movie": module.RankingResult(False, set(), error="source down"),
        "disney_plus_series": module.RankingResult(False, set(), error="connection reset"),
    }
    stats = plugin._sync_server(fake, "Emby-A", results)
    assert stats["scanned"] == 6, stats
    state = plugin._state["servers"][identity]
    views = state["virtual_views"]
    assert set(views["attribute:remux"]["item_ids"]) == {"m1"}
    assert set(views["attribute:4k"]["item_ids"]) == {"m2"}
    assert set(views["attribute:dolby_vision"]["item_ids"]) == {"m2"}
    assert set(views["attribute:hdr"]["item_ids"]) == {"m2"}
    assert set(views["attribute:atmos"]["item_ids"]) == {"m2"}
    assert set(views["attribute:tvb"]["item_ids"]) == {"s1"}
    assert views["attribute:tvb"]["collection_type"] == "tvshows"
    assert set(views["attribute:adult"]["item_ids"]) == {"m4", "s2"}
    assert views["attribute:adult"]["collection_type"] == "mixed"
    assert set(views["ranking:netflix_movie"]["item_ids"]) == {"m1", "m2"}
    assert set(views["ranking:netflix_series"]["item_ids"]) == {"s1"}
    assert views[old_view_key]["item_ids"] == ["m1"], "榜单源失败应保留上次结果"
    assert views["ranking:disney_plus_series"]["item_ids"] == [], (
        "首次同步失败的已选专区也应保留一级库入口"
    )
    assert items == untouched, "同步不应修改 Emby Item 或 MediaSource"
    assert plugin._virtual_views and plugin._proxy_item_index
    assert all(
        "MediaSources" not in item and "MediaStreams" not in item
        for item in plugin._proxy_item_index.values()
    ), "网关常驻索引不应保留体积巨大的媒体流字段"

    second = plugin._sync_server(fake, "Emby-A", results)
    assert second["added"] == 0 and second["removed"] == 0, second
    fake._items["m1"]["MediaSources"][0]["Path"] = "/电影/A.WEB-DL.mkv"
    del fake._items["m2"]
    changed = plugin._sync_server(fake, "Emby-A", results)
    assert changed["removed"] >= 5, changed
    empty_remux = plugin._state["servers"][identity]["virtual_views"]["attribute:remux"]
    assert empty_remux["item_ids"] == [], "已启用但零命中的专区仍须保留在首页"
    assert plugin._state["servers"][identity]["virtual_views"]["attribute:tvb"]["item_ids"] == ["s1"]
    assert set(plugin._state["servers"][identity]["virtual_views"]["attribute:adult"]["item_ids"]) == {"m4", "s2"}

    # 临时连接失败不得把上次成功状态标记为失效，否则重启后一级库会消失。
    plugin._state["servers"][identity]["active"] = True
    plugin._create_clients = lambda: (_ for _ in ()).throw(RuntimeError("temporary offline"))
    assert plugin._run_lock.acquire(blocking=False)
    plugin._sync_worker("scheduled")
    assert plugin._state["servers"][identity]["active"] is True

    # 使用真实本地伪 Emby，验证 MoviePilot 3334 内置网关的
    # Views/Items/Latest/详情/海报与 302 透明转发；插件本身不监听端口。
    _FakeOriginHandler.items = {item["Id"]: copy.deepcopy(item) for item in items}
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeOriginHandler)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    proxy = module.MediaArchiver()
    proxy._enabled = proxy._attribute_enabled = True
    proxy._proxy_item_index = {item["Id"]: copy.deepcopy(item) for item in items}
    remux = proxy._make_virtual_view(
        "attribute:remux", "REMUX", "attribute", {"m1", "m2"},
        proxy._proxy_item_index, "2026-08-31T10:00:00Z",
    )
    mixed = proxy._make_virtual_view(
        "ranking:test_mixed", "测试混合榜", "ranking", {"m1", "s1"},
        proxy._proxy_item_index, "2026-08-31T10:00:00Z", "mixed",
    )
    remux_single = proxy._make_virtual_view(
        "attribute:remux", "REMUX", "attribute", {"m1"},
        proxy._proxy_item_index, "2026-08-31T10:00:00Z",
    )
    assert remux_single["cover_tag"] != remux["cover_tag"], "成员变化后封面标签必须刷新"
    proxy._virtual_views = {remux["id"]: remux, mixed["id"]: mixed}
    proxy._gateway_client_cache = module.EmbyClient(
        f"http://127.0.0.1:{origin.server_port}", "key"
    )
    proxy._gateway_server_name = "Fake"
    proxy._install_gateway_routes()
    assert proxy._proxy_status["running"], proxy._proxy_status
    assert proxy._proxy_status["api_port"] == 3334
    assert not hasattr(proxy, "_proxy_port")
    gateway_route = object.__new__(module.EmbyGatewayRoute)
    assert gateway_route.matches({"type": "http", "path": "/api/v1/plugin"})[0] == _Match.NONE
    assert gateway_route.matches({"type": "http", "path": "/Users/u/Views"})[0] == _Match.FULL
    assert gateway_route.matches({"type": "http", "path": "/QuickConnect/Enabled"})[0] == _Match.FULL
    assert gateway_route.matches({"type": "http", "path": "/Trailers"})[0] == _Match.FULL
    forwarded = proxy._forward_request_headers(
        _FakeRequest("/Users/u/Views", headers={"accept-encoding": "br, gzip"}),
        urllib.parse.urlsplit(f"http://127.0.0.1:{origin.server_port}"), b"", True,
    )
    encoding_headers = [
        value for key, value in forwarded.items() if key.casefold() == "accept-encoding"
    ]
    assert encoding_headers == ["identity"], forwarded
    health = asyncio.run(proxy._emby_gateway(
        _FakeRequest("/__mediaarchiver__/health"), "/__mediaarchiver__/health"
    ))
    assert json.loads(health.body)["ok"] is True
    views_response = asyncio.run(proxy._emby_gateway(
        _FakeRequest("/Users/u/Views"), "/Users/u/Views"
    ))
    views_payload = json.loads(views_response.body)
    injected = [item for item in views_payload["Items"] if item["Id"] == remux["id"]]
    assert len(injected) == 1 and injected[0]["Type"] == "CollectionFolder"
    assert injected[0]["ImageTags"]["Primary"] == remux["cover_tag"]
    assert len(remux["cover_tag"]) == 32
    assert injected[0]["PrimaryImageAspectRatio"] == 16 / 9
    mixed_dto = next(item for item in views_payload["Items"] if item["Id"] == mixed["id"])
    assert mixed_dto["CollectionType"] is None, "混合电影/剧集库必须使用 null CollectionType"
    # 某些 Emby/反代即使收到 identity 仍返回压缩 JSON；网关必须先解压再注入。
    for encoding in ("gzip", "deflate"):
        _FakeOriginHandler.views_encoding = encoding
        compressed_views = json.loads(asyncio.run(proxy._emby_gateway(
            _FakeRequest("/Users/u/Views"), "/Users/u/Views"
        )).body)
        assert any(item["Id"] == remux["id"] for item in compressed_views["Items"]), encoding
    _FakeOriginHandler.views_encoding = ""
    prefixed_views = json.loads(asyncio.run(proxy._emby_gateway(
        _FakeRequest("/emby/Users/u/Views"), "/emby/Users/u/Views"
    )).body)
    assert any(item["Id"] == remux["id"] for item in prefixed_views["Items"])
    query = f"ParentId={remux['id']}&Limit=2&SortBy=SortName"
    listing = json.loads(asyncio.run(proxy._emby_gateway(
        _FakeRequest("/Users/u/Items", query), "/Users/u/Items"
    )).body)
    assert [item["Id"] for item in listing["Items"]] == ["m1", "m2"]
    root_query = f"UserId=u&ParentId={remux['id']}&Limit=2&SortBy=SortName"
    root_listing = json.loads(asyncio.run(proxy._emby_gateway(
        _FakeRequest("/Items", root_query), "/Items"
    )).body)
    assert [item["Id"] for item in root_listing["Items"]] == ["m1", "m2"]
    query = f"ParentId={remux['id']}&Limit=1"
    prefixed_listing = json.loads(asyncio.run(proxy._emby_gateway(
        _FakeRequest("/emby/Users/u/Items", query), "/emby/Users/u/Items"
    )).body)
    assert len(prefixed_listing["Items"]) == 1
    query = f"ParentId={remux['id']}&Limit=1"
    latest = json.loads(asyncio.run(proxy._emby_gateway(
        _FakeRequest("/Users/u/Items/Latest", query), "/Users/u/Items/Latest"
    )).body)
    assert [item["Id"] for item in latest] == ["m2"]
    root_latest_query = f"UserId=u&ParentId={remux['id']}&Limit=1"
    root_latest = json.loads(asyncio.run(proxy._emby_gateway(
        _FakeRequest("/Items/Latest", root_latest_query), "/Items/Latest"
    )).body)
    assert [item["Id"] for item in root_latest] == ["m2"]
    detail = json.loads(asyncio.run(proxy._emby_gateway(
        _FakeRequest(f"/Items/{remux['id']}"), f"/Items/{remux['id']}"
    )).body)
    assert detail["Name"] == "REMUX" and detail["ChildCount"] == 2
    cover_response = asyncio.run(proxy._emby_gateway(
        _FakeRequest(f"/Items/{remux['id']}/Images/Primary"),
        f"/Items/{remux['id']}/Images/Primary",
    ))
    assert cover_response.headers.get("ETag") == f'"{remux["cover_tag"]}"'
    assert cover_response.body.startswith(b"\x89PNG")
    original_pillow_renderer = proxy._render_cover_pillow
    proxy._render_cover_pillow = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("Pillow unavailable")
    )
    proxy._cover_cache.clear()
    fallback_cover = proxy._render_cover_png(remux)
    assert fallback_cover.startswith(b"\x89PNG"), "无 Pillow 时也必须输出动态 PNG 封面"
    proxy._render_cover_pillow = original_pillow_renderer
    cached = asyncio.run(proxy._emby_gateway(
        _FakeRequest(
            f"/Items/{remux['id']}/Images/Primary",
            headers={"If-None-Match": f'"{remux["cover_tag"]}"'},
        ),
        f"/Items/{remux['id']}/Images/Primary",
    ))
    assert cached.status_code == 304
    redirect = asyncio.run(proxy._emby_gateway(
        _FakeRequest("/Videos/m1/stream"), "/Videos/m1/stream"
    ))
    assert redirect.status_code == 302
    assert redirect.headers.get("Location") == "https://cdn.invalid/original-m1"
    async def consume_redirect():
        return b"".join([part async for part in redirect.body_iterator])
    assert asyncio.run(consume_redirect()) == b""
    proxy.stop_service()
    origin.shutdown()
    origin.server_close()

    # MoviePilot v2 自带 httpx。验证异步连接池只创建一次、未知 Emby 路径
    # 透明放行，并能承受并发客户端请求；本地测试无需安装真实 httpx。
    class _FakeHttpxHeaders:
        def __init__(self, values):
            self._values = list(values)

        def multi_items(self):
            return list(self._values)

    class _FakeHttpxResponse:
        def __init__(self, body):
            self.status_code = 200
            self.headers = _FakeHttpxHeaders([
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            self._body = body
            self.closed = False

        async def aiter_raw(self, chunk_size=None):
            del chunk_size
            yield self._body

        async def aclose(self):
            self.closed = True

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.is_closed = False
            self.send_count = 0
            self.inflight = 0
            self.peak_inflight = 0
            _FakeHttpx.clients.append(self)

        def build_request(self, method, url, headers=None, content=None):
            return {"method": method, "url": url, "headers": headers, "content": content}

        async def send(self, request, stream=False):
            assert stream is True
            self.send_count += 1
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
            await asyncio.sleep(0.002)
            self.inflight -= 1
            if "/Users/u/Views" in request["url"]:
                body = json.dumps({"Items": [], "TotalRecordCount": 0}).encode()
            else:
                body = json.dumps({"path": urllib.parse.urlsplit(request["url"]).path}).encode()
            return _FakeHttpxResponse(body)

        async def aclose(self):
            self.is_closed = True

    class _FakeHttpx:
        clients = []
        AsyncClient = _FakeAsyncClient

        class Timeout:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

        class Limits:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    async def _consume(response):
        if hasattr(response.body_iterator, "__aiter__"):
            return b"".join([chunk async for chunk in response.body_iterator])
        return b"".join(response.body_iterator)

    original_httpx = module.httpx
    module.httpx = _FakeHttpx
    fast_proxy = module.MediaArchiver()
    fast_proxy._enabled = fast_proxy._attribute_enabled = True
    fast_proxy._gateway_client_cache = types.SimpleNamespace(api_root="http://fake-emby:8096")
    fast_proxy._virtual_views = {remux["id"]: remux}

    async def _exercise_pool():
        views_result = await fast_proxy._emby_gateway(
            _FakeRequest("/Users/u/Views"), "/Users/u/Views"
        )
        assert any(
            item["Id"] == remux["id"] for item in json.loads(views_result.body)["Items"]
        )

        async def request_once(index):
            path = f"/QuickConnect/Enabled?request={index}"
            response = await fast_proxy._emby_gateway(
                _FakeRequest("/QuickConnect/Enabled", f"request={index}"),
                "/QuickConnect/Enabled",
            )
            payload = json.loads(await _consume(response))
            assert payload["path"] == "/QuickConnect/Enabled"

        await asyncio.gather(*(request_once(index) for index in range(32)))

    asyncio.run(_exercise_pool())
    assert len(_FakeHttpx.clients) == 1, "全部网关请求应复用同一个异步连接池"
    assert _FakeHttpx.clients[0].peak_inflight > 1, "并发请求不应排队串行处理"
    assert _FakeHttpx.clients[0].send_count == 33
    assert fast_proxy._gateway_metrics["failures"] == 0
    fast_proxy.stop_service()
    assert _FakeHttpx.clients[0].is_closed, "插件停止时必须关闭连接池"
    module.httpx = original_httpx

    plugin._server_options = lambda: [{"title": "Emby-A", "value": "Emby-A"}]
    form, defaults = plugin.get_form()
    ui = list(_walk_ui(form))
    models = {
        node.get("props", {}).get("model") for node in ui
        if isinstance(node.get("props"), dict)
    }
    assert {
        "emby_server", "virtual_enabled", "ranking_enabled", "auto_sync",
        "daily_sync_enabled", "sync_cron",
    } <= models
    assert not {"manual_url", "manual_api_key", "use_moviepilot_servers"} & models
    assert not {"home_view_enabled", "dynamic_cover_enabled", "proxy_port", "proxy_bind"} & models
    all_text = json.dumps(form, ensure_ascii=False)
    assert "MoviePilot API 端口 3334" in all_text and "NAS-IP:8098" in all_text
    assert "NAS-IP:8099" not in all_text
    assert "旧合集" not in all_text
    assert not {"home_view_enabled", "dynamic_cover_enabled", "proxy_port", "proxy_bind"} & set(defaults)
    api_paths = {item["path"] for item in plugin.get_api()}
    assert {"/test_connection", "/rebuild", "/status"} <= api_paths
    assert "/emby" not in api_paths, "3334 根级网关无需重复暴露插件前缀匿名API"
    assert "/cleanup_legacy_collections" not in api_paths

    class _Trigger:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @classmethod
        def from_crontab(cls, expression, **kwargs):
            trigger = cls(**kwargs)
            trigger.expression = expression
            return trigger

    module.IntervalTrigger = module.CronTrigger = _Trigger
    plugin._auto_sync = plugin._daily_sync_enabled = True
    plugin._sync_cron = "0 4,16 * * *"
    services = plugin.get_service()
    assert [service["id"] for service in services] == [
        "MediaArchiver.virtual_library_reconcile",
        "MediaArchiver.timed_full_sync",
    ]
    assert services[0]["kwargs"] == {"mode": "incremental"}
    assert services[0]["trigger"].kwargs == {"minutes": plugin._sync_interval}
    assert services[1]["trigger"].expression == "0 4,16 * * *"
    assert services[1]["trigger"].kwargs == {"timezone": "Asia/Shanghai"}
    assert services[1]["kwargs"] == {
        "mode": "timed", "schedule": "0 4,16 * * *",
    }

    source = _plugin_path(root).read_text(encoding="utf-8").casefold()
    forbidden = (
        "import shutil", "os.rename", "shutil.move", "shutil.copy", "clouddrive",
        "webdav", "strm_root", "cd2_url", "/mnt/115", "\u674e\u660e\u5b87",
        "def ensure_collection", "def add_items",
    )
    assert not any(token.casefold() in source for token in forbidden), [
        token for token in forbidden if token.casefold() in source
    ]
    assert '"post", "/collections"' not in source
    package = json.loads((root / ("" if (root / "package.v2.json").exists() else "deliverables") / "package.v2.json").read_text(encoding="utf-8"))
    fetcher = module.RankingFetcher("key", "api.themoviedb.org", "zh-CN", ["US"], 40, 5)
    fetcher._tmdb = lambda path, params=None: {
        "results": [{"provider_id": 350, "provider_name": "Apple 原创内容"}]
    }
    assert fetcher._provider_id("apple_tv", "Movie") == 350

    assert module.MediaArchiver.PUBLIC_GATEWAY_PORT == 8098
    assert package["MediaArchiver"]["version"] == module.MediaArchiver.plugin_version == "4.3.9"
    assert module.MediaArchiver.plugin_author == "Boss"
    print("PASS: 定时全量、增量校准、3334网关、首页View、动态封面、原ItemId、分页、Latest与302透传通过")


if __name__ == "__main__":
    main()
