"""MoviePilot v2：媒体属性 + 榜单首页虚拟媒体库。

通过轻量 Emby 反向代理向 ``/Users/{id}/Views`` 注入一级媒体库，
再把虚拟库查询映射回原媒体 ItemId。不创建 Collection/BoxSet，不访问、
不移动、不复制、不重命名真实媒体文件，也不改写 MediaSource.Path。
"""
from __future__ import annotations

import html
import hashlib
import http.client
import http.server
import io
import json
import math
import re
import select
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

try:
    from apscheduler.triggers.interval import IntervalTrigger
except Exception:  # pragma: no cover - 极老 v2 宿主兼容
    IntervalTrigger = None


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
                "User-Agent": "MoviePilot-MediaVirtualLibrary/4.2.0",
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
        items = self._paged_items(
            Recursive=True,
            IncludeItemTypes="Movie,Series",
            Fields=self.ITEM_FIELDS,
        )
        return list({str(item.get("Id")): item for item in items if item.get("Id")}.values())

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.request_json(
                "GET",
                f"/Items/{urllib.parse.quote(str(item_id), safe='')}",
                {"Fields": self.ITEM_FIELDS},
            )
        except EmbyHttpError as err:
            if err.status == 404:
                return None
            raise

    def collection_members(self, collection_id: str) -> Set[str]:
        items = self._paged_items(
            ParentId=collection_id,
            Recursive=True,
            IncludeItemTypes="Movie,Series",
            Fields="Path",
        )
        return {str(item.get("Id")) for item in items if item.get("Id")}

    @staticmethod
    def _chunks(values: Iterable[str], size: int = 100) -> Iterable[List[str]]:
        batch: List[str] = []
        for value in values:
            batch.append(str(value))
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    def remove_items(self, collection_id: str, item_ids: Iterable[str]) -> None:
        for batch in self._chunks(sorted(set(item_ids))):
            path = f"/Collections/{urllib.parse.quote(collection_id, safe='')}/Items"
            query = {"Ids": ",".join(batch)}
            try:
                self.request_json("DELETE", path, query, expected=(200, 204))
            except EmbyHttpError as err:
                if err.status not in (404, 405):
                    raise
                self.request_json("POST", path + "/Delete", query, expected=(200, 204))


class VirtualProxyServer(http.server.ThreadingHTTPServer):
    """MoviePilot 插件内的轻量 Emby 反代服务。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], owner: Any):
        self.owner = owner
        super().__init__(address, VirtualProxyHandler)


class VirtualProxyHandler(http.server.BaseHTTPRequestHandler):
    """仅负责 HTTP 入口；虚拟视图与上游转发由插件实例处理。"""

    protocol_version = "HTTP/1.1"
    server_version = "MediaVirtualLibrary/4.0"

    def _dispatch(self) -> None:
        try:
            self.server.owner._proxy_dispatch(self)  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:  # pragma: no cover - 真实网络容错
            try:
                self.server.owner._record("ERROR", f"虚拟库反代请求失败：{err}")  # type: ignore[attr-defined]
                payload = json.dumps(
                    {"error": "Media virtual-library proxy failed", "detail": str(err)},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except Exception:
                pass
            self.close_connection = True

    do_GET = _dispatch
    do_HEAD = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch

    def log_message(self, fmt: str, *args: Any) -> None:
        # 避免每个海报/API 请求刷屏，错误会进入插件运行日志。
        return


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
            "User-Agent": "Mozilla/5.0 MoviePilot-MediaVirtualLibrary/4.2.0",
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
            headers={"User-Agent": "MoviePilot-MediaVirtualLibrary/4.2.0 (private use)"},
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

    def __init__(self, items: Sequence[Mapping[str, Any]]):
        self.items = [item for item in items if item.get("Id")]
        self.provider: Dict[Tuple[str, str, str], Set[str]] = {}
        self.title_year: Dict[Tuple[str, str, int], Set[str]] = {}
        self.title_only: Dict[Tuple[str, str], Set[str]] = {}
        for item in self.items:
            item_id = str(item["Id"])
            kind = self._type(item.get("Type"))
            providers = item.get("ProviderIds") or {}
            if isinstance(providers, Mapping):
                for raw_key, raw_value in providers.items():
                    provider = self._provider_name(str(raw_key))
                    if provider and raw_value not in (None, ""):
                        self.provider.setdefault((kind, provider, str(raw_value).casefold()), set()).add(item_id)
            year = RankingFetcher._year(item.get("ProductionYear") or item.get("PremiereDate"))
            for field in ("Name", "OriginalTitle", "SortName"):
                title = self._normalize(item.get(field))
                if not title:
                    continue
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
        for name, aliases in cls.PROVIDERS.items():
            if folded in {re.sub(r"[^a-z0-9]", "", alias.casefold()) for alias in aliases}:
                return name
        return ""

    @staticmethod
    def _normalize(value: Any) -> str:
        text = html.unescape(str(value or "")).casefold()
        return re.sub(r"[^\w\u3400-\u9fff]+", "", text, flags=re.UNICODE)


class MediaArchiver(_PluginBase):
    """媒体属性专区与独立榜单首页虚拟库。"""

    plugin_name = "媒体虚拟库"
    plugin_desc = "通过现有8098反代输出媒体属性与平台榜单一级虚拟库，不创建合集。"
    plugin_icon = "folder-move.svg"
    plugin_version = "4.2.0"
    plugin_author = "Boss"
    author_url = "https://github.com/ZangBanzi"
    plugin_config_prefix = "mediaarchiver_"
    plugin_order = 50
    auth_level = 1
    PUBLIC_GATEWAY_PORT = 8098
    # 仅供现有 8098 反代调用，不作为客户端入口，也无需对公网开放。
    INTERNAL_INJECT_PORT = 8097

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
    # 向后兼容 v3.0.0 测试和外部调用。
    RULES = ATTRIBUTE_RULES
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
        self._emby_servers: List[str] = []
        self._manual_url = ""
        self._manual_key = ""
        self._proxy_bind = "0.0.0.0"
        self._proxy_port = self.INTERNAL_INJECT_PORT
        self._proxy_server: Optional[VirtualProxyServer] = None
        self._proxy_thread: Optional[threading.Thread] = None
        self._proxy_lock = threading.RLock()
        self._virtual_views: Dict[str, Dict[str, Any]] = {}
        self._proxy_item_index: Dict[str, Dict[str, Any]] = {}
        self._cover_cache: Dict[str, Tuple[str, bytes]] = {}
        self._proxy_status: Dict[str, Any] = {
            "running": False, "message": "反代未启动", "requests": 0,
        }
        self._use_moviepilot_servers = False
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
        self._config: Dict[str, Any] = {}
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
        self._stop_proxy()
        self._cancel_timers()
        self._stopping = False
        cfg = dict(config or {})
        self._config = cfg
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
        selected_servers = cfg.get("emby_servers") or []
        if isinstance(selected_servers, str):
            selected_servers = [selected_servers]
        legacy_server = str(cfg.get("emby_server") or "").strip()
        if legacy_server and legacy_server not in selected_servers:
            selected_servers.append(legacy_server)
        self._emby_servers = [str(x).strip() for x in selected_servers if str(x).strip()]
        self._manual_url = str(cfg.get("manual_url") or "").strip()
        incoming_key = str(cfg.get("manual_api_key") or "").strip()
        # 密码框留空或回传星号时沿用已保存 Key，避免只改端口就丢失连接。
        saved_key = str(self.get_data("manual_api_key_secret") or "").strip()
        if not incoming_key or set(incoming_key) == {"*"}:
            incoming_key = saved_key or self._manual_key
        elif incoming_key:
            try:
                self.save_data("manual_api_key_secret", incoming_key)
            except Exception as err:
                logger.warning("[媒体虚拟库] 保存 Emby API Key 失败：%s", err)
        self._manual_key = incoming_key
        # v4.2 起一级虚拟库与品牌封面是插件固定能力，不再提供重复开关。
        # 8097 只作为 8098 反代的容器内网后端，客户端始终继续使用 8098。
        self._proxy_bind = "0.0.0.0"
        self._proxy_port = self.INTERNAL_INJECT_PORT
        self._use_moviepilot_servers = bool(cfg.get("use_moviepilot_servers", False))
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

        if self.get_state():
            self._start_proxy()

        if bool(cfg.get("rebuild_once", False)):
            reset = dict(cfg)
            reset["rebuild_once"] = False
            try:
                self.update_config(reset)
            except Exception as err:
                logger.warning("[媒体虚拟库] 重建开关复位失败：%s", err)
            self._boot_timer = threading.Timer(1.0, self._start_sync, kwargs={"mode": "rebuild"})
            self._boot_timer.daemon = True
            self._boot_timer.start()
        elif self.get_state() and self._auto_sync:
            self._boot_timer = threading.Timer(15.0, self._scheduled_sync)
            self._boot_timer.daemon = True
            self._boot_timer.start()

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

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
        try:
            wanted = urllib.parse.urlsplit(self._manual_url).netloc.casefold()
        except Exception:
            wanted = ""
        if wanted:
            for value in states:
                try:
                    if urllib.parse.urlsplit(str(value.get("api_root") or "")).netloc.casefold() == wanted:
                        selected = value
                        break
                except Exception:
                    continue
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

    def _start_proxy(self) -> None:
        """启动首页虚拟库反代；绑定失败不阻止 MoviePilot 本体启动。"""
        if self._proxy_server or self._stopping:
            return
        if not self._manual_url:
            self._proxy_status = {
                "running": False, "message": "请先填写原生 Emby 内网地址", "requests": 0,
            }
            return
        parsed = urllib.parse.urlsplit(self._manual_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            self._proxy_status = {
                "running": False, "message": "原生 Emby 内网地址无效", "requests": 0,
            }
            return
        upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if upstream_port == self.PUBLIC_GATEWAY_PORT:
            self._proxy_status = {
                "running": False,
                "message": "上游不能填写8098；请填写8098背后的原生Emby地址（通常8096）",
                "requests": 0,
            }
            self._record("ERROR", str(self._proxy_status["message"]))
            return
        if upstream_port == self._proxy_port and parsed.hostname in {
            "127.0.0.1", "localhost", "0.0.0.0", self._proxy_bind,
        }:
            self._proxy_status = {
                "running": False, "message": "反代端口不能与上游端口相同（会形成死循环）", "requests": 0,
            }
            return
        try:
            server = VirtualProxyServer((self._proxy_bind, self._proxy_port), self)
            thread = threading.Thread(
                target=server.serve_forever,
                name="MediaVirtualLibraryProxy", daemon=True,
            )
            self._proxy_server = server
            self._proxy_thread = thread
            thread.start()
            self._proxy_status = {
                "running": True,
                "message": (
                    f"8098输出模式就绪（内部注入 {self._proxy_bind}:{self._proxy_port}）"
                ),
                "requests": 0,
                "upstream": self._manual_url,
                "public_port": self.PUBLIC_GATEWAY_PORT,
            }
            self._record(
                "INFO",
                f"8098一级虚拟库链路已就绪：8098反代 -> 内部注入"
                f" {self._proxy_bind}:{self._proxy_port} -> {self._manual_url}",
            )
        except OSError as err:
            self._proxy_server = None
            self._proxy_thread = None
            self._proxy_status = {
                "running": False,
                "message": f"反代端口启动失败：{err}",
                "requests": 0,
            }
            self._record("ERROR", str(self._proxy_status["message"]))

    def _stop_proxy(self) -> None:
        server = getattr(self, "_proxy_server", None)
        thread = getattr(self, "_proxy_thread", None)
        self._proxy_server = None
        self._proxy_thread = None
        if server:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        status = getattr(self, "_proxy_status", {})
        self._proxy_status = {
            "running": False, "message": "反代已停止",
            "requests": int(status.get("requests") or 0),
        }

    def get_state(self) -> bool:
        return bool(self._enabled and (self._attribute_enabled or self._ranking_enabled))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not (self.get_state() and self._auto_sync and IntervalTrigger):
            return []
        return [{
            "id": "MediaArchiver.virtual_library_reconcile",
            "name": "媒体虚拟库定时校准",
            "trigger": IntervalTrigger(minutes=self._sync_interval),
            "func": self._scheduled_sync,
            "kwargs": {},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/test_connection", "endpoint": self.test_connection, "methods": ["POST"],
             "auth": "bear", "summary": "测试已保存的 Emby 302 连接"},
            {"path": "/rebuild", "endpoint": self.rebuild, "methods": ["POST"],
             "auth": "bear", "summary": "一键重建全部虚拟库"},
            {"path": "/cleanup_legacy_collections", "endpoint": self.cleanup_legacy_collections,
             "methods": ["POST"], "auth": "bear", "summary": "清空旧版插件合集成员"},
            {"path": "/status", "endpoint": self.status, "methods": ["GET"],
             "auth": "bear", "summary": "查询同步状态"},
        ]

    def rebuild(self) -> schemas.Response:
        return self._start_sync("rebuild")

    def cleanup_legacy_collections(self) -> schemas.Response:
        """只清理 v3.x 已记录的 BoxSet 关联，不删除任何媒体 Item/文件。"""
        if not self._run_lock.acquire(blocking=False):
            return schemas.Response(success=False, message="已有同步或清理任务运行中")
        try:
            recorded: Set[str] = set()
            states: List[Dict[str, Any]] = []
            for state in (self._state.get("servers") or {}).values():
                if not isinstance(state, dict):
                    continue
                states.append(state)
                for field in ("attribute_collections", "ranking_collections"):
                    recorded.update(
                        str(value) for value in (state.get(field) or {}).values() if str(value)
                    )
            if not recorded:
                return schemas.Response(
                    success=True, message="未找到本插件记录的旧合集，无需清理",
                    data={"collections": 0, "members": 0},
                )
            clients = self._create_clients()
            cleared_members = cleared_collections = 0
            failures: List[str] = []
            for client, server_name in clients:
                for collection_id in sorted(recorded):
                    try:
                        members = client.collection_members(collection_id)
                        if members:
                            client.remove_items(collection_id, members)
                            cleared_members += len(members)
                        cleared_collections += 1
                    except EmbyHttpError as err:
                        if err.status != 404:
                            failures.append(f"{server_name}/{collection_id}：{err}")
                    except Exception as err:
                        failures.append(f"{server_name}/{collection_id}：{err}")
            if failures and not cleared_collections:
                raise RuntimeError("；".join(failures[:5]))
            for state in states:
                state["attribute_collections"] = {}
                state["ranking_collections"] = {}
                state["legacy_collections_cleaned"] = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
            self._save_state()
            message = (
                f"旧合集成员已清理：{cleared_collections} 个 BoxSet，"
                f"{cleared_members} 条逻辑关联；原媒体与文件未删除"
            )
            if failures:
                message += f"；{len(failures)} 项失败"
            self._record("WARNING" if failures else "INFO", message)
            return schemas.Response(
                success=not failures, message=message,
                data={
                    "collections": cleared_collections, "members": cleared_members,
                    "failures": failures,
                },
            )
        except Exception as err:
            message = f"清理旧合集失败：{err}"
            self._record("ERROR", message)
            return schemas.Response(success=False, message=message)
        finally:
            self._run_lock.release()

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
            "last_sync": self._state.get("last_sync") or "",
            "selected_rankings": len(self._selected_rankings),
            "active_servers": active_servers,
            "source_status": self._state.get("source_status") or {},
            "proxy": dict(self._proxy_status),
            "virtual_views": len(self._virtual_views),
        })
        return schemas.Response(
            success=data.get("state") != "failed", message=str(data.get("message") or ""), data=data
        )

    # ------------------------------------------------------------------
    # Emby 首页虚拟库反向代理
    # ------------------------------------------------------------------

    _HOP_HEADERS = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade",
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

    def _proxy_dispatch(self, handler: VirtualProxyHandler) -> None:
        self._proxy_status["requests"] = int(self._proxy_status.get("requests") or 0) + 1
        parsed = urllib.parse.urlsplit(handler.path)
        route = self._route_path(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if route.rstrip("/") == "/__mediaarchiver__/health":
            self._send_json(handler, 200, {
                "ok": True, "proxy": dict(self._proxy_status),
                "views": len(self._virtual_views),
                "items": len(self._proxy_item_index),
            })
            return

        if str(handler.headers.get("Upgrade") or "").casefold() == "websocket":
            self._proxy_websocket(handler)
            return

        views_match = re.fullmatch(r"/Users/[^/]+/Views/?", route, flags=re.I)
        items_match = re.fullmatch(r"/Users/[^/]+/Items/?", route, flags=re.I)
        latest_match = re.fullmatch(r"/Users/[^/]+/Items/Latest/?", route, flags=re.I)
        detail_match = re.fullmatch(
            r"/(?:Users/[^/]+/)?Items/([0-9a-f]{32})/?", route, flags=re.I
        )
        image_match = re.fullmatch(
            r"/Items/([0-9a-f]{32})/Images/Primary(?:/\d+)?/?", route, flags=re.I
        )

        with self._proxy_lock:
            view = dict(self._virtual_views.get(
                self._query_value(query, "ParentId")
            ) or {})

        if detail_match:
            with self._proxy_lock:
                detail_view = dict(self._virtual_views.get(detail_match.group(1)) or {})
            if detail_view:
                self._send_json(handler, 200, self._synthetic_view(detail_view))
                return

        if image_match:
            with self._proxy_lock:
                image_view = dict(self._virtual_views.get(image_match.group(1)) or {})
            if image_view:
                self._send_virtual_cover(handler, image_view)
                return

        if (items_match or latest_match) and view:
            self._serve_virtual_items(handler, view, query, latest=bool(latest_match))
            return

        if views_match:
            self._proxy_forward(handler, transform=self._inject_views, force_identity=True)
            return

        self._proxy_forward(handler)

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
            "CollectionType": str(view.get("collection_type") or "movies"),
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

    def _serve_virtual_items(
        self,
        handler: VirtualProxyHandler,
        view: Mapping[str, Any],
        query: Mapping[str, List[str]],
        latest: bool,
    ) -> None:
        selected, total = self._select_view_item_ids(view, query, latest)
        if not selected:
            self._send_json(
                handler, 200,
                [] if latest else {"Items": [], "TotalRecordCount": total, "StartIndex": 0},
            )
            return

        pairs = urllib.parse.parse_qsl(
            urllib.parse.urlsplit(handler.path).query, keep_blank_values=True
        )
        discarded = {"parentid", "startindex", "limit", "ids", "sortby", "sortorder"}
        pairs = [(key, value) for key, value in pairs if key.casefold() not in discarded]
        pairs.extend([
            ("Ids", ",".join(selected)),
            ("Recursive", "true"),
            ("StartIndex", "0"),
            ("Limit", str(len(selected))),
        ])
        route = self._route_path(urllib.parse.urlsplit(handler.path).path)
        raw_path = urllib.parse.urlsplit(handler.path).path
        prefix = "/emby" if raw_path.casefold().startswith("/emby/") else ""
        user_match = re.match(r"/Users/([^/]+)/", route, flags=re.I)
        upstream_route = (
            f"{prefix}/Users/{user_match.group(1)}/Items"
            if user_match else f"{prefix}/Items"
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

        self._proxy_forward(handler, target_path=target, transform=transform, force_identity=True)

    def _select_view_item_ids(
        self,
        view: Mapping[str, Any],
        query: Mapping[str, List[str]],
        latest: bool,
    ) -> Tuple[List[str], int]:
        with self._proxy_lock:
            index = dict(self._proxy_item_index)
        ids = list(dict.fromkeys(str(x) for x in (view.get("item_ids") or []) if str(x)))
        include_types = {
            value.strip().casefold()
            for value in self._query_value(query, "IncludeItemTypes").split(",") if value.strip()
        }
        search_term = self._query_value(query, "SearchTerm").strip().casefold()
        years = {
            self._number(value) for value in self._query_value(query, "Years").split(",") if value
        }

        def accepted(item_id: str) -> bool:
            item = index.get(item_id) or {}
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
        start = 0 if latest else self._int_query(query, "StartIndex", 0, 0, 1_000_000)
        default_limit = 16 if latest else 50
        limit = self._int_query(query, "Limit", default_limit, 1, 200)
        return ids[start:start + limit], total

    def _int_query(
        self, query: Mapping[str, List[str]], name: str, default: int,
        minimum: int, maximum: int,
    ) -> int:
        return self._bounded_int(self._query_value(query, name, str(default)), default, minimum, maximum)

    @staticmethod
    def _send_json(handler: VirtualProxyHandler, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(payload)

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

    def _render_cover_svg(self, view: Mapping[str, Any]) -> bytes:
        theme = self._cover_theme(view)
        logo = html.escape(str(theme.get("logo") or "VIRTUAL"))
        name = html.escape(str(view.get("name") or "虚拟媒体库"))
        count = len(view.get("item_ids") or [])
        logo_size = 142 if len(logo) <= 4 else 92 if len(logo) <= 10 else 64
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{theme['bg']}"/><stop offset="1" stop-color="{theme['bg2']}"/></linearGradient></defs>
<rect width="960" height="540" fill="url(#g)"/><circle cx="890" cy="20" r="230" fill="{theme['accent']}" opacity=".14"/>
<rect x="54" y="62" width="16" height="416" rx="8" fill="{theme['accent']}"/>
<text x="108" y="245" fill="{theme['accent']}" font-family="Arial,Noto Sans,sans-serif" font-size="{logo_size}" font-weight="800">{logo}</text>
<text x="110" y="356" fill="{theme['fg']}" font-family="Noto Sans CJK SC,Arial,sans-serif" font-size="48" font-weight="700">{name}</text>
<rect x="108" y="397" width="510" height="58" rx="29" fill="none" stroke="{theme['accent']}" stroke-width="2"/>
<text x="135" y="435" fill="{theme['fg']}" font-family="Arial,sans-serif" font-size="27">{count} ITEMS   •   LIVE VIRTUAL LIBRARY</text>
</svg>'''
        return svg.encode("utf-8")

    def _send_virtual_cover(
        self, handler: VirtualProxyHandler, view: Mapping[str, Any],
    ) -> None:
        tag = str(
            view.get("cover_tag")
            or self._cover_tag(str(view.get("key") or view.get("name")), view.get("item_ids") or [])
        )
        etag = f'"{tag}"'
        if str(handler.headers.get("If-None-Match") or "").strip() == etag:
            handler.send_response(304)
            handler.send_header("ETag", etag)
            handler.end_headers()
            return
        with self._proxy_lock:
            cached = self._cover_cache.get(tag)
        if cached:
            mime_type, payload = cached
        else:
            mime_type, payload = "image/png", self._render_cover_png(view)
            with self._proxy_lock:
                if len(self._cover_cache) >= 96:
                    self._cover_cache.pop(next(iter(self._cover_cache)), None)
                self._cover_cache[tag] = (mime_type, payload)
        handler.send_response(200)
        handler.send_header("Content-Type", mime_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "public, max-age=86400")
        handler.send_header("ETag", etag)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(payload)

    def _upstream_path(self, incoming: str) -> str:
        parsed_upstream = urllib.parse.urlsplit(self._manual_url)
        parsed_incoming = urllib.parse.urlsplit(incoming)
        base_path = parsed_upstream.path.rstrip("/")
        path = parsed_incoming.path or "/"
        if base_path and not (
            path.casefold() == base_path.casefold()
            or path.casefold().startswith(base_path.casefold() + "/")
        ):
            path = base_path + (path if path.startswith("/") else "/" + path)
        if parsed_incoming.query:
            path += "?" + parsed_incoming.query
        return path

    def _upstream_connection(self) -> Tuple[http.client.HTTPConnection, urllib.parse.SplitResult]:
        target = urllib.parse.urlsplit(self._manual_url)
        port = target.port or (443 if target.scheme == "https" else 80)
        if target.scheme == "https":
            return http.client.HTTPSConnection(
                target.hostname, port, timeout=self._timeout,
                context=ssl.create_default_context(),
            ), target
        return http.client.HTTPConnection(target.hostname, port, timeout=self._timeout), target

    def _proxy_forward(
        self,
        handler: VirtualProxyHandler,
        target_path: str = "",
        transform: Optional[Callable[[int, List[Tuple[str, str]], bytes], Tuple[int, List[Tuple[str, str]], bytes]]] = None,
        force_identity: bool = False,
    ) -> None:
        connection, target = self._upstream_connection()
        length = self._number(handler.headers.get("Content-Length"))
        body = handler.rfile.read(length) if length > 0 else None
        headers: Dict[str, str] = {}
        for key, value in handler.headers.items():
            folded = key.casefold()
            if folded in self._HOP_HEADERS or folded in {"host", "content-length"}:
                continue
            headers[key] = value
        headers["Host"] = target.netloc
        headers["X-Forwarded-For"] = handler.client_address[0]
        headers["X-Forwarded-Proto"] = "https" if target.scheme == "https" else "http"
        if force_identity:
            headers["Accept-Encoding"] = "identity"
        if body is not None:
            headers["Content-Length"] = str(len(body))
        request_target = self._upstream_path(target_path or handler.path)
        try:
            connection.request(handler.command, request_target, body=body, headers=headers)
            response = connection.getresponse()
            response_headers = list(response.getheaders())
            if transform:
                raw = response.read()
                status, response_headers, raw = transform(
                    int(response.status), response_headers, raw
                )
                handler.send_response(status)
                for key, value in response_headers:
                    if key.casefold() in self._HOP_HEADERS or key.casefold() in {
                        "content-length", "content-encoding",
                    }:
                        continue
                    handler.send_header(key, value)
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                if handler.command != "HEAD":
                    handler.wfile.write(raw)
                return

            handler.send_response(response.status, response.reason)
            has_length = False
            for key, value in response_headers:
                folded = key.casefold()
                if folded in self._HOP_HEADERS:
                    continue
                if folded == "content-length":
                    has_length = True
                handler.send_header(key, value)
            if not has_length:
                handler.send_header("Connection", "close")
                handler.close_connection = True
            handler.end_headers()
            if handler.command != "HEAD":
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        finally:
            connection.close()

    def _proxy_websocket(self, handler: VirtualProxyHandler) -> None:
        target = urllib.parse.urlsplit(self._manual_url)
        port = target.port or (443 if target.scheme == "https" else 80)
        upstream: socket.socket = socket.create_connection(
            (str(target.hostname), port), timeout=self._timeout
        )
        if target.scheme == "https":
            upstream = ssl.create_default_context().wrap_socket(
                upstream, server_hostname=str(target.hostname)
            )
        request_lines = [f"{handler.command} {self._upstream_path(handler.path)} HTTP/1.1"]
        for key, value in handler.headers.items():
            if key.casefold() == "host":
                continue
            request_lines.append(f"{key}: {value}")
        request_lines.extend([f"Host: {target.netloc}", "", ""])
        upstream.sendall("\r\n".join(request_lines).encode("iso-8859-1"))
        response_head = b""
        while b"\r\n\r\n" not in response_head and len(response_head) < 64 * 1024:
            block = upstream.recv(4096)
            if not block:
                break
            response_head += block
        handler.connection.sendall(response_head)
        if not response_head.startswith(b"HTTP/1.1 101") and not response_head.startswith(b"HTTP/1.0 101"):
            upstream.close()
            handler.close_connection = True
            return
        handler.connection.setblocking(False)
        upstream.setblocking(False)
        try:
            while not self._stopping:
                readable, _, exceptional = select.select(
                    [handler.connection, upstream], [], [handler.connection, upstream], 1.0
                )
                if exceptional:
                    break
                for source in readable:
                    try:
                        data = source.recv(64 * 1024)
                    except (BlockingIOError, ssl.SSLWantReadError):
                        continue
                    if not data:
                        return
                    destination = upstream if source is handler.connection else handler.connection
                    destination.sendall(data)
        finally:
            try:
                upstream.close()
            except Exception:
                pass
            handler.close_connection = True

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
        """精简配置：只保留原生 Emby、专区选择和必要维护选项。"""
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
            connection_text = "尚未测试。填写地址和 API Key 后先保存，再点击“测试连接”。"
        proxy_type = "success" if self._proxy_status.get("running") else "warning"
        proxy_text = (
            f"{self._proxy_status.get('message') or '内部注入服务未启动'}；"
            f"客户端地址固定为 http://NAS-IP:{self.PUBLIC_GATEWAY_PORT}"
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
                     "text": "填写原生 Emby 内网地址和 API Key，勾选专区；客户端始终继续使用 8098。"},
                    {"component": "VAlert", "props": {
                        "type": "success", "variant": "tonal", "class": "mb-4",
                        "title": "固定 8098 单入口",
                        "text": "客户端仍访问 8098；现有302反代把普通Emby请求交给本插件的内部注入通道，播放仍由原8098返回302。内部通道不对公网开放。",
                    }},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "manual_url", "label": "原生 Emby 内网地址",
                                "placeholder": "http://emby:8096", "prepend-inner-icon": "mdi-server-network",
                                "hint": "填写8098背后的原生Emby地址，例如 http://emby:8096；不要填写8098，防止请求循环。",
                                "persistent-hint": True, "clearable": True,
                            }},
                        ]},
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "manual_api_key", "label": "Emby API Key", "type": "password",
                                "placeholder": "在 Emby 控制台 → API 密钥中新建",
                                "prepend-inner-icon": "mdi-key-variant", "autocomplete": "new-password",
                                "hint": "密码框不会明文展示 Key；请勿把 Key 写进 GitHub。",
                                "persistent-hint": True,
                            }},
                        ]},
                    ]},
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
                        }, "text": "测试连接（先保存）", "events": {"click": {
                            "api": "plugin/MediaArchiver/test_connection", "method": "post",
                            "params": {"token": token},
                        }}},
                        {"component": "VBtn", "props": {
                            "color": "primary", "variant": "flat", "prepend-icon": "mdi-refresh",
                        }, "text": "一键重建", "events": {"click": {
                            "api": "plugin/MediaArchiver/rebuild", "method": "post",
                            "params": {"token": token},
                        }}},
                        {"component": "VBtn", "props": {
                            "color": "warning", "variant": "outlined",
                            "prepend-icon": "mdi-broom",
                        }, "text": "清空 v3.x 旧合集成员", "events": {"click": {
                            "api": "plugin/MediaArchiver/cleanup_legacy_collections",
                            "method": "post", "params": {"token": token},
                        }}},
                    ]},
                ]},
            ]},
            {"component": "VRow", "props": {"class": "mb-3"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
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
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
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
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                        {"component": "VCardText", "content": [
                            {"component": "VSwitch", "props": {
                                "model": "auto_sync", "label": "自动维护新增与删除",
                                "color": "primary", "hide-details": True,
                            }},
                            {"component": "div", "props": {"class": "text-caption mt-2"},
                             "text": "推荐保持开启，自动做增量同步和定时校准。"},
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
                            "text": "只有直连地址无法使用、多服务器或外部榜单源需要覆盖时，才修改这里。",
                        }},
                        {"component": "VSwitch", "props": {
                            "model": "use_moviepilot_servers",
                            "label": "同时扫描 MoviePilot 已配置的 Emby（不会由本反代展示）",
                            "hide-details": True,
                        }},
                        {"component": "VSelect", "props": {
                            "multiple": True, "chips": True, "clearable": True,
                            "model": "emby_servers", "label": "MoviePilot Emby 服务器（可多选）",
                            "items": self._server_options(),
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
                        {"component": "VSwitch", "props": {
                            "model": "rebuild_once", "label": "旧版界面兼容：开启后保存即执行一次重建",
                            "hide-details": True,
                        }},
                    ]},
                ]},
            ]},
            {"component": "VAlert", "props": {
                "type": "success", "variant": "tonal", "class": "mt-5",
                "title": "最简单的使用方法",
                "text": "填写原生Emby地址和Key → 保存 → 一键重建 → 让现有8098反代的Emby上游指向内部注入地址 → 客户端继续使用NAS-IP:8098。",
            }},
        ]}
        defaults: Dict[str, Any] = {
            "enabled": True, "virtual_enabled": True, "ranking_enabled": False,
            "auto_sync": True,
            "use_moviepilot_servers": False,
            "emby_servers": [], "emby_server": "", "manual_url": "", "manual_api_key": "",
            "sync_interval": 60, "timeout": 30, "tmdb_api_key": "", "tmdb_domain": "",
            "ranking_regions": "US,GB,JP,KR,HK,TW", "ranking_limit": 100,
            "ranking_language": "zh-CN", "ranking_feed_url": "", "ranking_feed_token": "",
            "rebuild_once": False,
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
            connection_test.get("message") or "尚未测试连接，请在配置页保存地址和 Key 后测试。"
        )
        token = self._secret(getattr(settings, "API_TOKEN", ""))
        return [{"component": "div", "props": {"class": "pa-3"}, "content": [
            {"component": "VAlert", "props": {
                "type": state_type, "variant": "tonal", "class": "mb-3",
                "title": state_label,
                "text": f"{self._runtime.get('message') or '保存配置后点击一键重建。'}\n最后成功同步：{self._state.get('last_sync') or '暂无'}",
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
                    f"{self._proxy_status.get('message') or '内部注入服务未启动'}；"
                    f"Emby 客户端固定连接 http://NAS-IP:{self.PUBLIC_GATEWAY_PORT}"
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
                {"component": "VBtn", "props": {
                    "color": "warning", "variant": "outlined", "prepend-icon": "mdi-broom",
                }, "text": "清空 v3.x 旧合集成员", "events": {"click": {
                    "api": "plugin/MediaArchiver/cleanup_legacy_collections", "method": "post",
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

    def _scheduled_sync(self) -> None:
        if self.get_state() and not self._stopping:
            response = self._start_sync("scheduled")
            if not getattr(response, "success", False):
                logger.debug("[媒体虚拟库] 定时同步未启动：%s", getattr(response, "message", ""))

    def _sync_worker(self, mode: str) -> None:
        started = time.monotonic()
        self._runtime = {
            "state": "running", "message": "正在连接 Emby 并计算差异……",
            "mode": mode, "stats": {},
        }
        self._record("INFO", f"开始{'增量校准' if mode == 'incremental' else '全量重建'}")
        try:
            clients = self._create_clients()
            servers_state = self._state.setdefault("servers", {})
            for value in servers_state.values():
                if isinstance(value, dict):
                    value["active"] = False

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
        items = client.library_items()
        # Emby 少数版本会因多库查询返回重复 Item，按原 ItemId 去重。
        item_map = {str(item.get("Id")): item for item in items if item.get("Id")}
        items = list(item_map.values())
        index = LibraryIndex(items)
        identity = f"{server_name}|{client.api_root}"
        servers = self._state.setdefault("servers", {})
        state = servers.setdefault(identity, {})
        state.update({
            "name": server_name, "api_root": client.api_root,
            "active": True, "last_error": "",
        })
        # v3.x 的映射仅供用户手动点击“清理旧合集”，v4 不再写入。
        state.setdefault("attribute_collections", {})
        state.setdefault("ranking_collections", {})
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
        if self._attribute_enabled:
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
        if self._is_proxy_origin(client.api_root):
            with self._proxy_lock:
                self._virtual_views = {
                    str(value["id"]): dict(value) for value in next_views.values()
                    if value.get("id")
                }
                self._proxy_item_index = {key: dict(value) for key, value in item_map.items()}
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
        if not self._manual_url:
            return False
        try:
            return urllib.parse.urlsplit(api_root).netloc.casefold() == urllib.parse.urlsplit(
                self._manual_url
            ).netloc.casefold()
        except Exception:
            return False

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

        all_text = " ".join(self._all_scalar_values(item)).casefold()
        if "remux" in all_text:
            matched.add("remux")

        dimensions: List[Tuple[int, int]] = []
        candidates: List[Mapping[str, Any]] = [item]
        candidates.extend(x for x in sources if isinstance(x, Mapping))
        candidates.extend(streams)
        for value in candidates:
            width = self._number(value.get("Width") or value.get("width"))
            height = self._number(value.get("Height") or value.get("height"))
            dimensions.append((width, height))
        if any(width >= 3840 or height >= 2160 for width, height in dimensions) or re.search(
            r"(?<!\w)(?:2160p|4k|uhd)(?!\w)", all_text, flags=re.I
        ):
            matched.add("4k")

        video_streams = [
            stream for stream in streams
            if str(stream.get("Type") or stream.get("type") or "Video").casefold() == "video"
        ]
        video_text = " ".join(
            " ".join(self._all_scalar_values(stream)) for stream in video_streams
        ).casefold()
        dv_pattern = r"dolby[ ._-]*vision|\bdovi\b|\bdv(?:he|av|\d|\b)"
        hdr_pattern = r"\bhdr10\+?\b|\bhdr\b|\bhlg\b|smpte2084|\bpq\b|dolby[ ._-]*vision|\bdovi\b"
        if re.search(dv_pattern, video_text or all_text, flags=re.I) or re.search(
            dv_pattern, all_text, flags=re.I
        ):
            matched.add("dolby_vision")
        if re.search(hdr_pattern, video_text or all_text, flags=re.I) or re.search(
            hdr_pattern, all_text, flags=re.I
        ):
            matched.add("hdr")

        audio_streams = [
            stream for stream in streams
            if str(stream.get("Type") or stream.get("type") or "").casefold() == "audio"
        ]
        audio_text = " ".join(
            " ".join(self._all_scalar_values(stream)) for stream in audio_streams
        ).casefold()
        atmos_pattern = r"\batmos\b|\bjoc\b|e-?ac-?3[^\n]{0,30}joc|truehd[^\n]{0,30}atmos"
        if re.search(atmos_pattern, audio_text, flags=re.I) or re.search(
            atmos_pattern, all_text, flags=re.I
        ):
            matched.add("atmos")
        return matched

    @classmethod
    def _all_scalar_values(cls, value: Any, depth: int = 0) -> List[str]:
        if depth > 8 or value is None:
            return []
        if isinstance(value, Mapping):
            result: List[str] = []
            for key, child in value.items():
                result.append(str(key))
                result.extend(cls._all_scalar_values(child, depth + 1))
            return result
        if isinstance(value, (list, tuple, set)):
            result = []
            for child in value:
                result.extend(cls._all_scalar_values(child, depth + 1))
            return result
        if isinstance(value, (str, int, float, bool)):
            return [str(value)]
        return []

    @staticmethod
    def _number(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            match = re.search(r"\d+", str(value or ""))
            return int(match.group()) if match else 0

    def _create_clients(self) -> List[Tuple[EmbyClient, str]]:
        """直连地址优先；MoviePilot 媒体服务器仅作无直连配置时的兼容后备。"""
        configs: Dict[str, Any] = {}
        services: Dict[str, Any] = {}
        errors: List[str] = []
        clients: List[Tuple[EmbyClient, str]] = []
        seen_roots: Set[str] = set()
        manual_configured = bool(self._manual_url or self._manual_key)

        if manual_configured:
            if not (self._manual_url and self._manual_key):
                errors.append("Emby 302 地址与 API Key 必须同时填写")
            else:
                try:
                    client = EmbyClient(self._manual_url, self._manual_key, self._timeout)
                    seen_roots.add(client.api_root)
                    clients.append((client, "Emby 302"))
                except Exception as err:
                    errors.append(f"Emby 302：{err}")
            # 普通模式下直连是唯一目标，配置错误必须明确报错，不能被其它服务器掩盖。
            if not self._use_moviepilot_servers:
                if clients:
                    return clients
                detail = "；".join(errors) or "直连配置无效"
                raise RuntimeError("没有可用的 Emby 连接；" + detail)

        # 没有填写直连信息时，自动回退旧版 MoviePilot 配置；也可在高级设置中主动并用。
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
                        "请在插件首页填写‘原生 Emby 内网地址’与 API Key"
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
        self._stop_proxy()
        with self._event_lock:
            self._pending_ids.clear()
