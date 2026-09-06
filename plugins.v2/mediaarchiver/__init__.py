"""MoviePilot v2：媒体属性 + 榜单首页虚拟媒体库。

通过轻量 Emby 反向代理向 ``/Users/{id}/Views`` 注入一级媒体库，
再把虚拟库查询映射回原媒体 ItemId。不创建 Collection/BoxSet，不访问、
不移动、不复制、不重命名真实媒体文件，也不改写 MediaSource.Path。
"""
from __future__ import annotations

import asyncio
import gzip
import html
import hashlib
import http.client
import http.cookiejar
import io
import importlib.util
import json
import math
import os
import re
import ssl
import struct
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

try:
    from fastapi import Request, Response, WebSocket
    from fastapi.routing import APIRoute
    from starlette.responses import StreamingResponse
    from starlette.routing import Match
    from starlette.websockets import WebSocketDisconnect
    _FASTAPI_AVAILABLE = True
except Exception:  # pragma: no cover - 仅供脱离 MoviePilot 的静态测试
    Request = Response = WebSocket = Any  # type: ignore[misc,assignment]
    APIRoute = object  # type: ignore[misc,assignment]
    StreamingResponse = None  # type: ignore[assignment]
    Match = None  # type: ignore[assignment]
    WebSocketDisconnect = Exception  # type: ignore[assignment]
    _FASTAPI_AVAILABLE = False

try:
    from apscheduler.triggers.interval import IntervalTrigger
except Exception:  # pragma: no cover - 极老 v2 宿主兼容
    IntervalTrigger = None

try:
    from apscheduler.triggers.cron import CronTrigger
except Exception:  # pragma: no cover - 极老 v2 宿主兼容
    CronTrigger = None

try:
    import httpx  # MoviePilot v2 自带；用于异步连接池和真正的并发透传
except Exception:  # pragma: no cover - 离线测试或极老宿主回退标准库
    httpx = None  # type: ignore[assignment]

try:
    with open(__file__, "rb") as _source_file:
        SOURCE_SHA256 = hashlib.sha256(_source_file.read()).hexdigest()
except OSError:
    SOURCE_SHA256 = "unavailable"


class GatewayCookieJar(http.cookiejar.CookieJar):
    """连接池共享连接，但不保存或复用用户的上游 Cookie。"""

    def set_cookie(self, cookie: Any, *args: Any, **kwargs: Any) -> None:
        return


class GatewayStreamingResponse(StreamingResponse if _FASTAPI_AVAILABLE else object):
    """即使客户端在迭代响应体前断开，也释放上游与统计计数。"""

    def __init__(self, *args: Any, cleanup: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cleanup = cleanup

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._cleanup is not None:
                import anyio
                with anyio.CancelScope(shield=True):
                    await self._cleanup()


class EmbyHttpError(RuntimeError):
    """带状态码的 Emby HTTP 异常。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class EmbyClient:
    """虚拟库所需的最小 Emby REST 客户端。"""

    ITEM_FIELDS = (
        "Path,FileName,MediaSources,MediaStreams,Width,Height,Container,"
        "ProviderIds,ProductionYear,PremiereDate,DateCreated,DateLastSaved,"
        "OriginalTitle,SortName,Genres,Studios,CommunityRating,OfficialRating"
    )

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        base_url = str(base_url or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Emby 地址必须以 http:// 或 https:// 开头")
        if not str(api_key or "").strip():
            raise ValueError("Emby API Key 为空")
        if base_url.endswith("/web"):
            base_url = base_url[:-4]
        self.base_url = base_url
        self.api_key = str(api_key).strip()
        self.timeout = max(5, min(int(timeout), 120))
        self.api_root = self._discover_api_root()

    def _discover_api_root(self) -> str:
        candidates = [self.base_url]
        if not self.base_url.casefold().endswith("/emby"):
            candidates.append(f"{self.base_url}/emby")
        errors: List[str] = []
        for root in candidates:
            try:
                self._request_at(root, "GET", "/System/Info")
                return root
            except Exception as err:
                errors.append(str(err))
        raise RuntimeError("无法连接 Emby，请检查地址/API Key；" + "；".join(errors[-2:]))

    def _request_at(
        self,
        root: str,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        expected: Sequence[int] = (200, 204),
    ) -> bytes:
        pairs = []
        for key, value in (query or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            pairs.append((key, value))
        suffix = urllib.parse.urlencode(pairs, doseq=True)
        url = f"{root.rstrip('/')}/{path.lstrip('/')}"
        if suffix:
            url += "?" + suffix
        request = urllib.request.Request(
            url,
            data=None if method.upper() == "GET" else b"",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Emby-Token": self.api_key,
                "X-MediaBrowser-Token": self.api_key,
                "User-Agent": "MoviePilot-MediaVirtualLibrary/4.3.7",
            },
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if response.status not in expected:
                    raise EmbyHttpError(response.status, f"Emby 返回 HTTP {response.status}")
                return body
        except urllib.error.HTTPError as err:
            detail = err.read(400).decode("utf-8", "replace").strip()
            raise EmbyHttpError(
                err.code, f"Emby {method.upper()} {path} 失败：HTTP {err.code} {detail}"
            ) from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"无法连接 Emby：{err.reason}") from err

    def request_json(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        expected: Sequence[int] = (200, 204),
    ) -> Dict[str, Any]:
        raw = self._request_at(self.api_root, method, path, query, expected)
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {"Items": value}

    def _paged_items(self, **query: Any) -> List[Dict[str, Any]]:
        start, limit = 0, 300
        result: List[Dict[str, Any]] = []
        while True:
            params = dict(query)
            params.update({"StartIndex": start, "Limit": limit})
            payload = self.request_json("GET", "/Items", params)
            items = payload.get("Items") or []
            result.extend(item for item in items if isinstance(item, dict))
            total = int(payload.get("TotalRecordCount") or len(result))
            if not items or len(result) >= total:
                break
            start += len(items)
        return result

    def library_items(self) -> List[Dict[str, Any]]:
        return self._paged_items(
            Recursive=True,
            IncludeItemTypes="Movie,Series",
            Fields=self.ITEM_FIELDS,
        )

class EmbyGatewayRoute(APIRoute):  # type: ignore[misc]
    """兜底匹配 Emby API，但明确避开 MoviePilot 自身的根级入口。

    Emby 客户端生态存在大量非固定顶级路径，例如 QuickConnect、Trailers、
    HomeVideos、MediaInfo。旧版白名单会把这些请求变成 MoviePilot 404，表现为
    “能登录，但首页/详情/播放器没有内容”。这里改成 MoviePilot 保留路径黑名单，
    其余未知路径均透明交给 Emby，兼容官方客户端和第三方播放器。
    """

    MOVIEPILOT_PREFIXES = {"api", "docs", "redoc", "openapi.json"}

    def matches(self, scope: Mapping[str, Any]) -> Tuple[Any, Mapping[str, Any]]:
        match, child_scope = super().matches(scope)  # type: ignore[misc]
        if Match is None or match == Match.NONE:
            return match, child_scope
        path = str(scope.get("path") or "/")
        first = path.lstrip("/").split("/", 1)[0].casefold()
        if first in self.MOVIEPILOT_PREFIXES:
            return Match.NONE, child_scope
        return match, child_scope



@dataclass(frozen=True)
class RankEntry:
    """榜单条目的稳定身份；优先使用 ProviderId，标题仅作严格回退。"""

    media_type: str = ""
    tmdb: str = ""
    imdb: str = ""
    tvdb: str = ""
    anilist: str = ""
    bangumi: str = ""
    douban: str = ""
    title: str = ""
    original_title: str = ""
    year: int = 0


@dataclass
class RankingResult:
    ok: bool
    entries: Set[RankEntry]
    source: str = ""
    error: str = ""


RANK_GROUPS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "popular", "name": "热门", "subtitle": "全球榜单", "icon": "mdi-fire",
        "items": (
            ("imdb_popular_movie", "IMDb 最热门电影"),
            ("imdb_popular_series", "IMDb 最热门剧集"),
            ("tmdb_trending", "TMDB 趋势"),
            ("anilist_popular", "AniList 热门"),
            ("bangumi_today", "Bangumi 今日动漫"),
            ("popular_mixed", "混合榜"),
        ),
    },
    {
        "key": "netflix", "name": "Netflix", "subtitle": "全部地区汇总", "icon": "mdi-netflix",
        "items": (("netflix_movie", "电影榜"), ("netflix_series", "剧集榜"), ("netflix_mixed", "混合榜")),
    },
    {
        "key": "hbo", "name": "HBO", "subtitle": "全部地区汇总", "icon": "mdi-television-classic",
        "items": (("hbo_movie", "电影榜"), ("hbo_series", "剧集榜"), ("hbo_mixed", "混合榜")),
    },
    {
        "key": "apple_tv", "name": "Apple TV+", "subtitle": "全部地区汇总", "icon": "mdi-apple",
        "items": (("apple_tv_movie", "电影榜"), ("apple_tv_series", "剧集榜"), ("apple_tv_mixed", "混合榜")),
    },
    {
        "key": "disney_plus", "name": "Disney+", "subtitle": "全部地区汇总", "icon": "mdi-movie-open-star",
        "items": (("disney_plus_movie", "电影榜"), ("disney_plus_series", "剧集榜"), ("disney_plus_mixed", "混合榜")),
    },
    {
        "key": "crunchyroll", "name": "Crunchyroll", "subtitle": "全部地区汇总", "icon": "mdi-animation-play",
        "items": (("crunchyroll_movie", "电影榜"), ("crunchyroll_series", "剧集榜"), ("crunchyroll_mixed", "混合榜")),
    },
    {
        "key": "amazon_prime", "name": "Amazon Prime", "subtitle": "全部地区汇总", "icon": "mdi-amazon",
        "items": (("amazon_prime_movie", "电影榜"), ("amazon_prime_series", "剧集榜"), ("amazon_prime_mixed", "混合榜")),
    },
    {
        "key": "amazon", "name": "Amazon", "subtitle": "全部地区汇总", "icon": "mdi-amazon",
        "items": (("amazon_movie", "电影榜"), ("amazon_series", "剧集榜"), ("amazon_mixed", "混合榜")),
    },
    {
        "key": "hulu", "name": "Hulu", "subtitle": "全部地区汇总", "icon": "mdi-television-play",
        "items": (("hulu_movie", "电影榜"), ("hulu_series", "剧集榜"), ("hulu_mixed", "混合榜")),
    },
    {
        "key": "maoyan", "name": "猫眼", "subtitle": "全国榜单", "icon": "mdi-cat",
        "items": (("maoyan_movie", "电影榜"), ("maoyan_series", "剧集榜"),
                  ("maoyan_variety", "综艺榜"), ("maoyan_mixed", "混合榜")),
    },
    {
        "key": "douban", "name": "豆瓣", "subtitle": "全国榜单", "icon": "mdi-alpha-d-box",
        "items": (
            ("douban_soon", "即将上映"), ("douban_showing", "正在上映"),
            ("douban_new", "新片榜"), ("douban_weekly", "一周口碑榜"),
            ("douban_north_america", "北美票房榜"),
            ("douban_tv_domestic", "华语口碑剧集榜"),
            ("douban_tv_global", "全球口碑剧集榜"), ("douban_mixed", "混合榜"),
        ),
    },
    {
        "key": "tencent", "name": "腾讯视频", "subtitle": "全国榜单", "icon": "mdi-play-circle",
        "items": (
            ("tencent_hot", "腾讯热播"), ("tencent_series", "腾讯电视剧"),
            ("tencent_kids", "腾讯少儿"), ("tencent_movie", "腾讯电影"),
            ("tencent_anime", "腾讯动漫"), ("tencent_documentary", "腾讯纪录片"),
            ("tencent_mixed", "混合榜"),
        ),
    },
)


RANK_META: Dict[str, Dict[str, str]] = {}
for _group in RANK_GROUPS:
    for _key, _label in _group["items"]:
        RANK_META[_key] = {
            "group": _group["key"], "group_name": _group["name"], "label": _label,
            "collection": f"{_group['name']} · {_label}",
        }


# 截图中保存状态为 13 个：Netflix、Apple TV+、Disney+ 各三项，猫眼四项。
DEFAULT_RANKINGS: Set[str] = {
    "netflix_movie", "netflix_series", "netflix_mixed",
    "apple_tv_movie", "apple_tv_series", "apple_tv_mixed",
    "disney_plus_movie", "disney_plus_series", "disney_plus_mixed",
    "maoyan_movie", "maoyan_series", "maoyan_variety", "maoyan_mixed",
}


class RankingFetcher:
    """独立榜单取数层。

    精确来源优先级：自定义 JSON Feed > 官方/公开接口 > 网页兼容解析。
    任何来源失败都会返回 ``ok=False``，同步层会保留上次虚拟库成员，不会误清空。
    """

    PLATFORM_PROVIDERS: Dict[str, Tuple[str, ...]] = {
        "netflix": ("Netflix",),
        "hbo": ("Max", "HBO Max", "HBO", "Max Amazon Channel"),
        "apple_tv": ("Apple TV Plus", "Apple TV+", "Apple TV"),
        "disney_plus": ("Disney Plus", "Disney+", "Disney Plus Basic"),
        "crunchyroll": ("Crunchyroll",),
        "amazon_prime": ("Amazon Prime Video",),
        "amazon": ("Amazon Video",),
        "hulu": ("Hulu",),
        "tencent": ("Tencent Video", "WeTV"),
    }
    # Provider 名称会因 TMDB 语言和地区出现差异，使用官方稳定 ID 作为兜底。
    # 只有该 ID 确实存在于本次 Provider 列表时才采用，避免误匹配。
    PLATFORM_PROVIDER_IDS: Dict[str, Tuple[int, ...]] = {
        "netflix": (8,), "hbo": (1899, 384, 118), "apple_tv": (350,),
        "disney_plus": (337,), "crunchyroll": (283,),
        "amazon_prime": (9,), "amazon": (10,), "hulu": (15,),
    }
    DOUBAN_COLLECTIONS = {
        "douban_soon": ("movie_soon", "Movie"),
        "douban_showing": ("movie_showing", "Movie"),
        "douban_new": ("movie_latest", "Movie"),
        "douban_weekly": ("movie_weekly_best", "Movie"),
        "douban_north_america": ("movie_north_america", "Movie"),
        "douban_tv_domestic": ("tv_domestic", "Series"),
        "douban_tv_global": ("tv_global", "Series"),
    }

    def __init__(
        self,
        tmdb_key: str,
        tmdb_domain: str,
        language: str,
        regions: Sequence[str],
        limit: int,
        timeout: int,
        feed_url: str = "",
        feed_token: str = "",
        log: Optional[Callable[[str, str], None]] = None,
    ):
        self.tmdb_key = self._secret(tmdb_key)
        domain = str(tmdb_domain or "api.themoviedb.org").strip().rstrip("/")
        self.tmdb_base = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
        if not self.tmdb_base.endswith("/3"):
            self.tmdb_base += "/3"
        self.language = language or "zh-CN"
        self.regions = [x.strip().upper() for x in regions if x and x.strip()][:12] or ["US"]
        self.limit = max(20, min(int(limit), 300))
        self.timeout = max(5, min(int(timeout), 120))
        self.feed_url = str(feed_url or "").strip()
        self.feed_token = str(feed_token or "").strip()
        self.log = log or (lambda _level, _message: None)
        self._results: Dict[str, RankingResult] = {}
        self._feed: Dict[str, Any] = {}
        self._provider_ids: Dict[Tuple[str, str], Optional[int]] = {}
        self._provider_catalog: Dict[str, List[Dict[str, Any]]] = {}

    @staticmethod
    def _secret(value: Any) -> str:
        if hasattr(value, "get_secret_value"):
            try:
                return str(value.get_secret_value())
            except Exception:
                pass
        return str(value or "").strip()

    def fetch(self, selected: Iterable[str]) -> Dict[str, RankingResult]:
        self._load_feed()
        result: Dict[str, RankingResult] = {}
        for key in sorted(set(selected)):
            if key in RANK_META:
                result[key] = self._get(key)
        return result

    def _get(self, key: str) -> RankingResult:
        if key in self._results:
            return self._results[key]
        try:
            if key in self._feed:
                value = RankingResult(True, self._parse_feed_entries(self._feed[key]), "自定义榜单Feed")
            elif key.endswith("_mixed"):
                value = self._mixed(key)
            elif key in ("imdb_popular_movie", "imdb_popular_series"):
                value = self._imdb(key.endswith("movie"))
            elif key == "tmdb_trending":
                value = self._tmdb_trending()
            elif key == "anilist_popular":
                value = self._anilist()
            elif key == "bangumi_today":
                value = self._bangumi()
            elif key.startswith("douban_"):
                value = self._douban(key)
            elif key.startswith("maoyan_"):
                value = self._maoyan(key)
            elif key.startswith("tencent_"):
                value = self._tencent(key)
            else:
                value = self._platform(key)
        except Exception as err:
            value = RankingResult(False, set(), error=str(err))
        if value.ok and not value.entries:
            value = RankingResult(
                False, set(), source=value.source,
                error="榜单源返回 0 项，为防止误清空已保留旧集合",
            )
        self._results[key] = value
        if value.ok:
            self.log("INFO", f"榜单源 {RANK_META[key]['collection']}：取得 {len(value.entries)} 项（{value.source}）")
        else:
            self.log("WARNING", f"榜单源 {RANK_META[key]['collection']} 失败：{value.error}")
        return value

    def _mixed(self, key: str) -> RankingResult:
        if key == "popular_mixed":
            children = (
                "imdb_popular_movie", "imdb_popular_series", "tmdb_trending",
                "anilist_popular", "bangumi_today",
            )
        elif key == "maoyan_mixed":
            children = ("maoyan_movie", "maoyan_series", "maoyan_variety")
        elif key == "douban_mixed":
            children = tuple(self.DOUBAN_COLLECTIONS)
        elif key == "tencent_mixed":
            children = ("tencent_hot", "tencent_series", "tencent_kids", "tencent_movie",
                        "tencent_anime", "tencent_documentary")
        else:
            base = key[:-6]
            children = (f"{base}_movie", f"{base}_series")
        entries: Set[RankEntry] = set()
        ok_sources: List[str] = []
        errors: List[str] = []
        for child in children:
            item = self._get(child)
            if item.ok:
                entries.update(item.entries)
                ok_sources.append(item.source)
            elif item.error:
                errors.append(item.error)
        if errors:
            return RankingResult(
                False, set(),
                error="混合榜子源不完整，为防止移除旧成员已放弃本次更新："
                      + "；".join(errors),
            )
        if not ok_sources:
            return RankingResult(False, set(), error="；".join(errors) or "所有子榜单均不可用")
        return RankingResult(True, entries, "+".join(dict.fromkeys(ok_sources)))

    def _request(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> bytes:
        merged = {
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 MoviePilot-MediaVirtualLibrary/4.3.7",
        }
        merged.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json"
        last_error: Optional[BaseException] = None
        for attempt in range(3):
            request = urllib.request.Request(url, data=body, headers=merged, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as err:
                detail = err.read(240).decode("utf-8", "replace")
                last_error = RuntimeError(f"HTTP {err.code} {detail}")
                if err.code != 429 and err.code < 500:
                    raise last_error from err
                retry_after = str(err.headers.get("Retry-After") or "").strip()
                delay = min(5.0, float(retry_after)) if retry_after.isdigit() else 0.8 * (2 ** attempt)
            except (
                urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                http.client.HTTPException,
            ) as err:
                last_error = err
                delay = 0.8 * (2 ** attempt)
            if attempt < 2:
                self.log("WARNING", f"榜单请求暂时失败，{delay:.1f}秒后重试（{attempt + 1}/2）：{last_error}")
                time.sleep(delay)
        raise RuntimeError(str(last_error or "网络请求失败"))

    def _json(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        return json.loads(self._request(url, method, payload, headers).decode("utf-8"))

    def _load_feed(self) -> None:
        if not self.feed_url:
            return
        headers = {"Authorization": f"Bearer {self.feed_token}"} if self.feed_token else {}
        try:
            payload = self._json(self.feed_url, headers=headers)
            lists = payload.get("lists", payload) if isinstance(payload, dict) else {}
            if isinstance(lists, dict):
                self._feed = lists
                self.log("INFO", f"自定义榜单Feed已加载：{len(lists)} 个榜单")
        except Exception as err:
            self.log("WARNING", f"自定义榜单Feed加载失败，将使用内置来源：{err}")

    def _parse_feed_entries(self, value: Any) -> Set[RankEntry]:
        if isinstance(value, dict):
            value = value.get("items") or value.get("entries") or value.get("results") or []
        result: Set[RankEntry] = set()
        for item in value if isinstance(value, list) else []:
            if isinstance(item, str):
                if re.fullmatch(r"tt\d{7,10}", item):
                    result.add(RankEntry(imdb=item))
                continue
            if not isinstance(item, dict):
                continue
            media_type = self._media_type(item.get("type") or item.get("media_type"))
            title = str(item.get("title") or item.get("name") or "").strip()
            year = self._year(item.get("year") or item.get("production_year") or item.get("release_date"))
            result.add(RankEntry(
                media_type=media_type,
                tmdb=str(item.get("tmdb") or item.get("tmdb_id") or ""),
                imdb=str(item.get("imdb") or item.get("imdb_id") or ""),
                tvdb=str(item.get("tvdb") or item.get("tvdb_id") or ""),
                anilist=str(item.get("anilist") or item.get("anilist_id") or ""),
                bangumi=str(item.get("bangumi") or item.get("bangumi_id") or ""),
                douban=str(item.get("douban") or item.get("douban_id") or ""),
                title=title,
                original_title=str(item.get("original_title") or ""),
                year=year,
            ))
        return {entry for entry in result if any((
            entry.tmdb, entry.imdb, entry.tvdb, entry.anilist,
            entry.bangumi, entry.douban, entry.title,
        ))}

    def _tmdb(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if not self.tmdb_key:
            raise RuntimeError("未取得 TMDB API Key；请配置 MoviePilot TMDB 或在插件中填写覆盖Key")
        query = {"api_key": self.tmdb_key, "language": self.language}
        query.update(params or {})
        url = f"{self.tmdb_base}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        payload = self._json(url)
        if not isinstance(payload, dict):
            raise RuntimeError("TMDB 返回格式异常")
        return payload

    @staticmethod
    def _from_tmdb(item: Mapping[str, Any], media_type: str = "") -> Optional[RankEntry]:
        kind = media_type or str(item.get("media_type") or "")
        kind = "Movie" if kind.casefold() == "movie" else "Series" if kind.casefold() in ("tv", "series") else ""
        tmdb_id = item.get("id")
        if not tmdb_id or not kind:
            return None
        release = item.get("release_date") or item.get("first_air_date") or ""
        return RankEntry(
            media_type=kind, tmdb=str(tmdb_id),
            title=str(item.get("title") or item.get("name") or ""),
            original_title=str(item.get("original_title") or item.get("original_name") or ""),
            year=RankingFetcher._year(release),
        )

    def _tmdb_pages(self, path: str, params: Mapping[str, Any], media_type: str = "") -> Set[RankEntry]:
        entries: Set[RankEntry] = set()
        pages = min(5, max(1, math.ceil(self.limit / 20)))
        for page in range(1, pages + 1):
            payload = self._tmdb(path, {**params, "page": page})
            for item in payload.get("results") or []:
                entry = self._from_tmdb(item, media_type)
                if entry:
                    entries.add(entry)
                    if len(entries) >= self.limit:
                        return entries
            if page >= int(payload.get("total_pages") or page):
                break
        return entries

    def _tmdb_trending(self) -> RankingResult:
        return RankingResult(
            True, self._tmdb_pages("/trending/all/week", {}, ""), "TMDB官方趋势"
        )

    def _provider_id(self, platform: str, media_type: str) -> int:
        cache_key = (platform, media_type)
        if cache_key in self._provider_ids:
            value = self._provider_ids[cache_key]
            if value is None:
                raise RuntimeError(f"TMDB 未找到 {platform} 的 Watch Provider")
            return value
        catalog_key = "movie" if media_type == "Movie" else "tv"
        if catalog_key not in self._provider_catalog:
            payload = self._tmdb(f"/watch/providers/{catalog_key}")
            self._provider_catalog[catalog_key] = [
                provider for provider in (payload.get("results") or [])
                if isinstance(provider, dict)
            ]
        providers = self._provider_catalog[catalog_key]
        wanted = self.PLATFORM_PROVIDERS.get(platform, ())
        found = None
        normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
        wanted_normalized = {normalize(value) for value in wanted}
        for exact in wanted:
            for provider in providers:
                if str(provider.get("provider_name") or "").casefold() == exact.casefold():
                    found = int(provider["provider_id"])
                    break
            if found is not None:
                break
        if found is None:
            # 兼容 Apple TV+ / Apple TV Plus 等标点与本地化差异。
            for provider in providers:
                if normalize(provider.get("provider_name")) in wanted_normalized:
                    found = int(provider["provider_id"])
                    break
        if found is None:
            provider_ids = {
                int(provider.get("provider_id")) for provider in providers
                if str(provider.get("provider_id") or "").isdigit()
            }
            found = next(
                (provider_id for provider_id in self.PLATFORM_PROVIDER_IDS.get(platform, ())
                 if provider_id in provider_ids),
                None,
            )
        self._provider_ids[cache_key] = found
        if found is None:
            raise RuntimeError(f"TMDB 未找到 {platform} 的 Watch Provider")
        return found

    def _platform(self, key: str) -> RankingResult:
        suffix = "movie" if key.endswith("_movie") else "series" if key.endswith("_series") else ""
        if not suffix:
            raise RuntimeError("无法识别平台榜单类型")
        platform = key[:-(len(suffix) + 1)]
        media_type = "Movie" if suffix == "movie" else "Series"
        provider_id = self._provider_id(platform, media_type)
        entries: Set[RankEntry] = set()
        # 每个地区只取前一页热门项，再合并去重，避免“全部地区”造成过多请求。
        per_region = max(20, min(40, math.ceil(self.limit / max(1, len(self.regions)))))
        pages = max(1, math.ceil(per_region / 20))
        for region in self.regions:
            for page in range(1, pages + 1):
                payload = self._tmdb(
                    f"/discover/{'movie' if media_type == 'Movie' else 'tv'}",
                    {
                        "watch_region": region, "with_watch_providers": provider_id,
                        "sort_by": "popularity.desc", "page": page,
                    },
                )
                for item in payload.get("results") or []:
                    entry = self._from_tmdb(item, media_type)
                    if entry:
                        entries.add(entry)
                if len(entries) >= self.limit:
                    break
            if len(entries) >= self.limit:
                break
        return RankingResult(True, set(list(entries)[:self.limit]), "TMDB Watch Provider多地区汇总")

    def _imdb(self, movie: bool) -> RankingResult:
        path = "moviemeter" if movie else "tvmeter"
        raw = self._request(f"https://www.imdb.com/chart/{path}/").decode("utf-8", "replace")
        ids: List[str] = []
        seen: Set[str] = set()
        for imdb_id in re.findall(r"tt\d{7,10}", raw):
            if imdb_id not in seen:
                seen.add(imdb_id)
                ids.append(imdb_id)
            if len(ids) >= self.limit:
                break
        if not ids:
            raise RuntimeError("IMDb 页面未解析到条目")
        media_type = "Movie" if movie else "Series"
        return RankingResult(True, {RankEntry(media_type=media_type, imdb=x) for x in ids}, "IMDb榜单页")

    def _anilist(self) -> RankingResult:
        query = """
        query ($page: Int, $perPage: Int) {
          Page(page: $page, perPage: $perPage) {
            media(type: ANIME, sort: POPULARITY_DESC) {
              id idMal seasonYear format
              title { romaji english native }
            }
          }
        }
        """
        payload = self._json(
            "https://graphql.anilist.co", "POST",
            {"query": query, "variables": {"page": 1, "perPage": min(50, self.limit)}},
        )
        media = (((payload or {}).get("data") or {}).get("Page") or {}).get("media") or []
        entries: Set[RankEntry] = set()
        for item in media:
            titles = item.get("title") or {}
            kind = "Movie" if str(item.get("format") or "").upper() == "MOVIE" else "Series"
            entries.add(RankEntry(
                media_type=kind, anilist=str(item.get("id") or ""),
                title=str(titles.get("native") or titles.get("english") or titles.get("romaji") or ""),
                original_title=str(titles.get("romaji") or ""), year=self._year(item.get("seasonYear")),
            ))
        if not entries:
            raise RuntimeError("AniList 未返回条目")
        return RankingResult(True, entries, "AniList官方GraphQL")

    def _bangumi(self) -> RankingResult:
        payload = self._json(
            "https://api.bgm.tv/calendar",
            headers={"User-Agent": "MoviePilot-MediaVirtualLibrary/4.3.7 (private use)"},
        )
        today = date.today().isoweekday()
        groups = payload if isinstance(payload, list) else []
        selected = []
        for group in groups:
            weekday = group.get("weekday") or {}
            if int(weekday.get("id") or 0) == today:
                selected = group.get("items") or []
                break
        entries = {
            RankEntry(
                media_type="Series", bangumi=str(item.get("id") or ""),
                title=str(item.get("name_cn") or item.get("name") or ""),
                original_title=str(item.get("name") or ""), year=self._year(item.get("air_date")),
            )
            for item in selected if item.get("id")
        }
        return RankingResult(True, entries, "Bangumi每日放送API")

    def _douban(self, key: str) -> RankingResult:
        if key not in self.DOUBAN_COLLECTIONS:
            raise RuntimeError("未配置该豆瓣动态榜单")
        collection, default_type = self.DOUBAN_COLLECTIONS[key]
        url = (
            f"https://m.douban.com/rexxar/api/v2/subject_collection/{collection}/items?"
            + urllib.parse.urlencode({"start": 0, "count": self.limit, "items_only": 1})
        )
        payload = self._json(url, headers={"Referer": "https://m.douban.com/"})
        items = payload.get("subject_collection_items") or payload.get("items") or []
        entries: Set[RankEntry] = set()
        for row in items:
            subject = row.get("subject") if isinstance(row.get("subject"), dict) else row
            if not isinstance(subject, dict):
                continue
            subtype = str(subject.get("type") or subject.get("subtype") or "").casefold()
            media_type = "Series" if subtype in ("tv", "series") else default_type
            entries.add(RankEntry(
                media_type=media_type, douban=str(subject.get("id") or ""),
                title=str(subject.get("title") or subject.get("name") or ""),
                original_title=str(subject.get("original_title") or ""),
                year=self._year(subject.get("year") or subject.get("release_date")),
            ))
        if not entries:
            raise RuntimeError("豆瓣接口未返回条目")
        return RankingResult(True, entries, "豆瓣移动端公开集合")

    def _maoyan(self, key: str) -> RankingResult:
        media_type = "Movie" if key == "maoyan_movie" else "Series"
        url = "https://m.maoyan.com/asgard/board" if key == "maoyan_movie" else "https://piaofang.maoyan.com/web-heat"
        raw = self._request(url, headers={"Referer": "https://piaofang.maoyan.com/"}).decode("utf-8", "replace")
        entries = self._html_title_entries(raw, media_type)
        if not entries:
            raise RuntimeError("猫眼页面未解析到榜单条目；可用自定义Feed覆盖此榜单")
        return RankingResult(True, set(list(entries)[:self.limit]), "猫眼榜单页兼容解析")

    def _tencent(self, key: str) -> RankingResult:
        if key in ("tencent_hot", "tencent_mixed"):
            movie = self._tencent_discover("Movie", "")
            series = self._tencent_discover("Series", "")
            return RankingResult(True, movie | series, "TMDB腾讯视频多地区汇总")
        mapping = {
            "tencent_series": ("Series", ""), "tencent_kids": ("Series", "10762"),
            "tencent_movie": ("Movie", ""), "tencent_anime": ("Series", "16"),
            "tencent_documentary": ("Mixed", "99"),
        }
        media_type, genre = mapping.get(key, ("", ""))
        if media_type == "Mixed":
            entries = self._tencent_discover("Movie", genre) | self._tencent_discover("Series", genre)
        elif media_type:
            entries = self._tencent_discover(media_type, genre)
        else:
            raise RuntimeError("无法识别腾讯榜单")
        return RankingResult(True, entries, "TMDB腾讯视频多地区汇总")

    def _tencent_discover(self, media_type: str, genre: str) -> Set[RankEntry]:
        provider_id = self._provider_id("tencent", media_type)
        entries: Set[RankEntry] = set()
        for region in self.regions:
            params: Dict[str, Any] = {
                "watch_region": region, "with_watch_providers": provider_id,
                "sort_by": "popularity.desc", "page": 1,
            }
            if genre:
                params["with_genres"] = genre
            payload = self._tmdb(f"/discover/{'movie' if media_type == 'Movie' else 'tv'}", params)
            for item in payload.get("results") or []:
                entry = self._from_tmdb(item, media_type)
                if entry:
                    entries.add(entry)
            if len(entries) >= self.limit:
                break
        return set(list(entries)[:self.limit])

    @classmethod
    def _html_title_entries(cls, raw: str, media_type: str) -> Set[RankEntry]:
        values: List[str] = []
        patterns = (
            r'"(?:movieName|seriesName|programName|showName|nm|title)"\s*:\s*"([^"\\]{1,100}(?:\\.[^"\\]*)*)"',
            r'<(?:h2|h3|p|span)[^>]+class="[^"]*(?:name|title)[^"]*"[^>]*>([^<]{1,100})<',
        )
        for pattern in patterns:
            for value in re.findall(pattern, raw, flags=re.I):
                try:
                    value = json.loads(f'"{value}"')
                except Exception:
                    value = html.unescape(re.sub(r"<[^>]+>", "", value))
                value = re.sub(r"\s+", " ", value).strip()
                if 1 < len(value) <= 80 and value not in values:
                    values.append(value)
        return {RankEntry(media_type=media_type, title=value) for value in values}

    @staticmethod
    def _media_type(value: Any) -> str:
        text = str(value or "").casefold()
        if text in ("movie", "mov", "电影"):
            return "Movie"
        if text in ("tv", "series", "show", "电视剧", "剧集", "综艺", "anime"):
            return "Series"
        return ""

    @staticmethod
    def _year(value: Any) -> int:
        match = re.search(r"(?:19|20)\d{2}", str(value or ""))
        return int(match.group()) if match else 0


class LibraryIndex:
    """把外部榜单身份映射到当前 Emby 已入库的原 ItemId。"""

    PROVIDERS = {
        "tmdb": ("tmdb", "themoviedb"), "imdb": ("imdb",), "tvdb": ("tvdb",),
        "anilist": ("anilist",), "bangumi": ("bangumi", "bgm"), "douban": ("douban",),
    }
    PROVIDER_NAMES = {alias: name for name, aliases in PROVIDERS.items() for alias in aliases}

    def __init__(self, items: Iterable[Mapping[str, Any]]):
        self.provider: Dict[Tuple[str, str, str], Set[str]] = {}
        self.title_year: Dict[Tuple[str, str, int], Set[str]] = {}
        self.title_only: Dict[Tuple[str, str], Set[str]] = {}
        for item in items:
            if not item.get("Id"):
                continue
            item_id = str(item["Id"])
            kind = self._type(item.get("Type"))
            providers = item.get("ProviderIds") or {}
            if isinstance(providers, Mapping):
                for raw_key, raw_value in providers.items():
                    provider = self._provider_name(str(raw_key))
                    if provider and raw_value not in (None, ""):
                        self.provider.setdefault((kind, provider, str(raw_value).casefold()), set()).add(item_id)
            year = RankingFetcher._year(item.get("ProductionYear") or item.get("PremiereDate"))
            titles = {self._normalize(item.get(field)) for field in ("Name", "OriginalTitle", "SortName")} - {""}
            for title in titles:
                self.title_only.setdefault((kind, title), set()).add(item_id)
                if year:
                    self.title_year.setdefault((kind, title, year), set()).add(item_id)

    def match(self, entries: Iterable[RankEntry]) -> Set[str]:
        result: Set[str] = set()
        for entry in entries:
            kinds = [entry.media_type] if entry.media_type in ("Movie", "Series") else ["Movie", "Series"]
            matched: Set[str] = set()
            for provider in self.PROVIDERS:
                value = str(getattr(entry, provider) or "").casefold()
                if not value:
                    continue
                for kind in kinds:
                    matched.update(self.provider.get((kind, provider, value), set()))
            if matched:
                result.update(matched)
                continue
            titles = {self._normalize(entry.title), self._normalize(entry.original_title)} - {""}
            for kind in kinds:
                for title in titles:
                    if entry.year:
                        matched.update(self.title_year.get((kind, title, entry.year), set()))
                    else:
                        candidates = self.title_only.get((kind, title), set())
                        # 无年份时只有唯一同名项目才允许回退，避免误投射重拍片。
                        if len(candidates) == 1:
                            matched.update(candidates)
            result.update(matched)
        return result

    @staticmethod
    def _type(value: Any) -> str:
        return "Series" if str(value or "").casefold() == "series" else "Movie"

    @classmethod
    def _provider_name(cls, raw: str) -> str:
        folded = re.sub(r"[^a-z0-9]", "", raw.casefold())
        return cls.PROVIDER_NAMES.get(folded, "")

    @staticmethod
    def _normalize(value: Any) -> str:
        text = html.unescape(str(value or "")).casefold()
        return re.sub(r"[^\w\u3400-\u9fff]+", "", text, flags=re.UNICODE)


class MediaArchiver(_PluginBase):
    """媒体属性专区与独立榜单首页虚拟库。"""

    plugin_name = "媒体虚拟库"
    plugin_desc = "复用MoviePilot与NextEmby现有端口输出一级虚拟库，不创建合集。"
    plugin_icon = "folder-move.svg"
    plugin_version = "4.3.7"
    plugin_author = "Boss"
    author_url = "https://github.com/ZangBanzi"
    plugin_config_prefix = "mediaarchiver_"
    plugin_order = 50
    auth_level = 1
    PUBLIC_GATEWAY_PORT = 8098
    GATEWAY_ROUTE_NAME = "MediaArchiver_emby_gateway"

    ATTRIBUTE_RULES: Dict[str, Dict[str, str]] = {
        "remux": {"name": "Remux专区", "icon": "mdi-disc", "hint": "路径、文件名或媒体源信息包含 Remux"},
        "4k": {"name": "4K专区", "icon": "mdi-video-4k-box", "hint": "视频宽度≥3840或高度≥2160"},
        "dolby_vision": {"name": "Dolby Vision专区", "icon": "mdi-eye-circle", "hint": "视频流 DV/Dolby Vision 信息"},
        "hdr": {"name": "HDR专区", "icon": "mdi-brightness-7", "hint": "HDR10/HDR10+/HLG/PQ/DV"},
        "atmos": {"name": "Atmos专区", "icon": "mdi-surround-sound", "hint": "音频流 Atmos/JOC 信息"},
    }
    # 一级虚拟库封面模板。只保存品牌识别色与文字标志，不在线下载图片；
    # 这样断网也能生成封面，同时避免把外部图片地址写入 Emby。
    COVER_THEMES: Dict[str, Dict[str, str]] = {
        "remux": {"logo": "REMUX", "bg": "#090B10", "bg2": "#242A35", "accent": "#D4AF37", "fg": "#FFFFFF"},
        "4k": {"logo": "4K UHD", "bg": "#07131C", "bg2": "#123C52", "accent": "#00C8FF", "fg": "#FFFFFF"},
        "dolby_vision": {"logo": "DOLBY VISION", "bg": "#050505", "bg2": "#262626", "accent": "#FFFFFF", "fg": "#FFFFFF"},
        "hdr": {"logo": "HDR", "bg": "#180A25", "bg2": "#5B1B72", "accent": "#FFB000", "fg": "#FFFFFF"},
        "atmos": {"logo": "DOLBY ATMOS", "bg": "#07121A", "bg2": "#173A4E", "accent": "#7BD8FF", "fg": "#FFFFFF"},
        "popular": {"logo": "HOT", "bg": "#2A0B24", "bg2": "#6E1933", "accent": "#FF7139", "fg": "#FFFFFF"},
        "netflix": {"logo": "N", "bg": "#050505", "bg2": "#1A0507", "accent": "#E50914", "fg": "#FFFFFF"},
        "hbo": {"logo": "HBO", "bg": "#050505", "bg2": "#242424", "accent": "#F2F2F2", "fg": "#FFFFFF"},
        "apple_tv": {"logo": "tv+", "bg": "#050505", "bg2": "#252525", "accent": "#FFFFFF", "fg": "#FFFFFF"},
        "disney_plus": {"logo": "Disney+", "bg": "#06143F", "bg2": "#123F95", "accent": "#55B7FF", "fg": "#FFFFFF"},
        "crunchyroll": {"logo": "CRUNCHYROLL", "bg": "#2B160A", "bg2": "#6A2A0B", "accent": "#F47521", "fg": "#FFFFFF"},
        "amazon_prime": {"logo": "prime video", "bg": "#061D2A", "bg2": "#063E59", "accent": "#00A8E1", "fg": "#FFFFFF"},
        "amazon": {"logo": "amazon", "bg": "#101820", "bg2": "#273746", "accent": "#FF9900", "fg": "#FFFFFF"},
        "hulu": {"logo": "hulu", "bg": "#041F16", "bg2": "#07452E", "accent": "#1CE783", "fg": "#FFFFFF"},
        "maoyan": {"logo": "MAOYAN", "bg": "#2D070A", "bg2": "#83151E", "accent": "#F03D37", "fg": "#FFFFFF"},
        "douban": {"logo": "DOUBAN", "bg": "#062116", "bg2": "#0B5D37", "accent": "#00B51D", "fg": "#FFFFFF"},
        "tencent": {"logo": "TENCENT VIDEO", "bg": "#07172C", "bg2": "#0C4165", "accent": "#20D36B", "fg": "#FFFFFF"},
        "default": {"logo": "VIRTUAL", "bg": "#111827", "bg2": "#3730A3", "accent": "#818CF8", "fg": "#FFFFFF"},
    }
    EVENT_TYPES = {
        "library.new", "itemadded", "library.updated", "library.update", "itemupdated",
        "item.updated", "library.deleted", "itemremoved", "itemdeleted", "item.removed",
    }

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._attribute_enabled = False
        self._ranking_enabled = False
        self._auto_sync = False
        self._daily_sync_enabled = True
        self._sync_cron = "0 4 * * *"
        self._sync_cron_error = ""
        self._emby_servers: List[str] = []
        self._proxy_lock = threading.RLock()
        self._virtual_views: Dict[str, Dict[str, Any]] = {}
        self._proxy_item_index: Dict[str, Dict[str, Any]] = {}
        self._cover_cache: Dict[str, Tuple[str, bytes]] = {}
        self._cover_lock = threading.Lock()
        self._selection_cache: OrderedDict = OrderedDict()
        self._selection_index: Any = None
        self._proxy_status: Dict[str, Any] = {
            "running": False, "message": "MoviePilot 内置网关未注册", "requests": 0,
        }
        self._gateway_client_cache: Optional[EmbyClient] = None
        self._gateway_client_lock = threading.Lock()
        self._gateway_server_name = ""
        self._gateway_api_root = ""
        self._gateway_routes_installed = False
        self._httpx_client: Any = None
        self._httpx_loop: Optional[asyncio.AbstractEventLoop] = None
        self._httpx_lock = threading.Lock()
        self._gateway_metrics_lock = threading.Lock()
        self._gateway_metrics: Dict[str, int] = {
            "inflight": 0, "peak_inflight": 0, "failures": 0,
            "suppressed_errors": 0, "active_streams": 0, "decode_recoveries": 0,
            "decode_retries": 0,
        }
        self._gateway_last_error_at = float("-inf")
        self._gateway_suppressed_errors = 0
        self._gateway_last_error: Dict[str, Any] = {}
        self._decoder_status = {
            "gzip": True, "deflate": True,
            "br": bool(importlib.util.find_spec("brotli") or importlib.util.find_spec("brotlicffi")),
            "zstd": bool(importlib.util.find_spec("zstandard")),
        }
        self._timeout = 30
        self._sync_interval = 60
        self._enabled_rules: Set[str] = set(self.ATTRIBUTE_RULES)
        self._selected_rankings: Set[str] = set(DEFAULT_RANKINGS)
        self._tmdb_key = ""
        self._tmdb_domain = ""
        self._ranking_language = "zh-CN"
        self._ranking_regions: List[str] = ["US", "GB", "JP", "KR", "HK", "TW"]
        self._ranking_limit = 100
        self._ranking_feed_url = ""
        self._ranking_feed_token = ""
        self._run_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._pending_ids: Dict[str, Set[str]] = {}
        self._event_timer: Optional[threading.Timer] = None
        self._boot_timer: Optional[threading.Timer] = None
        self._stopping = False
        self._logs = deque(maxlen=300)
        self._state: Dict[str, Any] = {"servers": {}, "last_sync": "", "source_status": {}}
        self._runtime: Dict[str, Any] = {
            "state": "idle", "message": "尚未同步", "mode": "", "stats": {}
        }
        self._ranking_cache: Dict[str, RankingResult] = {}

    def init_plugin(self, config: Optional[dict] = None) -> None:
        self._remove_gateway_routes()
        self._close_httpx_client()
        self._cancel_timers()
        self._stopping = False
        cfg = dict(config or {})
        # v3.2 起主页面只保留功能开关：任一虚拟库开启即视为插件启用。
        # 仍读取旧 enabled 字段，保证从 v3.1 及更早版本无损升级。
        self._enabled = bool(
            cfg.get("enabled", False)
            or cfg.get("virtual_enabled", cfg.get("attribute_enabled", False))
            or cfg.get("ranking_enabled", False)
        )
        self._attribute_enabled = bool(cfg.get("virtual_enabled", cfg.get("attribute_enabled", False)))
        self._ranking_enabled = bool(cfg.get("ranking_enabled", False))
        self._auto_sync = bool(cfg.get("auto_sync", True))
        self._daily_sync_enabled = bool(cfg.get("daily_sync_enabled", True))
        self._sync_cron, self._sync_cron_error = self._normalize_sync_cron(
            cfg.get("sync_cron", "0 4 * * *")
        )
        selected_servers = cfg.get("emby_servers") or []
        if isinstance(selected_servers, str):
            selected_servers = [selected_servers]
        selected_server = str(cfg.get("emby_server") or "").strip()
        # 新界面为单服务器选择；一旦用户明确选择，就不要被旧版隐藏的
        # emby_servers 列表抢占优先级。仅在新字段为空时兼容旧配置。
        if selected_server:
            selected_servers = [selected_server]
        self._emby_servers = [str(x).strip() for x in selected_servers if str(x).strip()]
        self._gateway_client_cache = None
        self._gateway_server_name = ""
        self._gateway_api_root = ""
        self._timeout = self._bounded_int(cfg.get("timeout"), 30, 5, 120)
        self._sync_interval = self._bounded_int(cfg.get("sync_interval"), 60, 10, 1440)
        self._ranking_limit = self._bounded_int(cfg.get("ranking_limit"), 100, 20, 300)
        self._enabled_rules = {
            key for key in self.ATTRIBUTE_RULES if bool(cfg.get(f"zone_{key}", True))
        }
        self._selected_rankings = {
            key for key in RANK_META
            if bool(cfg.get(f"rank_{key}", key in DEFAULT_RANKINGS))
        }
        self._tmdb_key = str(cfg.get("tmdb_api_key") or "").strip()
        self._tmdb_domain = str(cfg.get("tmdb_domain") or "").strip()
        self._ranking_language = str(cfg.get("ranking_language") or "zh-CN").strip()
        region_value = cfg.get("ranking_regions") or "US,GB,JP,KR,HK,TW"
        if isinstance(region_value, str):
            region_value = region_value.split(",")
        self._ranking_regions = [str(x).strip().upper() for x in region_value if str(x).strip()][:12]
        self._ranking_feed_url = str(cfg.get("ranking_feed_url") or "").strip()
        self._ranking_feed_token = str(cfg.get("ranking_feed_token") or "").strip()
        self._load_state()
        self._restore_proxy_cache()

        if self._daily_sync_enabled and self._sync_cron_error:
            self._record(
                "WARNING",
                f"Cron 表达式无效：{self._sync_cron_error}；已回退为 {self._sync_cron}",
            )

        if self.get_state():
            self._install_gateway_routes()

        # 启动后的轻量校准只属于“实时增量维护”。仅开启每日定时时，
        # 必须严格等到用户设置的时刻再执行，避免重启 MoviePilot 就意外全扫。
        if self.get_state() and self._auto_sync:
            self._boot_timer = threading.Timer(15.0, self._scheduled_sync)
            self._boot_timer.daemon = True
            self._boot_timer.start()

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    def _cron_trigger(self, expression: Optional[str] = None) -> Any:
        """按 MoviePilot 时区创建标准五段 Cron 触发器。"""
        if CronTrigger is None:
            raise RuntimeError("当前 MoviePilot 环境缺少 APScheduler CronTrigger")
        cron = str(expression or self._sync_cron).strip()
        timezone = str(getattr(settings, "TZ", "") or "").strip()
        if timezone:
            try:
                return CronTrigger.from_crontab(cron, timezone=timezone)
            except Exception:
                # 极少数旧 APScheduler 不接受字符串时区；交给宿主调度器时区。
                return CronTrigger.from_crontab(cron)
        return CronTrigger.from_crontab(cron)

    def _normalize_sync_cron(self, value: Any) -> Tuple[str, str]:
        """规范并验证五段 Cron；错误时回退为每天 04:00。"""
        default = "0 4 * * *"
        cron = re.sub(r"\s+", " ", str(value or default).strip())
        if len(cron.split(" ")) != 5:
            return default, f"{cron!r} 不是标准五段 Cron"
        if CronTrigger is not None:
            try:
                self._cron_trigger(cron)
            except Exception as err:
                return default, f"{cron!r}（{err}）"
        return cron, ""

    def _load_state(self) -> None:
        try:
            state = self.get_data("virtual_state") or {}
            logs = self.get_data("virtual_logs") or []
            if isinstance(state, dict):
                if "servers" in state:
                    self._state.update(state)
                elif state.get("server"):
                    # v3.0.0 单服务器状态迁移。
                    self._state["servers"] = {
                        str(state["server"]): {
                            "name": str(state["server"]).split("|", 1)[0],
                            "attribute_collections": dict(state.get("collections") or {}),
                            "ranking_collections": {},
                            "attribute_counts": dict(state.get("counts") or {}),
                            "ranking_counts": {}, "last_sync": state.get("last_sync") or "",
                        }
                    }
            if isinstance(logs, list):
                self._logs = deque(logs[-300:], maxlen=300)
        except Exception as err:
            logger.warning("[媒体虚拟库] 读取状态失败：%s", err)

    def _save_state(self) -> None:
        self.save_data("virtual_state", self._state)
        self.save_data("virtual_logs", list(self._logs))

    @staticmethod
    def _view_id(key: str) -> str:
        """生成 Emby 风格的稳定 32 位 ID，同一专区重启后不变。"""
        return hashlib.md5(f"mediaarchiver:{key}".encode("utf-8")).hexdigest()

    def _restore_proxy_cache(self) -> None:
        """从持久状态恢复虚拟库 ID/成员，等后台同步刷新媒体元数据。"""
        states = [
            value for value in (self._state.get("servers") or {}).values()
            if isinstance(value, dict) and value.get("active", True)
        ]
        selected: Optional[Dict[str, Any]] = None
        wanted_name = self._emby_servers[0] if self._emby_servers else ""
        if wanted_name:
            for value in states:
                if str(value.get("name") or "") == wanted_name:
                    selected = value
                    break
        if selected is None and states:
            selected = states[0]
        restored: Dict[str, Dict[str, Any]] = {}
        if selected:
            for key, view in (selected.get("virtual_views") or {}).items():
                if not isinstance(view, Mapping):
                    continue
                item_ids = [str(x) for x in (view.get("item_ids") or []) if str(x)]
                normalized = dict(view)
                normalized.update({
                    "key": str(view.get("key") or key),
                    "id": str(view.get("id") or self._view_id(str(key))),
                    "item_ids": list(dict.fromkeys(item_ids)),
                })
                normalized["cover_tag"] = str(
                    view.get("cover_tag")
                    or self._cover_tag(str(normalized["key"]), normalized["item_ids"])
                )
                restored[str(normalized["id"])] = normalized
        with self._proxy_lock:
            self._virtual_views = restored

    @staticmethod
    def _moviepilot_api_port() -> int:
        try:
            return int(getattr(settings, "PORT", 3001) or 3001)
        except (TypeError, ValueError):
            return 3001

    def _install_gateway_routes(self) -> None:
        """把网关挂到 MoviePilot 已有 FastAPI，不创建监听器、不占用 8098。"""
        if self._gateway_routes_installed or self._stopping:
            return
        if not _FASTAPI_AVAILABLE:
            self._proxy_status = {
                "running": False, "message": "当前宿主未提供 FastAPI 路由能力", "requests": 0,
            }
            return
        try:
            from app.factory import app as moviepilot_app

            self._remove_gateway_routes(moviepilot_app)
            methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
            try:
                moviepilot_app.router.add_api_route(
                    "/{emby_path:path}", self._emby_gateway_http,
                    methods=methods, name=f"{self.GATEWAY_ROUTE_NAME}_http",
                    include_in_schema=False, route_class_override=EmbyGatewayRoute,
                )
            except TypeError:
                # 兼容不支持 route_class_override 的较早 FastAPI。
                moviepilot_app.router.routes.append(EmbyGatewayRoute(
                    path="/{emby_path:path}", endpoint=self._emby_gateway_http,
                    methods=methods, name=f"{self.GATEWAY_ROUTE_NAME}_http",
                    include_in_schema=False,
                ))
            moviepilot_app.router.add_api_websocket_route(
                "/socket", self._emby_gateway_websocket,
                name=f"{self.GATEWAY_ROUTE_NAME}_socket",
            )
            moviepilot_app.router.add_api_websocket_route(
                "/emby/socket", self._emby_gateway_websocket,
                name=f"{self.GATEWAY_ROUTE_NAME}_emby_socket",
            )
            moviepilot_app.openapi_schema = None
            self._gateway_routes_installed = True
            self._proxy_status = {
                "running": True,
                "message": f"已复用 MoviePilot API 端口 {self._moviepilot_api_port()}（未新增监听端口）",
                "requests": 0,
                "api_port": self._moviepilot_api_port(),
                "public_port": self.PUBLIC_GATEWAY_PORT,
            }
            self._record(
                "INFO",
                f"v{self.plugin_version} code={SOURCE_SHA256[:12]}："
                f"一级虚拟库网关已挂载到 MoviePilot:{self._moviepilot_api_port()}；"
                f"NextEmby 对外端口保持 {self.PUBLIC_GATEWAY_PORT}",
            )
        except Exception as err:
            self._gateway_routes_installed = False
            self._proxy_status = {
                "running": False, "message": f"MoviePilot 内置网关注册失败：{err}", "requests": 0,
            }
            self._record("ERROR", str(self._proxy_status["message"]))

    def _remove_gateway_routes(self, moviepilot_app: Any = None) -> None:
        try:
            if moviepilot_app is None:
                from app.factory import app as moviepilot_app
            routes = list(getattr(moviepilot_app, "routes", []) or [])
            removed = False
            for route in routes:
                if str(getattr(route, "name", "") or "").startswith(self.GATEWAY_ROUTE_NAME):
                    moviepilot_app.routes.remove(route)
                    removed = True
            if removed:
                moviepilot_app.openapi_schema = None
        except Exception:
            pass
        self._gateway_routes_installed = False
        status = getattr(self, "_proxy_status", {})
        self._proxy_status = {
            "running": False, "message": "MoviePilot 内置网关已停止",
            "requests": int(status.get("requests") or 0),
        }

    def get_state(self) -> bool:
        return bool(self._enabled and (self._attribute_enabled or self._ranking_enabled))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        services: List[Dict[str, Any]] = []
        if self._auto_sync and IntervalTrigger:
            services.append({
                "id": "MediaArchiver.virtual_library_reconcile",
                "name": "媒体虚拟库增量校准",
                "trigger": IntervalTrigger(minutes=self._sync_interval),
                "func": self._scheduled_sync,
                "kwargs": {"mode": "incremental"},
            })
        if self._daily_sync_enabled and CronTrigger:
            try:
                services.append({
                    "id": "MediaArchiver.timed_full_sync",
                    "name": f"媒体虚拟库定时全量更新 {self._sync_cron}",
                    "trigger": self._cron_trigger(),
                    "func": self._scheduled_sync,
                    "kwargs": {"mode": "timed", "schedule": self._sync_cron},
                })
            except Exception as err:
                logger.error("[媒体虚拟库] 定时任务注册失败：%s", err)
        return services

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/test_connection", "endpoint": self.test_connection, "methods": ["POST"],
             "auth": "bear", "summary": "测试 MoviePilot 已配置的 Emby"},
            {"path": "/rebuild", "endpoint": self.rebuild, "methods": ["POST"],
             "auth": "bear", "summary": "一键重建全部虚拟库"},
            {"path": "/status", "endpoint": self.status, "methods": ["GET"],
             "auth": "bear", "summary": "查询同步状态"},
        ]

    def rebuild(self) -> schemas.Response:
        return self._start_sync("rebuild")

    def test_connection(self) -> schemas.Response:
        """测试已保存的 Emby 连接；只读取 System/Info，不扫描媒体库。"""
        tested_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            clients = self._create_clients()
            names = [f"{name}（{client.api_root}）" for client, name in clients]
            message = f"连接成功：{'、'.join(names)}"
            result = {
                "ok": True, "message": message, "time": tested_at,
                "servers": len(clients),
            }
            self._record("INFO", message)
        except Exception as err:
            message = f"连接失败：{err}"
            result = {"ok": False, "message": message, "time": tested_at, "servers": 0}
            self._record("ERROR", message)
        self._state["connection_test"] = result
        try:
            self._save_state()
        except Exception as err:
            logger.warning("[媒体虚拟库] 保存连接测试状态失败：%s", err)
        return schemas.Response(success=bool(result["ok"]), message=message, data=result)

    def status(self) -> schemas.Response:
        data = dict(self._runtime)
        active_servers = sum(
            1 for state in (self._state.get("servers") or {}).values()
            if not isinstance(state, dict) or state.get("active", True)
        )
        data.update({
            "version": self.plugin_version, "code_sha256": SOURCE_SHA256,
            "last_sync": self._state.get("last_sync") or "",
            "selected_rankings": len(self._selected_rankings),
            "active_servers": active_servers,
            "source_status": self._state.get("source_status") or {},
            "proxy": dict(self._proxy_status),
            "virtual_views": len(self._virtual_views),
            "daily_sync_enabled": self._daily_sync_enabled,
            "sync_cron": self._sync_cron,
        })
        return schemas.Response(
            success=data.get("state") != "failed", message=str(data.get("message") or ""), data=data
        )

    # ------------------------------------------------------------------
    # Emby 首页虚拟库反向代理
    # ------------------------------------------------------------------

    _HOP_HEADERS = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    }

    @staticmethod
    def _route_path(path: str) -> str:
        if path.casefold() == "/emby":
            return "/"
        if path.casefold().startswith("/emby/"):
            return path[5:]
        return path

    @staticmethod
    def _query_value(query: Mapping[str, List[str]], name: str, default: str = "") -> str:
        for key, values in query.items():
            if key.casefold() == name.casefold() and values:
                return str(values[-1])
        return default

    async def _emby_gateway_http(self, request: Request, emby_path: str = "") -> Response:
        del emby_path
        return await self._emby_gateway(request, str(request.url.path or "/"))

    async def _emby_gateway(self, request: Request, incoming_path: str) -> Response:
        """NextEmby 的普通 Emby API 上游；播放 302 仍由最外层 NextEmby 处理。"""
        with self._gateway_metrics_lock:
            self._proxy_status["requests"] = int(self._proxy_status.get("requests") or 0) + 1
            self._gateway_metrics["inflight"] += 1
            self._gateway_metrics["peak_inflight"] = max(
                self._gateway_metrics["peak_inflight"], self._gateway_metrics["inflight"]
            )
        try:
            return await self._emby_gateway_dispatch(request, incoming_path)
        finally:
            with self._gateway_metrics_lock:
                self._gateway_metrics["inflight"] = max(
                    0, self._gateway_metrics["inflight"] - 1
                )

    async def _emby_gateway_dispatch(self, request: Request, incoming_path: str) -> Response:
        route = self._route_path(incoming_path)
        query = urllib.parse.parse_qs(str(request.url.query or ""), keep_blank_values=True)

        if route.rstrip("/") == "/__mediaarchiver__/health":
            with self._gateway_metrics_lock:
                performance = dict(self._gateway_metrics)
                last_error = dict(self._gateway_last_error)
            performance["async_pool"] = bool(httpx is not None)
            return self._json_response(200, {
                "ok": True, "version": self.plugin_version, "code_sha256": SOURCE_SHA256,
                "pid": os.getpid(), "gateway": dict(self._proxy_status),
                "views": len(self._virtual_views), "items": len(self._proxy_item_index),
                "upstream": self._gateway_server_name or "MoviePilot Emby（首次请求时解析）",
                "performance": performance,
                "decoders": dict(self._decoder_status),
                "last_error": last_error,
            })

        if str(request.method).upper() not in {"GET", "HEAD"}:
            return await self._gateway_stream_response(request, route)

        views_match = re.fullmatch(r"/Users/[^/]+/Views/?", route, flags=re.I)
        # Emby Web 通常使用 /Users/{id}/Items；部分电视端、手机端和第三方
        # 客户端则使用 /Items?UserId=...。两种入口必须映射同一个虚拟 ParentId。
        items_match = re.fullmatch(r"/(?:Users/[^/]+/)?Items/?", route, flags=re.I)
        latest_match = re.fullmatch(
            r"/(?:Users/[^/]+/)?Items/Latest/?", route, flags=re.I
        )
        detail_match = re.fullmatch(
            r"/(?:Users/[^/]+/)?Items/([0-9a-f]{32})/?", route, flags=re.I
        )
        image_match = re.fullmatch(
            r"/Items/([0-9a-f]{32})/Images/Primary(?:/\d+)?/?", route, flags=re.I
        )
        with self._proxy_lock:
            view = dict(self._virtual_views.get(self._query_value(query, "ParentId")) or {})

        if detail_match:
            with self._proxy_lock:
                detail_view = dict(self._virtual_views.get(detail_match.group(1)) or {})
            if detail_view:
                return self._json_response(200, self._synthetic_view(detail_view))

        if image_match:
            with self._proxy_lock:
                image_view = dict(self._virtual_views.get(image_match.group(1)) or {})
            if image_view:
                return await asyncio.to_thread(self._virtual_cover_response, request, image_view)

        if (items_match or latest_match) and view:
            return await self._virtual_items_response(
                request, route, view, query, latest=bool(latest_match)
            )
        if views_match:
            return await self._gateway_buffered_response(
                request, route, transform=self._inject_views, force_identity=True
            )
        return await self._gateway_stream_response(request, route)

    def _synthetic_view(
        self, view: Mapping[str, Any], template: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        template = template or {}
        view_id = str(view.get("id") or self._view_id(str(view.get("key") or view.get("name"))))
        name = str(view.get("name") or "虚拟媒体库")
        count = len(view.get("item_ids") or [])
        cover_tag = str(
            view.get("cover_tag")
            or self._cover_tag(str(view.get("key") or name), view.get("item_ids") or [])
        )
        raw_collection_type = str(view.get("collection_type") or "").casefold()
        # Emby 的混合电影/剧集库不是名为 "mixed" 的 CollectionType；协议要求
        # 返回 null，让客户端采用通用浏览页面。返回未知字符串会令部分客户端
        # 无法进入“查看全部”。
        collection_type: Optional[str] = (
            None if raw_collection_type in {"", "mixed"} else raw_collection_type
        )
        return {
            "Name": name,
            "ServerId": str(template.get("ServerId") or view.get("server_id") or ""),
            "Id": view_id,
            "Guid": view_id,
            "Etag": cover_tag,
            "DisplayPreferencesId": view_id,
            "PresentationUniqueKey": view_id,
            "DateCreated": str(view.get("updated") or "2000-01-01T00:00:00.0000000Z"),
            "CanDelete": False,
            "CanDownload": False,
            "LockData": False,
            "LockedFields": [],
            "SortName": name,
            "ForcedSortName": name,
            "ExternalUrls": [],
            "Taglines": [],
            "RemoteTrailers": [],
            "ProviderIds": {},
            "IsFolder": True,
            "ParentId": str(template.get("ParentId") or "1"),
            "Type": "CollectionFolder",
            "CollectionType": collection_type,
            "ChildCount": count,
            "RecursiveItemCount": count,
            "ImageTags": {"Primary": cover_tag},
            "BackdropImageTags": [],
            "PrimaryImageAspectRatio": 1.7777777777777777,
            "SupportsSync": True,
            "UserData": {
                "PlaybackPositionTicks": 0, "IsFavorite": False, "Played": False,
            },
        }

    def _inject_views(
        self, status: int, headers: List[Tuple[str, str]], body: bytes,
    ) -> Tuple[int, List[Tuple[str, str]], bytes]:
        if status < 200 or status >= 300:
            return status, headers, body
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            return status, headers, body
        items = payload.get("Items")
        if not isinstance(items, list):
            items = []
            payload["Items"] = items
        existing = {str(item.get("Id")) for item in items if isinstance(item, Mapping)}
        template = next((item for item in items if isinstance(item, Mapping)), {})
        with self._proxy_lock:
            views = [dict(value) for value in self._virtual_views.values()]
        for view in views:
            if str(view.get("id")) not in existing:
                items.append(self._synthetic_view(view, template))
        payload["TotalRecordCount"] = len(items)
        return status, headers, json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    async def _virtual_items_response(
        self,
        request: Request,
        route: str,
        view: Mapping[str, Any],
        query: Mapping[str, List[str]],
        latest: bool,
    ) -> Response:
        selected, total = self._select_view_item_ids(view, query, latest)
        if not selected:
            return self._json_response(
                200,
                [] if latest else {"Items": [], "TotalRecordCount": total, "StartIndex": 0},
            )

        pairs = urllib.parse.parse_qsl(
            str(request.url.query or ""), keep_blank_values=True
        )
        discarded = {"parentid", "startindex", "limit", "ids", "sortby", "sortorder"}
        pairs = [(key, value) for key, value in pairs if key.casefold() not in discarded]
        pairs.extend([
            ("Ids", ",".join(selected)),
            ("Recursive", "true"),
            ("StartIndex", "0"),
            ("Limit", str(len(selected))),
        ])
        user_match = re.match(r"/Users/([^/]+)/", route, flags=re.I)
        upstream_route = (
            f"/Users/{user_match.group(1)}/Items" if user_match else "/Items"
        )
        target = upstream_route + "?" + urllib.parse.urlencode(pairs, doseq=True)

        def transform(
            status: int, headers: List[Tuple[str, str]], body: bytes,
        ) -> Tuple[int, List[Tuple[str, str]], bytes]:
            if status < 200 or status >= 300:
                return status, headers, body
            value = json.loads(body.decode("utf-8"))
            raw_items = value.get("Items") if isinstance(value, dict) else value
            if not isinstance(raw_items, list):
                raw_items = []
            by_id = {
                str(item.get("Id")): item for item in raw_items if isinstance(item, Mapping)
            }
            ordered = [by_id[item_id] for item_id in selected if item_id in by_id]
            result: Any = ordered if latest else {
                "Items": ordered,
                "TotalRecordCount": total,
                "StartIndex": self._int_query(query, "StartIndex", 0, 0, 1_000_000),
            }
            return status, headers, json.dumps(
                result, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")

        return await self._gateway_buffered_response(
            request, target, transform=transform, force_identity=True
        )

    def _select_view_item_ids(
        self,
        view: Mapping[str, Any],
        query: Mapping[str, List[str]],
        latest: bool,
    ) -> Tuple[List[str], int]:
        # 同步线程只会整体替换该字典，不会原地修改；保留当前引用即可，
        # 避免每个浏览请求都复制整个媒体索引。
        with self._proxy_lock:
            index = self._proxy_item_index
            if self._selection_index is not index:
                self._selection_cache.clear()
                self._selection_index = index
        cache_key = (str(view.get("id")), id(view.get("item_ids")), latest, tuple(
            (key, self._query_value(query, key)) for key in (
                "IncludeItemTypes", "ExcludeItemTypes", "SearchTerm", "Years", "Ids", "SortBy", "SortOrder"
            )
        ))
        start = 0 if latest else self._int_query(query, "StartIndex", 0, 0, 1_000_000)
        limit = self._int_query(query, "Limit", 16 if latest else 50, 1, 200)
        with self._proxy_lock:
            cached = self._selection_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 30:
                self._selection_cache.move_to_end(cache_key)
                return list(cached[1][start:start + limit]), len(cached[1])
        ids = list(dict.fromkeys(str(x) for x in (view.get("item_ids") or []) if str(x)))
        include_types = {
            value.strip().casefold()
            for value in self._query_value(query, "IncludeItemTypes").split(",") if value.strip()
        }
        search_term = self._query_value(query, "SearchTerm").strip().casefold()
        exclude_types = {x.strip().casefold() for x in self._query_value(query, "ExcludeItemTypes").split(",") if x.strip()}
        wanted_ids = {x.strip() for x in self._query_value(query, "Ids").split(",") if x.strip()}
        years = {
            self._number(value) for value in self._query_value(query, "Years").split(",") if value
        }

        def accepted(item_id: str) -> bool:
            item = index.get(item_id) or {}
            if wanted_ids and item_id not in wanted_ids:
                return False
            if str(item.get("Type") or "").casefold() in exclude_types:
                return False
            if include_types and str(item.get("Type") or "").casefold() not in include_types:
                return False
            if years and self._number(item.get("ProductionYear")) not in years:
                return False
            if search_term:
                text = " ".join(str(item.get(key) or "") for key in (
                    "Name", "OriginalTitle", "SortName", "ProductionYear",
                )).casefold()
                if search_term not in text:
                    return False
            return True

        ids = [item_id for item_id in ids if accepted(item_id)]
        sort_by = "DateCreated" if latest else self._query_value(query, "SortBy", "SortName").split(",")[0]
        descending = latest or self._query_value(query, "SortOrder").casefold() == "descending"

        def sort_key(item_id: str) -> Tuple[bool, Any, str]:
            item = index.get(item_id) or {}
            key = sort_by.casefold()
            if key in {"datecreated", "datelastcontentadded", "datelastsaved", "premieredate"}:
                value: Any = str(item.get(sort_by) or item.get("DateCreated") or item.get("PremiereDate") or "")
            elif key in {"productionyear", "communityrating", "criticrating"}:
                value = float(item.get(sort_by) or item.get("ProductionYear") or 0)
            elif key == "random":
                value = hashlib.sha1(item_id.encode("utf-8")).hexdigest()
            else:
                value = str(item.get(sort_by) or item.get("SortName") or item.get("Name") or "").casefold()
            return (value in ("", 0, 0.0), value, item_id)

        ids.sort(key=sort_key, reverse=descending)
        total = len(ids)
        with self._proxy_lock:
            if self._selection_index is index:
                self._selection_cache[cache_key] = (time.monotonic(), tuple(ids))
                while len(self._selection_cache) > 64:
                    self._selection_cache.popitem(last=False)
        return ids[start:start + limit], total

    def _int_query(
        self, query: Mapping[str, List[str]], name: str, default: int,
        minimum: int, maximum: int,
    ) -> int:
        return self._bounded_int(self._query_value(query, name, str(default)), default, minimum, maximum)

    @staticmethod
    def _json_response(status: int, value: Any) -> Response:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return Response(
            content=payload, status_code=status,
            headers={"Cache-Control": "no-store"},
            media_type="application/json; charset=utf-8",
        )

    @staticmethod
    def _cover_tag(key: str, item_ids: Iterable[str]) -> str:
        """成员未变化时保持稳定；成员变化后让 Emby 客户端自动刷新封面。"""
        members = ",".join(sorted({str(value) for value in item_ids if str(value)}))
        # Emby Web 的部分版本按 32 位 ImageTag 处理，使用完整 MD5 避免不发起图片请求。
        return hashlib.md5(f"cover-v2|{key}|{members}".encode("utf-8")).hexdigest()

    def _cover_theme(self, view: Mapping[str, Any]) -> Dict[str, str]:
        key = str(view.get("key") or "")
        theme_key = "default"
        if key.startswith("attribute:"):
            theme_key = key.split(":", 1)[1]
        elif key.startswith("ranking:"):
            rank_key = key.split(":", 1)[1]
            theme_key = str((RANK_META.get(rank_key) or {}).get("group") or "default")
        return dict(self.COVER_THEMES.get(theme_key) or self.COVER_THEMES["default"])

    @staticmethod
    def _hex_rgb(value: str) -> Tuple[int, int, int]:
        text = str(value or "#000000").lstrip("#")
        if len(text) == 3:
            text = "".join(char * 2 for char in text)
        try:
            return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
        except (TypeError, ValueError):
            return 0, 0, 0

    @staticmethod
    def _pillow_font(image_font: Any, size: int, bold: bool = True) -> Any:
        candidates = (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
        for path in candidates:
            try:
                return image_font.truetype(path, size=size)
            except Exception:
                continue
        return image_font.load_default()

    def _render_cover_pillow(self, view: Mapping[str, Any]) -> Optional[bytes]:
        """有 Pillow 和可用字体时生成包含中文专区名的高清 PNG。"""
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
        except Exception:
            return None
        width, height = 960, 540
        theme = self._cover_theme(view)
        start = self._hex_rgb(theme["bg"])
        end = self._hex_rgb(theme["bg2"])
        accent = self._hex_rgb(theme["accent"])
        foreground = self._hex_rgb(theme["fg"])
        image = Image.new("RGB", (width, height), start)
        draw = ImageDraw.Draw(image)
        for y in range(height):
            factor = y / max(1, height - 1)
            color = tuple(round(start[i] * (1 - factor) + end[i] * factor) for i in range(3))
            draw.line((0, y, width, y), fill=color)
        # 背景光晕、品牌色竖线与左下角状态胶囊均为矢量绘制，不依赖网络素材。
        draw.ellipse((660, -180, 1120, 280), fill=tuple(min(255, int(v * 0.34 + 16)) for v in accent))
        draw.rounded_rectangle((54, 62, 70, 478), radius=8, fill=accent)
        logo = str(theme.get("logo") or "VIRTUAL")
        name = str(view.get("name") or "虚拟媒体库")
        count = len(view.get("item_ids") or [])
        logo_size = 142 if len(logo) <= 4 else 96 if len(logo) <= 10 else 66
        logo_font = self._pillow_font(ImageFont, logo_size)
        while logo_size > 40:
            box = draw.textbbox((0, 0), logo, font=logo_font)
            if box[2] - box[0] <= 760:
                break
            logo_size -= 6
            logo_font = self._pillow_font(ImageFont, logo_size)
        draw.text((108, 122), logo, font=logo_font, fill=accent)
        name_font = self._pillow_font(ImageFont, 48)
        while True:
            box = draw.textbbox((0, 0), name, font=name_font)
            if box[2] - box[0] <= 790 or getattr(name_font, "size", 32) <= 28:
                break
            name_font = self._pillow_font(ImageFont, int(getattr(name_font, "size", 34)) - 3)
        draw.text((110, 302), name, font=name_font, fill=foreground)
        meta_font = self._pillow_font(ImageFont, 27, bold=False)
        meta = f"{count} ITEMS   •   LIVE VIRTUAL LIBRARY"
        meta_box = draw.textbbox((0, 0), meta, font=meta_font)
        chip_width = min(780, meta_box[2] - meta_box[0] + 54)
        draw.rounded_rectangle((108, 397, 108 + chip_width, 455), radius=29, outline=accent, width=2)
        draw.text((135, 410), meta, font=meta_font, fill=foreground)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    _BITMAP_FONT: Dict[str, Tuple[str, ...]] = {
        "A": ("01110","10001","10001","11111","10001","10001","10001"),
        "B": ("11110","10001","10001","11110","10001","10001","11110"),
        "C": ("01111","10000","10000","10000","10000","10000","01111"),
        "D": ("11110","10001","10001","10001","10001","10001","11110"),
        "E": ("11111","10000","10000","11110","10000","10000","11111"),
        "F": ("11111","10000","10000","11110","10000","10000","10000"),
        "G": ("01111","10000","10000","10111","10001","10001","01111"),
        "H": ("10001","10001","10001","11111","10001","10001","10001"),
        "I": ("11111","00100","00100","00100","00100","00100","11111"),
        "J": ("00111","00010","00010","00010","10010","10010","01100"),
        "K": ("10001","10010","10100","11000","10100","10010","10001"),
        "L": ("10000","10000","10000","10000","10000","10000","11111"),
        "M": ("10001","11011","10101","10101","10001","10001","10001"),
        "N": ("10001","11001","10101","10011","10001","10001","10001"),
        "O": ("01110","10001","10001","10001","10001","10001","01110"),
        "P": ("11110","10001","10001","11110","10000","10000","10000"),
        "Q": ("01110","10001","10001","10001","10101","10010","01101"),
        "R": ("11110","10001","10001","11110","10100","10010","10001"),
        "S": ("01111","10000","10000","01110","00001","00001","11110"),
        "T": ("11111","00100","00100","00100","00100","00100","00100"),
        "U": ("10001","10001","10001","10001","10001","10001","01110"),
        "V": ("10001","10001","10001","10001","10001","01010","00100"),
        "W": ("10001","10001","10001","10101","10101","10101","01010"),
        "X": ("10001","10001","01010","00100","01010","10001","10001"),
        "Y": ("10001","10001","01010","00100","00100","00100","00100"),
        "Z": ("11111","00001","00010","00100","01000","10000","11111"),
        "0": ("01110","10001","10011","10101","11001","10001","01110"),
        "1": ("00100","01100","00100","00100","00100","00100","01110"),
        "2": ("01110","10001","00001","00010","00100","01000","11111"),
        "3": ("11110","00001","00001","01110","00001","00001","11110"),
        "4": ("00010","00110","01010","10010","11111","00010","00010"),
        "5": ("11111","10000","10000","11110","00001","00001","11110"),
        "6": ("01110","10000","10000","11110","10001","10001","01110"),
        "7": ("11111","00001","00010","00100","01000","01000","01000"),
        "8": ("01110","10001","10001","01110","10001","10001","01110"),
        "9": ("01110","10001","10001","01111","00001","00001","01110"),
        "+": ("00000","00100","00100","11111","00100","00100","00000"),
        "-": ("00000","00000","00000","11111","00000","00000","00000"),
        " ": ("00000","00000","00000","00000","00000","00000","00000"),
    }

    @staticmethod
    def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + chunk_type + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
        )

    def _render_cover_basic_png(self, view: Mapping[str, Any]) -> bytes:
        """纯标准库 PNG，确保精简容器无 Pillow/中文字体时封面仍可显示。"""
        width, height = 960, 540
        theme = self._cover_theme(view)
        start, end = self._hex_rgb(theme["bg"]), self._hex_rgb(theme["bg2"])
        accent, foreground = self._hex_rgb(theme["accent"]), self._hex_rgb(theme["fg"])
        pixels = bytearray(width * height * 3)
        for y in range(height):
            factor = y / max(1, height - 1)
            color = bytes(round(start[i] * (1 - factor) + end[i] * factor) for i in range(3))
            offset = y * width * 3
            pixels[offset:offset + width * 3] = color * width

        def rectangle(x1: int, y1: int, x2: int, y2: int, color: Tuple[int, int, int]) -> None:
            left, right = max(0, x1), min(width, x2)
            row = bytes(color) * max(0, right - left)
            for y in range(max(0, y1), min(height, y2)):
                offset = (y * width + left) * 3
                pixels[offset:offset + len(row)] = row

        def draw_text(text: str, center_y: int, scale: int, color: Tuple[int, int, int]) -> None:
            text = "".join(char for char in text.upper() if char in self._BITMAP_FONT)
            if not text:
                return
            character_width = 6 * scale
            total_width = max(0, len(text) * character_width - scale)
            x_start = max(88, (width - total_width) // 2)
            y_start = center_y - (7 * scale) // 2
            for index, char in enumerate(text):
                glyph = self._BITMAP_FONT[char]
                for row_index, row_bits in enumerate(glyph):
                    for column_index, bit in enumerate(row_bits):
                        if bit == "1":
                            x = x_start + index * character_width + column_index * scale
                            y = y_start + row_index * scale
                            rectangle(x, y, x + scale, y + scale, color)

        rectangle(54, 62, 70, 478, accent)
        logo = str(theme.get("logo") or "VIRTUAL")
        scale = 24 if len(logo) <= 2 else 17 if len(logo) <= 5 else 10 if len(logo) <= 11 else 7
        draw_text(logo, 225, scale, accent)
        draw_text("VIRTUAL LIBRARY", 355, 8, foreground)
        draw_text(f"{len(view.get('item_ids') or [])} ITEMS - LIVE", 435, 5, foreground)
        raw = b"".join(
            b"\x00" + bytes(pixels[y * width * 3:(y + 1) * width * 3])
            for y in range(height)
        )
        signature = b"\x89PNG\r\n\x1a\n"
        return (
            signature
            + self._png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + self._png_chunk(b"IDAT", zlib.compress(raw, 6))
            + self._png_chunk(b"IEND", b"")
        )

    def _render_cover_png(self, view: Mapping[str, Any]) -> bytes:
        try:
            payload = self._render_cover_pillow(view)
            if payload:
                return payload
        except Exception as err:
            logger.warning("[媒体虚拟库] Pillow封面生成失败，已切换无依赖PNG：%s", err)
        return self._render_cover_basic_png(view)

    def _virtual_cover_response(
        self, request: Request, view: Mapping[str, Any],
    ) -> Response:
        tag = str(
            view.get("cover_tag")
            or self._cover_tag(str(view.get("key") or view.get("name")), view.get("item_ids") or [])
        )
        etag = f'"{tag}"'
        if str(request.headers.get("If-None-Match") or "").strip() == etag:
            return Response(status_code=304, headers={"ETag": etag})
        # ponytail: 冷封面串行生成，限制 CPU 并避免重复绘图；需要时再按 tag 分锁。
        with self._cover_lock:
            cached = self._cover_cache.get(tag)
            if cached:
                mime_type, payload = cached
            else:
                mime_type, payload = "image/png", self._render_cover_png(view)
                if len(self._cover_cache) >= 96:
                    self._cover_cache.pop(next(iter(self._cover_cache)), None)
                self._cover_cache[tag] = (mime_type, payload)
        return Response(
            content=b"" if str(request.method).upper() == "HEAD" else payload,
            status_code=200,
            headers={
                "Cache-Control": "public, max-age=86400", "ETag": etag,
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(len(payload)),
            },
            media_type=mime_type,
        )

    def _gateway_client(self) -> EmbyClient:
        if self._gateway_client_cache:
            return self._gateway_client_cache
        with self._gateway_client_lock:
            if self._gateway_client_cache:
                return self._gateway_client_cache
            client, server_name = self._create_clients()[0]
            self._gateway_client_cache = client
            self._gateway_server_name = server_name
            self._gateway_api_root = client.api_root
            return client

    def _gateway_failed(
        self, err: Exception, request: Any = None, incoming: str = "",
        stage: str = "upstream", status: int = 0,
        headers: Sequence[Tuple[str, str]] = (), size: int = 0,
    ) -> None:
        """只记录脱敏诊断，日志中不包含查询凭据、Cookie 或响应内容。"""
        now = time.monotonic()
        should_log = False
        suppressed = 0
        diagnostic = {
            "version": self.plugin_version, "code": SOURCE_SHA256[:12],
            "method": str(getattr(request, "method", "") or ""),
            "route": re.sub(r"/[^/]{32,}", "/{id}", incoming.split("?", 1)[0])[:160],
            "stage": stage, "status": status, "bytes": size,
            "encoding": ",".join(self._content_encodings(headers)) or "none",
            "error_type": type(err).__name__,
            "trace": [f"{frame.name}:{frame.lineno}" for frame in traceback.extract_tb(err.__traceback__)[-4:]],
        }
        with self._gateway_metrics_lock:
            self._gateway_metrics["failures"] += 1
            self._gateway_last_error = diagnostic
            if now - self._gateway_last_error_at >= 30:
                should_log = True
                suppressed = self._gateway_suppressed_errors
                self._gateway_suppressed_errors = 0
                self._gateway_last_error_at = now
            else:
                self._gateway_suppressed_errors += 1
                self._gateway_metrics["suppressed_errors"] += 1
        if should_log:
            suffix = f"（此前已合并 {suppressed} 条同类错误）" if suppressed else ""
            self._record("ERROR", "MoviePilot 内置 Emby 网关请求失败："
                         + json.dumps(diagnostic, ensure_ascii=False) + suffix)

    def _async_http_client(self) -> Any:
        """为 FastAPI 网关复用一个 httpx 连接池，避免每个资源重新 TCP 握手。"""
        if httpx is None:
            return None
        loop = asyncio.get_running_loop()
        client = self._httpx_client
        if client is not None and not bool(getattr(client, "is_closed", False)):
            if self._httpx_loop is loop:
                return client
            if self._httpx_loop and self._httpx_loop.is_running():
                raise RuntimeError("Emby 网关连接池不可跨活动事件循环复用")
            self._close_httpx_client()
        with self._httpx_lock:
            client = self._httpx_client
            if client is not None and not bool(getattr(client, "is_closed", False)):
                return client
            timeout = httpx.Timeout(
                float(self._timeout), connect=min(10.0, float(self._timeout)), pool=10.0
            )
            limits = httpx.Limits(
                max_connections=128, max_keepalive_connections=64, keepalive_expiry=30.0
            )
            self._httpx_client = httpx.AsyncClient(
                timeout=timeout, limits=limits, follow_redirects=False,
                trust_env=False, http2=False, cookies=GatewayCookieJar(),
            )
            self._httpx_loop = loop
            return self._httpx_client

    def _close_httpx_client(self) -> None:
        """插件重载/停止时异步归还连接池；关闭失败不影响 MoviePilot。"""
        with self._httpx_lock:
            client = getattr(self, "_httpx_client", None)
            loop = getattr(self, "_httpx_loop", None)
            self._httpx_client = None
            self._httpx_loop = None
        if client is None or bool(getattr(client, "is_closed", False)):
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        async def close_pool() -> None:
            try:
                await client.aclose()
            except Exception as err:
                logger.debug("[媒体虚拟库] 关闭异步连接池失败：%s", type(err).__name__)
        try:
            if running is not None and (running is loop or not loop or not loop.is_running()):
                running.create_task(close_pool())
            elif loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(close_pool(), loop)
            else:
                asyncio.run(close_pool())
        except Exception as err:
            logger.debug("[媒体虚拟库] 关闭异步连接池失败：%s", err)

    def _httpx_request(
        self, client: EmbyClient, method: str, incoming: str,
        headers: Mapping[str, str], body: Any,
    ) -> Tuple[Any, Any]:
        """构造 httpx 请求；返回共享客户端与尚未发送的请求对象。"""
        async_client = self._async_http_client()
        if async_client is None:
            return None, None
        target, request_target = self._upstream_target(client, incoming)
        url = f"{target.scheme}://{target.netloc}{request_target}"
        request = async_client.build_request(
            method, url,
            headers=[(key.encode("ascii"), value.encode("latin-1")) for key, value in headers.items()],
            content=body or None,
        )
        return async_client, request

    def _incoming_target(self, request: Request, incoming: str) -> str:
        """保留播放器原始百分号编码与查询串，避免字幕/路径被二次解码。"""
        if "?" in incoming:
            return incoming
        path = incoming
        raw_path = getattr(request, "scope", {}).get("raw_path")
        if raw_path and self._route_path(str(request.url.path)) == incoming:
            path = self._route_path(urllib.parse.quote_from_bytes(raw_path, safe="/%:@!$&'()*+,;=-._~"))
        else:
            path = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=-._~")
        return path + (("?" + str(request.url.query)) if request.url.query else "")

    def _upstream_target(
        self, client: EmbyClient, incoming: str,
    ) -> Tuple[urllib.parse.SplitResult, str]:
        target = urllib.parse.urlsplit(client.api_root)
        parsed = urllib.parse.urlsplit(incoming)
        route = self._route_path(parsed.path or "/")
        base_path = target.path.rstrip("/")
        if route == "/":
            path = base_path or "/"
        else:
            path = base_path + (route if route.startswith("/") else "/" + route)
        if parsed.query:
            path += "?" + parsed.query
        return target, path

    def _upstream_connection(
        self, target: urllib.parse.SplitResult,
    ) -> http.client.HTTPConnection:
        port = target.port or (443 if target.scheme == "https" else 80)
        if target.scheme == "https":
            return http.client.HTTPSConnection(
                target.hostname, port, timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(target.hostname, port, timeout=self._timeout)

    def _forward_request_headers(
        self, request: Request, target: urllib.parse.SplitResult,
        body: bytes, force_identity: bool,
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        connection_tokens = set()
        for key, value in request.headers.items():
            if key.casefold() == "connection":
                connection_tokens.update(x.strip().casefold() for x in value.split(","))
        for key, value in request.headers.items():
            folded = key.casefold()
            if folded in self._HOP_HEADERS | connection_tokens | {"host", "content-length"}:
                continue
            # Starlette 通常把入站名称规范成小写。若先保留 ``accept-encoding``
            # 再添加 ``Accept-Encoding: identity``，普通 dict 会同时保留两项，
            # http.client 随后发出两个同名头；部分 Emby 会采用前一个并返回
            # Brotli/gzip。需要转换 JSON 时必须先剔除入站值再写入唯一的 identity。
            if force_identity and folded in {
                "accept-encoding", "accept", "if-none-match", "if-modified-since",
                "if-match", "if-unmodified-since", "range", "if-range",
            }:
                continue
            headers[folded] = str(value)
        # httpx 默认 Accept-Encoding 含 br/zstd。客户端未声明时不主动增加，
        # 避免把它无法解码的压缩响应透明传回去。
        headers.setdefault("accept-encoding", "identity")
        headers["Host"] = target.netloc
        client_host = str(getattr(getattr(request, "client", None), "host", "") or "")
        forwarded = headers.pop("x-forwarded-for", "").strip()
        if client_host:
            headers["X-Forwarded-For"] = f"{forwarded}, {client_host}" if forwarded else client_host
        elif forwarded:
            headers["X-Forwarded-For"] = forwarded
        headers.setdefault("x-forwarded-proto", str(getattr(request.url, "scheme", "http") or "http"))
        if force_identity:
            headers["accept-encoding"] = "identity"
            headers["accept"] = "application/json"
        if body:
            headers["Content-Length"] = str(len(body))
        return headers

    def _open_upstream(
        self, client: EmbyClient, method: str, incoming: str,
        headers: Dict[str, str], body: bytes,
    ) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        target, request_target = self._upstream_target(client, incoming)
        connection = self._upstream_connection(target)
        try:
            connection.request(method, request_target, body=body or None, headers=headers)
            return connection, connection.getresponse()
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _httpx_header_pairs(headers: Any) -> List[Tuple[str, str]]:
        # HTTPX 可能把非ASCII响应头自动解为UTF-8；代理必须保存原始字节。
        if hasattr(headers, "raw"):
            return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in headers.raw]
        return list(headers.multi_items())

    @classmethod
    def _response_header_pairs(
        cls, headers: Iterable[Tuple[str, str]], transformed: bool = False,
    ) -> List[Tuple[str, str]]:
        headers = list(headers)
        excluded = set(cls._HOP_HEADERS)
        for key, value in headers:
            if str(key).casefold() == "connection":
                excluded.update(x.strip().casefold() for x in str(value).split(","))
        if transformed:
            # 响应体一旦解压或改写，原长度、压缩标记及校验标签均已失效。
            excluded.update({
                "content-length", "content-encoding", "content-md5", "digest", "etag",
                "last-modified", "cache-control", "expires", "content-type",
            })
        return [(str(key), str(value)) for key, value in headers
                if str(key).casefold() not in excluded]

    @classmethod
    def _apply_response_headers(
        cls, response: Response, headers: Sequence[Tuple[str, str]], transformed: bool = False,
    ) -> Response:
        pairs = cls._response_header_pairs(headers, transformed)
        if hasattr(response, "raw_headers"):
            overridden = {key.casefold().encode("latin-1") for key, _ in pairs}
            response.raw_headers = [(k, v) for k, v in response.raw_headers if k not in overridden]
            response.raw_headers.extend((k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in pairs)
        else:  # 离线测试响应对象
            response.headers.update(dict(pairs))
        return response

    @staticmethod
    def _content_encodings(headers: Iterable[Tuple[str, str]]) -> List[str]:
        encodings: List[str] = []
        for key, value in headers:
            if str(key).casefold() != "content-encoding":
                continue
            encodings.extend(
                token.strip().casefold()
                for token in str(value).split(",")
                if token.strip() and token.strip().casefold() != "identity"
            )
        return encodings

    MAX_JSON_BYTES = 16 * 1024 * 1024

    @classmethod
    def _decode_codec(cls, encoding: str, body: bytes) -> bytes:
        limit = cls.MAX_JSON_BYTES
        if encoding in {"gzip", "x-gzip"}:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                decoded = stream.read(limit + 1)
        elif encoding == "deflate":
            try:
                decoder = zlib.decompressobj()
                decoded = decoder.decompress(body, limit + 1)
            except zlib.error:
                decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                decoded = decoder.decompress(body, limit + 1)
            if not decoder.eof and len(decoded) <= limit:
                raise ValueError("deflate 数据不完整")
        elif encoding == "br":
            try:
                import brotli
            except ImportError:
                try:
                    import brotlicffi as brotli
                except ImportError as err:
                    raise ValueError("缺少 Brotli 解码器") from err
            decoder = brotli.Decompressor()
            output = bytearray()
            for start in range(0, len(body), 1024):
                output.extend(decoder.process(body[start:start + 1024]))
                if len(output) > limit:
                    raise ValueError("JSON 解压后超过大小限制")
            if not decoder.is_finished():
                raise ValueError("Brotli 数据不完整")
            decoded = bytes(output)
        elif encoding in {"zstd", "x-zstd"}:
            try:
                import zstandard
                # stream_reader 支持没有声明原始长度的 Zstd 帧。
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as stream:
                    decoded = stream.read(limit + 1)
            except ImportError:
                try:
                    from compression import zstd
                except ImportError as err:
                    raise ValueError("缺少 Zstandard 解码器") from err
                decoded = zstd.decompress(body)
        else:
            raise ValueError("未知压缩格式")
        if len(decoded) > limit:
            raise ValueError("JSON 解压后超过大小限制")
        return decoded

    @staticmethod
    def _valid_json_bytes(body: bytes) -> Optional[bytes]:
        try:
            # json.loads(bytes) 同时处理 UTF-8 BOM、UTF-16/32 等 JSON 编码。
            value = json.loads(body)
            if not isinstance(value, (dict, list)):
                return None
            if not body.startswith(b"\xef\xbb\xbf") and b"\x00" not in body[:4] and not body.startswith((b"\xff\xfe", b"\xfe\xff")):
                return body
            return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, UnicodeError, RecursionError):
            return None

    @classmethod
    def _decode_buffered_body(
        cls, headers: Iterable[Tuple[str, str]], body: bytes,
    ) -> bytes:
        """仅用于 JSON：按压缩头解码；头缺失/错误时以完整 JSON 验证兜底。

        绝不使用 errors='ignore/replace'。图片、视频与错误页不调用此函数。
        """
        if len(body) > cls.MAX_JSON_BYTES:
            raise ValueError("JSON 响应超过大小限制")
        encodings = cls._content_encodings(headers)
        plain = cls._valid_json_bytes(body)
        if plain is not None:
            # 某些上游已解压，但遗留 Content-Encoding。
            return plain
        if len(encodings) <= 4:
            try:
                decoded = body
                for encoding in reversed(encodings):
                    decoded = cls._decode_codec(encoding, decoded)
                result = cls._valid_json_bytes(decoded)
                if result is not None:
                    return result
            except (ValueError, OSError, EOFError, ImportError, zlib.error):
                pass
            except Exception:
                # 可选 br/zstd 模块定义自己的解压异常；继续已限定的候选检查。
                pass
        for encoding in ("gzip", "deflate", "br", "zstd"):
            try:
                result = cls._valid_json_bytes(cls._decode_codec(encoding, body))
                if result is not None:
                    return result
            except Exception:
                continue
        raise ValueError("Emby 响应无法还原为完整 JSON（压缩头缺失、格式错误或缺少解码器）")

    async def _fetch_gateway_bytes(
        self, client: EmbyClient, method: str, incoming: str,
        headers: Mapping[str, str], body: bytes,
    ) -> Tuple[int, List[Tuple[str, str]], bytes]:
        if httpx is not None:
            async_client, outgoing = self._httpx_request(client, method, incoming, headers, body)
            upstream = await async_client.send(outgoing, stream=True)
            try:
                output = bytearray()
                async for chunk in upstream.aiter_raw():
                    output.extend(chunk)
                    if len(output) > self.MAX_JSON_BYTES:
                        raise ValueError("JSON 响应超过大小限制")
                return int(upstream.status_code), self._httpx_header_pairs(upstream.headers), bytes(output)
            finally:
                import anyio
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        def fetch() -> Tuple[int, List[Tuple[str, str]], bytes]:
            connection, upstream = self._open_upstream(client, method, incoming, dict(headers), body)
            try:
                raw = upstream.read(self.MAX_JSON_BYTES + 1)
                if len(raw) > self.MAX_JSON_BYTES:
                    raise ValueError("JSON 响应超过大小限制")
                return int(upstream.status), list(upstream.getheaders()), raw
            finally:
                upstream.close()
                connection.close()
        return await asyncio.to_thread(fetch)

    async def _gateway_buffered_response(
        self, request: Request, incoming: str,
        transform: Optional[Callable[[int, List[Tuple[str, str]], bytes], Tuple[int, List[Tuple[str, str]], bytes]]] = None,
        force_identity: bool = False,
    ) -> Response:
        stage, status, response_headers, raw = "connect", 0, [], b""
        try:
            client = self._gateway_client_cache or await asyncio.to_thread(self._gateway_client)
            body = await request.body()
            full_incoming = self._incoming_target(request, incoming)
            target, _ = self._upstream_target(client, full_incoming)
            headers = self._forward_request_headers(request, target, body, force_identity)
            method = str(request.method).upper()
            # HEAD 也需要按 GET 计算虚拟内容长度，但绝不向客户端发送响应体。
            upstream_method = "GET" if transform and method == "HEAD" else method
            transformed = False
            for attempt in range(2):
                stage = "read_response"
                status, response_headers, raw = await self._fetch_gateway_bytes(
                    client, upstream_method, full_incoming, headers, body
                )
                # 登录失效、302、304、204、错误页原样返回，不当作 JSON 解码。
                if transform is None or status != 200:
                    break
                stage = "decode_json"
                try:
                    decoded = await asyncio.to_thread(self._decode_buffered_body, response_headers, raw)
                    if decoded != raw:
                        with self._gateway_metrics_lock:
                            self._gateway_metrics["decode_recoveries"] += 1
                    stage = "transform_json"
                    status, response_headers, raw = await asyncio.to_thread(
                        transform, status, response_headers, decoded
                    )
                    transformed = True
                    break
                except (ValueError, UnicodeError):
                    if attempt or upstream_method != "GET":
                        raise
                    with self._gateway_metrics_lock:
                        self._gateway_metrics["decode_retries"] += 1
                    headers["accept-encoding"] = "identity"
                    headers["accept"] = "application/json"
                    headers["cache-control"] = "no-cache"
            response = Response(
                content=b"" if method == "HEAD" else raw, status_code=status,
                headers={"Cache-Control": "private, no-store", "Content-Length": str(len(raw))}
                    if transformed else None,
                media_type="application/json" if transformed else None,
            )
            return self._apply_response_headers(response, response_headers, transformed)
        except Exception as err:
            self._gateway_failed(err, request, incoming, stage, status, response_headers, len(raw))
            return self._json_response(502, {
                "error": "Emby gateway failed", "stage": stage,
                "version": self.plugin_version, "code": SOURCE_SHA256[:12],
            })

    async def _gateway_stream_response(self, request: Request, incoming: str) -> Response:
        upstream = None
        connection = None
        cleanup = None
        handed_off = False
        try:
            client = self._gateway_client_cache or await asyncio.to_thread(self._gateway_client)
            full_incoming = self._incoming_target(request, incoming)
            target, _ = self._upstream_target(client, full_incoming)
            method = str(request.method).upper()
            if httpx is not None:
                # 客户端大请求（例如图片上传）也流式发送，不常驻内存。
                body: Any = request.stream() if hasattr(request, "stream") and method not in {"GET", "HEAD", "OPTIONS"} else await request.body()
                headers = self._forward_request_headers(request, target, b"", False)
                if request.headers.get("content-length") is not None:
                    headers["Content-Length"] = request.headers["content-length"]
                async_client, outgoing = self._httpx_request(client, method, full_incoming, headers, body)
                upstream = await async_client.send(outgoing, stream=True)
                status, response_headers = int(upstream.status_code), self._httpx_header_pairs(upstream.headers)
            else:
                body = await request.body()
                headers = self._forward_request_headers(request, target, body, False)
                connection, upstream = await asyncio.to_thread(
                    self._open_upstream, client, method, full_incoming, headers, body
                )
                status, response_headers = int(upstream.status), list(upstream.getheaders())
            closed = False
            counted = False

            async def cleanup() -> None:
                nonlocal closed
                if closed:
                    return
                closed = True
                try:
                    if connection is None:
                        import anyio
                        with anyio.CancelScope(shield=True):
                            await upstream.aclose()
                    else:
                        await asyncio.to_thread(upstream.close)
                        connection.close()
                finally:
                    if counted:
                        with self._gateway_metrics_lock:
                            self._gateway_metrics["active_streams"] -= 1

            if method == "HEAD" or status in {204, 304}:
                await cleanup()
                handed_off = True
                return self._apply_response_headers(Response(status_code=status), response_headers)
            with self._gateway_metrics_lock:
                self._gateway_metrics["active_streams"] += 1
            counted = True

            async def stream() -> Any:
                try:
                    if connection is None:
                        # 不指定聚合块大小；首块到达即发送，避免等满 1 MiB。
                        async for chunk in upstream.aiter_raw():
                            if chunk:
                                yield chunk
                    else:
                        while True:
                            chunk = await asyncio.to_thread(upstream.read1, 64 * 1024)
                            if not chunk:
                                break
                            yield chunk
                except Exception as err:
                    self._gateway_failed(err, request, incoming, "stream_body", status, response_headers)
                    raise
                finally:
                    await cleanup()

            response = GatewayStreamingResponse(stream(), status_code=status, cleanup=cleanup)
            response = self._apply_response_headers(response, response_headers)
            handed_off = True
            return response
        except Exception as err:
            self._gateway_failed(err, request, incoming, "open_stream")
            return self._json_response(502, {
                "error": "Emby gateway failed", "version": self.plugin_version,
                "code": SOURCE_SHA256[:12], "stage": "open_stream",
            })
        finally:
            if upstream is not None and not handed_off:
                if cleanup is not None:
                    await cleanup()
                elif connection is None:
                    import anyio
                    with anyio.CancelScope(shield=True):
                        await upstream.aclose()
                else:
                    upstream.close()
                    connection.close()

    async def _emby_gateway_websocket(self, websocket: WebSocket) -> None:
        """转发 Emby /socket；NextEmby 自行处理时本入口不会被调用。"""
        try:
            import inspect
            import websockets  # type: ignore

            client = await asyncio.to_thread(self._gateway_client)
            incoming = str(websocket.url.path or "/socket")
            if websocket.url.query:
                incoming += "?" + str(websocket.url.query)
            target, request_target = self._upstream_target(client, incoming)
            scheme = "wss" if target.scheme == "https" else "ws"
            upstream_url = urllib.parse.urlunsplit(
                (scheme, target.netloc, request_target.split("?", 1)[0],
                 request_target.split("?", 1)[1] if "?" in request_target else "", "")
            )
            excluded = {
                "host", "connection", "upgrade", "sec-websocket-key",
                "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol",
            }
            extra_headers = [
                (str(key), str(value)) for key, value in websocket.headers.items()
                if str(key).casefold() not in excluded
            ]
            kwargs: Dict[str, Any] = {"ping_interval": None}
            if "proxy" in inspect.signature(websockets.connect).parameters:
                kwargs["proxy"] = None
            parameter = (
                "additional_headers"
                if "additional_headers" in inspect.signature(websockets.connect).parameters
                else "extra_headers"
            )
            kwargs[parameter] = extra_headers
            subprotocols = list(websocket.scope.get("subprotocols") or [])
            if subprotocols:
                kwargs["subprotocols"] = subprotocols
            async with websockets.connect(upstream_url, **kwargs) as upstream:
                await websocket.accept(subprotocol=getattr(upstream, "subprotocol", None))

                async def client_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            return
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(str(message))

                tasks = {
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                }
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        try:
                            task.result()
                        except (WebSocketDisconnect, asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                            pass
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await websocket.close(code=1000)
                except (WebSocketDisconnect, RuntimeError):
                    pass
        except WebSocketDisconnect:
            return
        except Exception as err:
            self._record("WARNING", f"Emby WebSocket 转发结束：{err}")
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    def _server_options(self) -> List[Dict[str, str]]:
        options: List[Dict[str, str]] = []
        seen: Set[str] = set()
        try:
            configs = MediaServerHelper().get_configs() or {}
            values = configs.values() if isinstance(configs, dict) else configs
            for config in values:
                name = self._object_value(config, "name", "Name")
                kind = self._object_value(config, "type", "Type", "kind")
                if name and (not kind or "emby" in str(kind).casefold()) and str(name) not in seen:
                    seen.add(str(name))
                    options.append({"title": str(name), "value": str(name)})
        except Exception as err:
            logger.warning("[媒体虚拟库] 获取媒体服务器列表失败：%s", err)
        return options

    def _aggregate_counts(self) -> Tuple[Dict[str, int], Dict[str, int]]:
        attributes = {key: 0 for key in self.ATTRIBUTE_RULES}
        rankings = {key: 0 for key in RANK_META}
        for state in (self._state.get("servers") or {}).values():
            if isinstance(state, dict) and not state.get("active", True):
                continue
            for key, value in (state.get("attribute_counts") or {}).items():
                attributes[key] = attributes.get(key, 0) + int(value or 0)
            for key, value in (state.get("ranking_counts") or {}).items():
                rankings[key] = rankings.get(key, 0) + int(value or 0)
        return attributes, rankings

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """傻瓜式配置：选择 MoviePilot Emby、专区和自动维护即可。"""
        attribute_counts, ranking_counts = self._aggregate_counts()
        active_server_count = sum(
            1 for state in (self._state.get("servers") or {}).values()
            if not isinstance(state, dict) or state.get("active", True)
        )
        connection_test = self._state.get("connection_test") or {}
        if connection_test:
            connection_type = "success" if connection_test.get("ok") else "error"
            connection_text = str(connection_test.get("message") or "连接状态未知")
            if connection_test.get("time"):
                connection_text += f"｜{connection_test['time']}"
        else:
            connection_type = "info"
            connection_text = "尚未测试；插件会直接读取 MoviePilot 已保存的 Emby 配置。"
        proxy_type = "success" if self._proxy_status.get("running") else "warning"
        proxy_text = (
            f"{self._proxy_status.get('message') or 'MoviePilot 内置网关未注册'}；"
            f"NextEmby 对外地址仍为 NAS-IP:{self.PUBLIC_GATEWAY_PORT}"
        )

        attribute_cards: List[dict] = []
        for key, rule in self.ATTRIBUTE_RULES.items():
            attribute_cards.append({"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 4}, "content": [
                {"component": "VCard", "props": {"variant": "outlined", "class": "h-100"}, "content": [
                    {"component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "d-flex align-center mb-2"}, "content": [
                            {"component": "VIcon", "props": {"icon": rule["icon"], "class": "mr-2 text-primary"}},
                            {"component": "div", "props": {"class": "font-weight-bold"}, "text": rule["name"]},
                            {"component": "VSpacer"},
                            {"component": "VChip", "props": {"size": "small", "variant": "tonal"},
                             "text": f"{attribute_counts.get(key, 0)} 项"},
                        ]},
                        {"component": "VCheckbox", "props": {
                            "model": f"zone_{key}", "label": "启用此专区", "hide-details": True,
                        }},
                        {"component": "div", "props": {"class": "text-caption mt-1"}, "text": rule["hint"]},
                    ]},
                ]},
            ]})

        ranking_cards: List[dict] = []
        for group in RANK_GROUPS:
            checks: List[dict] = []
            for key, label in group["items"]:
                checks.append({"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 4}, "content": [
                    {"component": "VCheckbox", "props": {
                        "model": f"rank_{key}", "label": label, "hide-details": True,
                    }},
                ]})
            selected = sum(1 for key, _ in group["items"] if key in self._selected_rankings)
            hits = sum(ranking_counts.get(key, 0) for key, _ in group["items"])
            ranking_cards.append({"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VCard", "props": {"variant": "outlined", "class": "mb-2"}, "content": [
                    {"component": "VCardTitle", "props": {"class": "d-flex align-center flex-wrap"}, "content": [
                        {"component": "VIcon", "props": {"icon": group["icon"], "class": "mr-3"}},
                        {"component": "div", "content": [
                            {"component": "div", "text": group["name"]},
                            {"component": "div", "props": {"class": "text-caption"}, "text": group["subtitle"]},
                        ]},
                        {"component": "VSpacer"},
                        {"component": "VChip", "props": {"size": "small", "variant": "tonal"},
                         "text": f"已选 {selected}｜命中 {hits}"},
                    ]},
                    {"component": "VCardText", "content": [{"component": "VRow", "content": checks}]},
                ]},
            ]})

        token = self._secret(getattr(settings, "API_TOKEN", ""))
        form = {"component": "VForm", "content": [
            {"component": "VCard", "props": {"variant": "tonal", "color": "primary", "class": "mb-4"}, "content": [
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-overline text-primary"},
                     "text": "EMBY VIRTUAL LIBRARY"},
                    {"component": "div", "props": {"class": "text-h4 font-weight-bold mb-2"},
                     "text": "Emby 媒体虚拟库"},
                    {"component": "div", "props": {"class": "text-body-1 mb-4"},
                     "text": "选择 MoviePilot 已配置的 Emby，勾选专区并重建；客户端继续使用 NextEmby 8098。"},
                    {"component": "VAlert", "props": {
                        "type": "success", "variant": "tonal", "class": "mb-4",
                        "title": "零新增端口",
                        "text": f"插件复用 MoviePilot API 端口 {self._moviepilot_api_port()}；NextEmby 继续独占并对外提供 8098，插件不会监听 8097、8098 或 8099。",
                    }},
                    {"component": "VSelect", "props": {
                        "model": "emby_server", "label": "MoviePilot 已配置的 Emby",
                        "items": self._server_options(), "clearable": True,
                        "prepend-inner-icon": "mdi-server-network",
                        "hint": "留空会自动使用第一台 Emby；当前你的 MoviePilot 配置仍应指向原生 8096。",
                        "persistent-hint": True,
                    }},
                    {"component": "VAlert", "props": {
                        "type": connection_type, "variant": "tonal", "class": "my-3",
                        "title": "Emby 连接状态", "text": connection_text,
                    }},
                    {"component": "VAlert", "props": {
                        "type": proxy_type, "variant": "tonal", "class": "my-3",
                        "title": "8098 虚拟库链路", "text": proxy_text,
                    }},
                    {"component": "div", "props": {"class": "d-flex flex-wrap ga-3"}, "content": [
                        {"component": "VBtn", "props": {
                            "color": "primary", "variant": "outlined", "prepend-icon": "mdi-lan-connect",
                        }, "text": "测试连接", "events": {"click": {
                            "api": "plugin/MediaArchiver/test_connection", "method": "post",
                            "params": {"token": token},
                        }}},
                        {"component": "VBtn", "props": {
                            "color": "primary", "variant": "flat", "prepend-icon": "mdi-refresh",
                        }, "text": "一键重建", "events": {"click": {
                            "api": "plugin/MediaArchiver/rebuild", "method": "post",
                            "params": {"token": token},
                        }}},
                    ]},
                ]},
            ]},
            {"component": "VRow", "props": {"class": "mb-3"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                        {"component": "VCardText", "content": [
                            {"component": "VSwitch", "props": {
                                "model": "ranking_enabled", "label": "启用榜单虚拟库",
                                "color": "primary", "hide-details": True,
                            }},
                            {"component": "div", "props": {"class": "text-caption mt-2"},
                             "text": "把勾选的平台榜单显示成首页独立栏目。"},
                        ]},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                        {"component": "VCardText", "content": [
                            {"component": "VSwitch", "props": {
                                "model": "virtual_enabled", "label": "启用媒体属性专区",
                                "color": "primary", "hide-details": True,
                            }},
                            {"component": "div", "props": {"class": "text-caption mt-2"},
                             "text": "Remux、4K、Dolby Vision、HDR、Atmos。"},
                        ]},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                        {"component": "VCardText", "content": [
                            {"component": "VSwitch", "props": {
                                "model": "auto_sync", "label": "实时增量维护",
                                "color": "primary", "hide-details": True,
                            }},
                            {"component": "div", "props": {"class": "text-caption mt-2"},
                             "text": "自动跟踪新增、更新和删除，并轻量校准。"},
                        ]},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                        {"component": "VCardText", "content": [
                            {"component": "VSwitch", "props": {
                                "model": "daily_sync_enabled", "label": "定时更新全部专区",
                                "color": "primary", "hide-details": True,
                            }},
                            {"component": "VTextField", "props": {
                                "model": "sync_cron", "label": "更新时间（Cron）",
                                "placeholder": "0 4 * * *", "density": "compact", "class": "mt-2",
                                "prepend-inner-icon": "mdi-clock-outline",
                                "hint": "标准5段Cron；每天4点：0 4 * * *",
                                "persistent-hint": True,
                            }},
                        ]},
                    ]},
                ]},
            ]},
            {"component": "VRow", "props": {"class": "mb-4"}, "content": [
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                        {"component": "VCardText", "text": f"{len(self._selected_rankings)}\n已选榜单"},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                        {"component": "VCardText", "text": f"{sum(ranking_counts.values())}\n榜单命中"},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                        {"component": "VCardText", "text": f"{sum(attribute_counts.values())}\n属性命中"},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                        {"component": "VCardText", "text": f"{active_server_count}\n生效服务器"},
                    ]},
                ]},
            ]},
            {"component": "div", "props": {"class": "text-h5 font-weight-bold mb-1"}, "text": "选择榜单"},
            {"component": "div", "props": {"class": "text-body-2 mb-3"},
             "text": "先勾选上方“启用榜单虚拟库”，再选择需要的榜单；每一项都会成为首页一级虚拟库。"},
            {"component": "VRow", "content": ranking_cards},
            {"component": "div", "props": {"class": "text-h5 font-weight-bold mt-5 mb-1"},
             "text": "媒体属性专区"},
            {"component": "div", "props": {"class": "text-body-2 mb-3"},
             "text": "先勾选上方“启用媒体属性专区”，再选择需要自动维护的专区。"},
            {"component": "VRow", "content": attribute_cards},
            {"component": "VExpansionPanels", "props": {
                "multiple": True, "variant": "accordion", "class": "mt-5",
            }, "content": [
                {"component": "VExpansionPanel", "content": [
                    {"component": "VExpansionPanelTitle", "content": [
                        {"component": "VIcon", "props": {"icon": "mdi-cog-outline", "class": "mr-2"}},
                        {"component": "span", "text": "高级设置（通常不用改）"},
                    ]},
                    {"component": "VExpansionPanelText", "content": [
                        {"component": "VAlert", "props": {
                            "type": "warning", "variant": "tonal", "class": "mb-3",
                            "text": "增量间隔用于轻量维护；Cron 触发时会全量刷新所有专区和榜单。时间按 MoviePilot 容器时区执行。",
                        }},
                        {"component": "VRow", "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "sync_interval", "label": "校准间隔（分钟）", "type": "number",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "timeout", "label": "连接超时（秒）", "type": "number",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "ranking_limit", "label": "每榜最多条目", "type": "number",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "ranking_language", "label": "榜单语言",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "ranking_regions", "label": "平台榜地区（英文逗号分隔）",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "tmdb_domain", "label": "TMDB API 域名覆盖（通常留空）",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "tmdb_api_key", "label": "TMDB API Key 覆盖（通常留空）",
                                    "type": "password",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "ranking_feed_url", "label": "自定义榜单 JSON Feed（可选）",
                                }},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "ranking_feed_token", "label": "Feed Bearer Token（可选）",
                                    "type": "password",
                                }},
                            ]},
                        ]},
                    ]},
                ]},
            ]},
            {"component": "VAlert", "props": {
                "type": "success", "variant": "tonal", "class": "mt-5",
                "title": "最简单的使用方法",
                "text": f"MoviePilot继续连接原生Emby:8096 → NextEmby的原始服务器端口改为{self._moviepilot_api_port()} → 插件一键重建 → 客户端仍访问NAS-IP:8098。",
            }},
        ]}
        defaults: Dict[str, Any] = {
            "enabled": True, "virtual_enabled": True, "ranking_enabled": False,
            "auto_sync": True, "daily_sync_enabled": True,
            "sync_cron": "0 4 * * *",
            "emby_server": "", "emby_servers": [],
            "sync_interval": 60, "timeout": 30, "tmdb_api_key": "", "tmdb_domain": "",
            "ranking_regions": "US,GB,JP,KR,HK,TW", "ranking_limit": 100,
            "ranking_language": "zh-CN", "ranking_feed_url": "", "ranking_feed_token": "",
        }
        defaults.update({f"zone_{key}": True for key in self.ATTRIBUTE_RULES})
        defaults.update({f"rank_{key}": key in DEFAULT_RANKINGS for key in RANK_META})
        return [form], defaults

    def get_page(self) -> List[dict]:
        """简洁结果页；技术日志默认折叠，仅在排障时展开。"""
        attribute_counts, ranking_counts = self._aggregate_counts()
        active_server_count = sum(
            1 for state in (self._state.get("servers") or {}).values()
            if not isinstance(state, dict) or state.get("active", True)
        )
        runtime_state = str(self._runtime.get("state") or "idle")
        state_meta = {
            "idle": ("尚未运行", "info"),
            "running": ("正在同步", "info"),
            "completed": ("同步成功", "success"),
            "completed_with_warnings": ("已完成，有少量警告", "warning"),
            "failed": ("同步失败", "error"),
        }
        state_label, state_type = state_meta.get(runtime_state, (runtime_state, "info"))
        stats = self._runtime.get("stats") or {}
        result_rows: List[dict] = []
        if self._attribute_enabled:
            for key, rule in self.ATTRIBUTE_RULES.items():
                if key not in self._enabled_rules:
                    continue
                result_rows.append({"component": "tr", "content": [
                    {"component": "td", "text": "属性专区"},
                    {"component": "td", "text": rule["name"]},
                    {"component": "td", "text": "已启用"},
                    {"component": "td", "text": str(attribute_counts.get(key, 0))},
                ]})
        source_rows: List[dict] = []
        if self._ranking_enabled:
            for key in sorted(self._selected_rankings):
                meta = RANK_META[key]
                status = (self._state.get("source_status") or {}).get(key) or {}
                if status.get("ok"):
                    label = "正常"
                    detail = str(status.get("source") or "")
                elif status:
                    label = "失败，已保留旧内容"
                    detail = str(status.get("error") or "")
                else:
                    label = "等待首次同步"
                    detail = ""
                result_rows.append({"component": "tr", "content": [
                    {"component": "td", "text": "榜单"},
                    {"component": "td", "text": meta["collection"]},
                    {"component": "td", "text": label},
                    {"component": "td", "text": str(ranking_counts.get(key, 0))},
                ]})
                source_rows.append({"component": "tr", "content": [
                    {"component": "td", "text": meta["collection"]},
                    {"component": "td", "text": label},
                    {"component": "td", "text": detail},
                ]})
        if not result_rows:
            result_rows = [{"component": "tr", "content": [
                {"component": "td", "props": {"colspan": 4},
                 "text": "尚未启用虚拟库，请回到配置页勾选后保存。"},
            ]}]
        log_rows = [{"component": "tr", "content": [
            {"component": "td", "text": str(item.get("time", ""))},
            {"component": "td", "text": str(item.get("level", "INFO"))},
            {"component": "td", "text": str(item.get("message", ""))},
        ]} for item in reversed(list(self._logs)[-50:])]
        connection_test = self._state.get("connection_test") or {}
        connection_type = (
            "success" if connection_test.get("ok") else "error" if connection_test else "info"
        )
        connection_message = str(
            connection_test.get("message") or "尚未测试；将复用 MoviePilot 已配置的 Emby。"
        )
        schedule_message = (
            f"Cron {self._sync_cron}"
            if self._daily_sync_enabled else "未启用定时更新"
        )
        token = self._secret(getattr(settings, "API_TOKEN", ""))
        return [{"component": "div", "props": {"class": "pa-3"}, "content": [
            {"component": "VAlert", "props": {
                "type": state_type, "variant": "tonal", "class": "mb-3",
                "title": state_label,
                "text": (
                    f"{self._runtime.get('message') or '保存配置后点击一键重建。'}\n"
                    f"最后成功同步：{self._state.get('last_sync') or '暂无'}\n"
                    f"定时更新：{schedule_message}"
                ),
            }},
            {"component": "VAlert", "props": {
                "type": connection_type, "variant": "tonal", "class": "mb-3",
                "title": "Emby 连接", "text": connection_message,
            }},
            {"component": "VAlert", "props": {
                "type": "success" if self._proxy_status.get("running") else "warning",
                "variant": "tonal", "class": "mb-3",
                "title": "8098 虚拟库入口",
                "text": (
                    f"{self._proxy_status.get('message') or 'MoviePilot 内置网关未注册'}；"
                    f"NextEmby 对外地址固定为 http://NAS-IP:{self.PUBLIC_GATEWAY_PORT}"
                ),
            }},
            {"component": "div", "props": {"class": "d-flex flex-wrap ga-3 mb-3"}, "content": [
                {"component": "VBtn", "props": {
                    "color": "primary", "variant": "outlined", "prepend-icon": "mdi-lan-connect",
                }, "text": "测试连接", "events": {"click": {
                    "api": "plugin/MediaArchiver/test_connection", "method": "post",
                    "params": {"token": token},
                }}},
                {"component": "VBtn", "props": {
                    "color": "primary", "variant": "flat", "prepend-icon": "mdi-refresh",
                }, "text": "一键重建", "events": {"click": {
                    "api": "plugin/MediaArchiver/rebuild", "method": "post",
                    "params": {"token": token},
                }}},
            ]},
            {"component": "VRow", "props": {"class": "mb-3"}, "content": [
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                        {"component": "VCardText", "text": f"{int(stats.get('scanned') or 0)}\n本次扫描"},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                        {"component": "VCardText", "text": f"{sum(ranking_counts.values())}\n榜单命中"},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                        {"component": "VCardText", "text": f"{sum(attribute_counts.values())}\n属性命中"},
                    ]},
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                        {"component": "VCardText", "text": f"{active_server_count}\n生效服务器"},
                    ]},
                ]},
            ]},
            {"component": "div", "props": {"class": "text-h5 font-weight-bold mb-2"},
             "text": "虚拟库结果"},
            {"component": "VTable", "props": {"density": "compact", "hover": True}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": "类型"}, {"component": "th", "text": "名称"},
                    {"component": "th", "text": "状态"}, {"component": "th", "text": "命中"},
                ]}]},
                {"component": "tbody", "content": result_rows},
            ]},
            {"component": "VExpansionPanels", "props": {
                "multiple": True, "variant": "accordion", "class": "mt-4",
            }, "content": [
                {"component": "VExpansionPanel", "content": [
                    {"component": "VExpansionPanelTitle", "content": [
                        {"component": "VIcon", "props": {"icon": "mdi-tools", "class": "mr-2"}},
                        {"component": "span", "text": "故障详情与运行日志（需要时展开）"},
                    ]},
                    {"component": "VExpansionPanelText", "content": [
                        {"component": "div", "props": {"class": "text-h6 mb-2"}, "text": "榜单数据源"},
                        {"component": "VTable", "props": {"density": "compact", "hover": True}, "content": [
                            {"component": "thead", "content": [{"component": "tr", "content": [
                                {"component": "th", "text": "榜单"}, {"component": "th", "text": "状态"},
                                {"component": "th", "text": "来源或错误"},
                            ]}]},
                            {"component": "tbody", "content": source_rows or [{"component": "tr", "content": [
                                {"component": "td", "props": {"colspan": 3}, "text": "暂无榜单运行记录"},
                            ]}]},
                        ]},
                        {"component": "div", "props": {"class": "text-h6 my-2"}, "text": "最近 50 条日志"},
                        {"component": "VTable", "props": {"density": "compact", "hover": True}, "content": [
                            {"component": "thead", "content": [{"component": "tr", "content": [
                                {"component": "th", "text": "时间"}, {"component": "th", "text": "级别"},
                                {"component": "th", "text": "内容"},
                            ]}]},
                            {"component": "tbody", "content": log_rows or [{"component": "tr", "content": [
                                {"component": "td", "props": {"colspan": 3}, "text": "暂无日志"},
                            ]}]},
                        ]},
                    ]},
                ]},
            ]},
        ]}]

    def _start_sync(self, mode: str = "rebuild") -> schemas.Response:
        """启动后台同步，避免在 MoviePilot API/UI 请求线程中长时间阻塞。"""
        if not self.get_state():
            return schemas.Response(
                success=False, message="请至少启用媒体属性专区或榜单虚拟库其中一项"
            )
        if self._stopping:
            return schemas.Response(success=False, message="插件正在停止")
        if not self._run_lock.acquire(blocking=False):
            return schemas.Response(success=False, message="已有同步任务运行中，请勿重复点击")
        worker = threading.Thread(
            target=self._sync_worker, args=(mode,),
            name="MediaVirtualLibrarySync", daemon=True,
        )
        worker.start()
        return schemas.Response(success=True, message="已启动后台同步，请在插件数据页查看进度")

    def _scheduled_sync(self, mode: str = "scheduled", schedule: str = "") -> None:
        if self.get_state() and not self._stopping:
            if mode == "timed":
                self._record("INFO", f"Cron {schedule} 已触发，准备更新全部专区")
            response = self._start_sync(mode)
            if not getattr(response, "success", False):
                logger.debug("[媒体虚拟库] 定时同步未启动：%s", getattr(response, "message", ""))

    def _sync_worker(self, mode: str) -> None:
        started = time.monotonic()
        self._runtime = {
            "state": "running", "message": "正在连接 Emby 并计算差异……",
            "mode": mode, "stats": {},
        }
        mode_name = {
            "incremental": "增量校准",
            "timed": "定时全量更新",
            "scheduled": "启动校准",
            "rebuild": "全量重建",
        }.get(mode, "全量重建")
        self._record("INFO", f"开始{mode_name}")
        try:
            clients = self._create_clients()
            servers_state = self._state.setdefault("servers", {})

            ranking_results: Dict[str, RankingResult] = {}
            if self._ranking_enabled and self._selected_rankings:
                cache_complete = all(key in self._ranking_cache for key in self._selected_rankings)
                if mode == "incremental" and cache_complete:
                    ranking_results = {
                        key: self._ranking_cache[key] for key in self._selected_rankings
                    }
                    self._record("INFO", "增量校准复用榜单缓存，不重复请求外部榜单源")
                else:
                    ranking_results = self._make_fetcher().fetch(self._selected_rankings)
                    self._ranking_cache = dict(ranking_results)
            elif not self._ranking_enabled:
                self._ranking_cache = {}

            self._state["source_status"] = {
                key: {
                    "ok": result.ok, "source": result.source,
                    "error": result.error, "items": len(result.entries),
                }
                for key, result in ranking_results.items()
            }

            totals = {
                "servers": 0, "scanned": 0, "added": 0, "removed": 0,
                "attribute_hits": 0, "ranking_hits": 0, "ranking_failed": 0,
            }
            failures: List[str] = []
            for client, server_name in clients:
                if self._stopping:
                    raise RuntimeError("插件已停止")
                try:
                    stats = self._sync_server(client, server_name, ranking_results)
                    totals["servers"] += 1
                    for key in ("scanned", "added", "removed", "attribute_hits",
                                "ranking_hits", "ranking_failed"):
                        totals[key] += int(stats.get(key) or 0)
                except Exception as err:
                    message = f"{server_name}：{err}"
                    failures.append(message)
                    self._record("ERROR", f"服务器同步失败：{message}")

            if not totals["servers"]:
                raise RuntimeError("所有 Emby 服务器均同步失败；" + "；".join(failures))
            # 临时断线不能让已保存的虚拟库失效。仅当用户明确选择了服务器，
            # 且至少一台完成同步后，才停用已经不在选择列表中的旧状态。
            selected_names = set(self._emby_servers)
            if selected_names:
                for value in servers_state.values():
                    if isinstance(value, dict) and str(value.get("name") or "") not in selected_names:
                        value["active"] = False
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            self._state["last_sync"] = now
            self._save_state()
            seconds = round(time.monotonic() - started, 2)
            suffix = f"；{len(failures)} 台服务器失败" if failures else ""
            message = (
                f"完成：{totals['servers']} 台 Emby，扫描 {totals['scanned']} 项，"
                f"新增 {totals['added']}，移除 {totals['removed']}，用时 {seconds}s{suffix}"
            )
            self._runtime = {
                "state": "completed_with_warnings" if failures else "completed",
                "message": message, "mode": mode, "stats": totals,
            }
            self._record("WARNING" if failures else "INFO", message)
        except Exception as err:
            message = str(err)
            self._runtime = {"state": "failed", "message": message, "mode": mode, "stats": {}}
            self._record("ERROR", f"同步终止：{message}")
            try:
                self._save_state()
            except Exception:
                pass
        finally:
            self._run_lock.release()

    def _make_fetcher(self) -> RankingFetcher:
        tmdb_key = self._tmdb_key or self._secret(getattr(settings, "TMDB_API_KEY", ""))
        tmdb_domain = self._tmdb_domain or str(
            getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org") or "api.themoviedb.org"
        )
        return RankingFetcher(
            tmdb_key=tmdb_key, tmdb_domain=tmdb_domain,
            language=self._ranking_language, regions=self._ranking_regions,
            limit=self._ranking_limit, timeout=self._timeout,
            feed_url=self._ranking_feed_url, feed_token=self._ranking_feed_token,
            log=self._record,
        )

    def _sync_server(
        self,
        client: EmbyClient,
        server_name: str,
        ranking_results: Mapping[str, RankingResult],
    ) -> Dict[str, int]:
        """计算首页虚拟库成员；不调用 ``/Collections``。"""
        # 去重集中在此处；后续复用字典视图，不再复制整库列表。
        item_map = {str(item["Id"]): item for item in client.library_items() if item.get("Id")}
        items = item_map.values()
        index = None
        identity = f"{server_name}|{client.api_root}"
        servers = self._state.setdefault("servers", {})
        state = servers.setdefault(identity, {})
        state.update({
            "name": server_name, "api_root": client.api_root,
            "active": True, "last_error": "",
        })
        previous_views = {
            str(key): dict(value) for key, value in (state.get("virtual_views") or {}).items()
            if isinstance(value, Mapping)
        }
        next_views: Dict[str, Dict[str, Any]] = {}
        attribute_counts: Dict[str, int] = {}
        ranking_counts: Dict[str, int] = {}
        ranking_failed = 0
        now = datetime.now().astimezone().isoformat(timespec="seconds")

        desired_attributes: Dict[str, Set[str]] = {
            key: set() for key in self.ATTRIBUTE_RULES
        }
        if self._attribute_enabled and self._enabled_rules:
            for item in items:
                if str(item.get("Type") or "Movie").casefold() != "movie":
                    continue
                item_id = str(item["Id"])
                for key in self._classify(item):
                    if key in self._enabled_rules:
                        desired_attributes[key].add(item_id)

        for key, rule in self.ATTRIBUTE_RULES.items():
            enabled = self._attribute_enabled and key in self._enabled_rules
            wanted = desired_attributes[key] if enabled else set()
            attribute_counts[key] = len(wanted)
            if enabled:
                view_key = f"attribute:{key}"
                next_views[view_key] = self._make_virtual_view(
                    view_key, rule["name"], "attribute", wanted, item_map, now, "movies"
                )

        active_rankings = self._selected_rankings if self._ranking_enabled else set()
        for key in (rank_key for rank_key in RANK_META if rank_key in active_rankings):
            result = ranking_results.get(key)
            name = RANK_META[key]["collection"]
            view_key = f"ranking:{key}"
            if not result or not result.ok:
                ranking_failed += 1
                old = previous_views.get(view_key)
                if old:
                    next_views[view_key] = old
                    ranking_counts[key] = len(old.get("item_ids") or [])
                else:
                    ranking_counts[key] = 0
                    next_views[view_key] = self._make_virtual_view(
                        view_key, name, "ranking", set(), item_map, now,
                        self._ranking_collection_type(key),
                    )
                # 外部榜单源失败时保留上次内存/持久结果，不误清空首页栏目。
                continue
            if index is None:
                index = LibraryIndex(items)
            wanted = index.match(result.entries)
            ranking_counts[key] = len(wanted)
            next_views[view_key] = self._make_virtual_view(
                view_key, name, "ranking", wanted, item_map, now,
                self._ranking_collection_type(key),
            )

        for key in RANK_META:
            ranking_counts.setdefault(key, 0)

        added = removed = 0
        for view_key in set(previous_views) | set(next_views):
            old_ids = {
                str(value) for value in (previous_views.get(view_key, {}).get("item_ids") or [])
            }
            new_ids = {
                str(value) for value in (next_views.get(view_key, {}).get("item_ids") or [])
            }
            added += len(new_ids - old_ids)
            removed += len(old_ids - new_ids)

        state.update({
            "attribute_counts": attribute_counts,
            "ranking_counts": ranking_counts,
            "virtual_views": next_views,
            "last_sync": now,
        })
        primary_name = self._emby_servers[0] if self._emby_servers else ""
        is_primary = (
            self._is_proxy_origin(client.api_root)
            or (not self._gateway_api_root and (not primary_name or server_name == primary_name))
        )
        if is_primary:
            self._gateway_client_cache = client
            self._gateway_server_name = server_name
            self._gateway_api_root = client.api_root
            # 先构造完整快照，持锁时只替换引用，浏览请求不等待整库整理。
            proxy_views = {
                str(value["id"]): dict(value) for value in next_views.values()
                if value.get("id")
            }
            proxy_index = {key: self._compact_proxy_item(value) for key, value in item_map.items()}
            with self._proxy_lock:
                self._virtual_views = proxy_views
                self._proxy_item_index = proxy_index
        attribute_hits = sum(attribute_counts.values())
        ranking_hits = sum(ranking_counts.values())
        self._record(
            "INFO",
            f"{server_name}：扫描 {len(items)} 项，属性命中 {attribute_hits}，"
            f"榜单命中 {ranking_hits}，首页虚拟库 {len(next_views)} 个，"
            f"新增 {added}，移除 {removed}",
        )
        return {
            "scanned": len(items), "added": added, "removed": removed,
            "attribute_hits": attribute_hits, "ranking_hits": ranking_hits,
            "ranking_failed": ranking_failed,
        }

    def _make_virtual_view(
        self,
        key: str,
        name: str,
        kind: str,
        item_ids: Iterable[str],
        item_map: Mapping[str, Mapping[str, Any]],
        updated: str,
        collection_type_hint: str = "",
    ) -> Dict[str, Any]:
        ids = sorted({str(value) for value in item_ids if str(value)})
        types = {
            str(item_map.get(item_id, {}).get("Type") or "").casefold() for item_id in ids
        }
        if types and types <= {"movie"}:
            collection_type = "movies"
        elif types and types <= {"series"}:
            collection_type = "tvshows"
        else:
            collection_type = collection_type_hint or "mixed"
        server_id = next(
            (str(item_map[item_id].get("ServerId")) for item_id in ids
             if item_id in item_map and item_map[item_id].get("ServerId")),
            "",
        )
        return {
            "id": self._view_id(key), "key": key, "name": name, "kind": kind,
            "collection_type": collection_type, "item_ids": ids,
            "server_id": server_id, "updated": updated,
            "cover_tag": self._cover_tag(key, ids),
        }

    @staticmethod
    def _ranking_collection_type(key: str) -> str:
        folded = str(key or "").casefold()
        if folded.endswith("_movie") or (
            folded.startswith("douban_") and "tv_" not in folded
        ):
            return "movies"
        if folded.endswith("_series") or "_tv_" in folded:
            return "tvshows"
        return "mixed"

    def _is_proxy_origin(self, api_root: str) -> bool:
        return bool(self._gateway_api_root and api_root.rstrip("/") == self._gateway_api_root.rstrip("/"))

    @eventmanager.register(EventType.WebhookMessage)
    def on_webhook(self, event: Event) -> None:
        """Emby 新增、更新、删除后防抖触发差异校准。"""
        if not (self.get_state() and self._auto_sync) or self._stopping:
            return
        info = getattr(event, "event_data", None)
        if not info:
            return
        event_name = str(self._object_value(info, "event", "Event") or "").casefold()
        if event_name and event_name not in self.EVENT_TYPES:
            return
        server_name = str(self._object_value(info, "server_name", "ServerName") or "*")
        item_id = str(self._object_value(info, "item_id", "ItemId", "id", "Id") or "")
        raw_object = self._object_value(info, "json_object", "JsonObject")
        if not item_id and isinstance(raw_object, Mapping):
            item = raw_object.get("Item") or raw_object.get("item") or {}
            if isinstance(item, Mapping):
                item_id = str(item.get("Id") or item.get("id") or "")
        with self._event_lock:
            self._pending_ids.setdefault(server_name, set()).add(item_id or "*")
            if self._event_timer:
                self._event_timer.cancel()
            self._event_timer = threading.Timer(8.0, self._flush_events)
            self._event_timer.daemon = True
            self._event_timer.start()

    def _flush_events(self) -> None:
        if self._stopping:
            return
        with self._event_lock:
            if self._run_lock.locked():
                self._event_timer = threading.Timer(10.0, self._flush_events)
                self._event_timer.daemon = True
                self._event_timer.start()
                return
            count = sum(len(values) for values in self._pending_ids.values())
            self._pending_ids.clear()
            self._event_timer = None
        if count:
            self._record("INFO", f"合并 {count} 个 Emby Webhook 变更，开始增量校准")
            self._start_sync("incremental")

    @staticmethod
    def _compact_proxy_item(item: Mapping[str, Any]) -> Dict[str, Any]:
        """只保留虚拟库筛选/排序需要的字段，避免常驻完整媒体流。"""
        fields = (
            "Type", "Name", "OriginalTitle", "SortName", "ProductionYear",
            "DateCreated", "DateLastSaved", "PremiereDate", "CommunityRating",
            "CriticRating",
        )
        return {key: item[key] for key in fields if item.get(key) not in (None, "")}

    @classmethod
    def _iter_scalar_values(cls, value: Any, depth: int = 0) -> Iterator[str]:
        """逐个产生标量文本，避免递归过程中反复创建列表或拼接字符串。"""
        if depth > 8 or value is None:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                yield str(key)
                yield from cls._iter_scalar_values(child, depth + 1)
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                yield from cls._iter_scalar_values(child, depth + 1)
            return
        if isinstance(value, (str, int, float, bool)):
            yield str(value)

    @classmethod
    def _scalar_text(cls, value: Any) -> str:
        return " ".join(cls._iter_scalar_values(value)).casefold()

    def _classify(self, item: Mapping[str, Any]) -> Set[str]:
        """先读取结构化媒体流，再用路径/文件名关键字回退。"""
        matched: Set[str] = set()
        sources = item.get("MediaSources") or []
        if not isinstance(sources, list):
            sources = []
        streams: List[Mapping[str, Any]] = []
        item_streams = item.get("MediaStreams") or []
        if isinstance(item_streams, list):
            streams.extend(x for x in item_streams if isinstance(x, Mapping))
        for source in sources:
            if isinstance(source, Mapping) and isinstance(source.get("MediaStreams"), list):
                streams.extend(x for x in source["MediaStreams"] if isinstance(x, Mapping))

        item_text = self._scalar_text({
            key: item.get(key) for key in (
                "Path", "FileName", "Name", "OriginalTitle", "SortName", "Container"
            ) if item.get(key) not in (None, "")
        })
        source_text = self._scalar_text([
            {key: value for key, value in source.items()
             if str(key).casefold() != "mediastreams"}
            for source in sources if isinstance(source, Mapping)
        ])
        stream_text = self._scalar_text(streams)
        all_text = " ".join((item_text, source_text, stream_text))
        if "remux" in all_text:
            matched.add("remux")

        candidates: List[Mapping[str, Any]] = [item]
        candidates.extend(x for x in sources if isinstance(x, Mapping))
        candidates.extend(streams)
        if any(
            self._number(value.get("Width") or value.get("width")) >= 3840
            or self._number(value.get("Height") or value.get("height")) >= 2160
            for value in candidates
        ) or re.search(
            r"(?<!\w)(?:2160p|4k|uhd)(?!\w)", all_text, flags=re.I
        ):
            matched.add("4k")

        dv_pattern = r"dolby[ ._-]*vision|\bdovi\b|\bdv(?:he|av|\d|\b)"
        hdr_pattern = r"\bhdr10\+?\b|\bhdr\b|\bhlg\b|smpte2084|\bpq\b|dolby[ ._-]*vision|\bdovi\b"
        if re.search(dv_pattern, all_text, flags=re.I):
            matched.add("dolby_vision")
        if re.search(hdr_pattern, all_text, flags=re.I):
            matched.add("hdr")

        atmos_pattern = r"\batmos\b|\bjoc\b|e-?ac-?3[^\n]{0,30}joc|truehd[^\n]{0,30}atmos"
        if re.search(atmos_pattern, all_text, flags=re.I):
            matched.add("atmos")
        return matched

    @staticmethod
    def _number(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            match = re.search(r"\d+", str(value or ""))
            return int(match.group()) if match else 0

    def _create_clients(self) -> List[Tuple[EmbyClient, str]]:
        """只复用 MoviePilot 已配置的 Emby，不再保存第二份地址或 API Key。"""
        configs: Dict[str, Any] = {}
        services: Dict[str, Any] = {}
        errors: List[str] = []
        clients: List[Tuple[EmbyClient, str]] = []
        seen_roots: Set[str] = set()
        helper = MediaServerHelper()
        try:
            raw_configs = helper.get_configs() or {}
            if isinstance(raw_configs, Mapping):
                configs = {str(key): value for key, value in raw_configs.items()}
        except Exception as err:
            errors.append(f"读取 MoviePilot 服务器配置失败：{err}")

        names = list(self._emby_servers)
        if not names:
            for key, config in configs.items():
                name = str(self._object_value(config, "name", "Name") or key)
                kind = str(self._object_value(config, "type", "Type", "kind") or "")
                if not kind or "emby" in kind.casefold():
                    names.append(name)
        try:
            try:
                raw_services = helper.get_services(
                    type_filter="emby", name_filters=names or None
                ) or {}
            except TypeError:
                raw_services = helper.get_services(type_filter="emby") or {}
            if isinstance(raw_services, Mapping):
                services = {str(key): value for key, value in raw_services.items()}
        except Exception as err:
            errors.append(f"读取 MoviePilot Emby 实例失败：{err}")

        if not names:
            names = list(services)
        for name in dict.fromkeys(names):
            service_info = services.get(name)
            config = configs.get(name)
            if config is None:
                for key, candidate in configs.items():
                    candidate_name = str(self._object_value(candidate, "name", "Name") or key)
                    if candidate_name == name:
                        config = candidate
                        break
            instance = self._object_value(service_info, "instance", "client")
            try:
                inactive = getattr(instance, "is_inactive", None)
                if callable(inactive) and inactive():
                    raise RuntimeError("服务器当前未连接")
                url, api_key = self._extract_connection(config, service_info, instance)
                if not url or not api_key:
                    raise RuntimeError(
                        "当前 MoviePilot 版本未暴露 Emby 地址/API Key，"
                        "请先在 MoviePilot 媒体服务器中重新保存 Emby 配置"
                    )
                client = EmbyClient(url, api_key, self._timeout)
                if client.api_root not in seen_roots:
                    seen_roots.add(client.api_root)
                    clients.append((client, name))
            except Exception as err:
                errors.append(f"{name}：{err}")

        for message in errors:
            self._record("WARNING", message)
        if not clients:
            detail = "；".join(errors[-5:]) if errors else "未选择 Emby 服务器"
            raise RuntimeError("没有可用的 Emby 连接；" + detail)
        return clients

    @classmethod
    def _extract_connection(cls, *objects: Any) -> Tuple[str, str]:
        url = api_key = ""
        port = ""
        visited: Set[int] = set()

        def visit(value: Any, depth: int = 0) -> None:
            nonlocal url, api_key, port
            if value is None or depth > 4 or id(value) in visited:
                return
            visited.add(id(value))
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("{"):
                    try:
                        visit(json.loads(text), depth + 1)
                    except Exception:
                        pass
                return
            if isinstance(value, Mapping):
                mapping = {str(key).casefold(): child for key, child in value.items()}
            else:
                model_dump = getattr(value, "model_dump", None)
                if callable(model_dump):
                    try:
                        visit(model_dump(), depth + 1)
                    except Exception:
                        pass
                mapping = {}
                for key in (
                    "url", "host", "address", "server_url", "base_url", "host_url",
                    "endpoint", "uri",
                    "_url", "_host", "port", "_port", "api_key", "apikey",
                    "token", "access_token", "_api_key", "_apikey", "_token",
                    "config", "data", "settings", "instance", "client", "emby", "_emby",
                ):
                    if hasattr(value, key):
                        try:
                            mapping[key.casefold()] = getattr(value, key)
                        except Exception:
                            pass
            for key in (
                "url", "host", "address", "server_url", "base_url", "host_url",
                "endpoint", "uri", "_url", "_host",
            ):
                candidate = cls._secret(mapping.get(key))
                if not url and candidate:
                    url = candidate
            for key in (
                "api_key", "apikey", "token", "access_token",
                "_api_key", "_apikey", "_token",
            ):
                candidate = cls._secret(mapping.get(key))
                if not api_key and candidate and candidate != "**********":
                    api_key = candidate
            if not port:
                port = cls._secret(mapping.get("port") or mapping.get("_port"))
            for key in ("config", "data", "settings", "instance", "client", "emby", "_emby"):
                if key in mapping:
                    visit(mapping[key], depth + 1)

        for obj in objects:
            visit(obj)
        if url:
            if not url.startswith(("http://", "https://")):
                url = "http://" + url
            parsed = urllib.parse.urlsplit(url)
            if port and parsed.hostname and parsed.port is None:
                host = parsed.hostname
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                netloc = f"{host}:{port}"
                if parsed.username:
                    auth = parsed.username
                    if parsed.password:
                        auth += ":" + parsed.password
                    netloc = auth + "@" + netloc
                url = urllib.parse.urlunsplit(
                    (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
                )
        return url.rstrip("/"), api_key

    @staticmethod
    def _secret(value: Any) -> str:
        if value is None:
            return ""
        getter = getattr(value, "get_secret_value", None)
        if callable(getter):
            try:
                return str(getter()).strip()
            except Exception:
                pass
        return str(value).strip()

    @staticmethod
    def _object_value(value: Any, *names: str) -> Any:
        if value is None:
            return None
        if isinstance(value, Mapping):
            folded = {str(key).casefold(): child for key, child in value.items()}
            for name in names:
                if name.casefold() in folded:
                    return folded[name.casefold()]
            return None
        for name in names:
            if hasattr(value, name):
                try:
                    return getattr(value, name)
                except Exception:
                    continue
        return None

    def _record(self, level: str, message: str) -> None:
        level = str(level or "INFO").upper()
        text = str(message)
        self._logs.append({
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": level, "message": text,
        })
        log_method = {
            "DEBUG": logger.debug, "WARNING": logger.warning,
            "ERROR": logger.error,
        }.get(level, logger.info)
        log_method("[媒体虚拟库] %s", text)

    def _cancel_timers(self) -> None:
        for timer in (getattr(self, "_event_timer", None), getattr(self, "_boot_timer", None)):
            if timer:
                try:
                    timer.cancel()
                except Exception:
                    pass
        self._event_timer = None
        self._boot_timer = None

    def stop_service(self) -> None:
        self._stopping = True
        self._cancel_timers()
        self._remove_gateway_routes()
        self._close_httpx_client()
        with self._event_lock:
            self._pending_ids.clear()
