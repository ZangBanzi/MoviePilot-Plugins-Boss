"""MoviePilot v2：媒体属性专区 + TgtoDrive 风格榜单虚拟库。

只维护 Emby Collection/BoxSet 与原媒体 ItemId 的逻辑关联；不访问、不移动、
不复制、不重命名任何真实媒体文件，也不会改写 MediaSource.Path。
"""
from __future__ import annotations

import html
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
        "ProviderIds,ProductionYear,PremiereDate,OriginalTitle,SortName,Genres,Studios"
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
        self._collection_cache: Optional[Dict[str, str]] = None

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
                "User-Agent": "MoviePilot-MediaVirtualLibrary/3.2",
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

    def find_collection(self, name: str) -> Optional[str]:
        if self._collection_cache is None:
            self._collection_cache = {}
            for item in self._paged_items(
                Recursive=True, IncludeItemTypes="BoxSet", Fields="Path"
            ):
                if item.get("Id") and item.get("Name"):
                    self._collection_cache[str(item["Name"]).strip().casefold()] = str(item["Id"])
        return self._collection_cache.get(name.strip().casefold())

    def ensure_collection(self, name: str) -> str:
        existing = self.find_collection(name)
        if existing:
            return existing
        payload = self.request_json(
            "POST", "/Collections", {"Name": name, "IsLocked": False},
            expected=(200, 201, 204),
        )
        collection_id = payload.get("Id") or (payload.get("Collection") or {}).get("Id")
        if not collection_id:
            self._collection_cache = None
            collection_id = self.find_collection(name)
        if not collection_id:
            raise RuntimeError(f"Emby 已响应创建，但未能取得 Collection：{name}")
        if self._collection_cache is not None:
            self._collection_cache[name.strip().casefold()] = str(collection_id)
        return str(collection_id)

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

    def add_items(self, collection_id: str, item_ids: Iterable[str]) -> None:
        for batch in self._chunks(sorted(set(item_ids))):
            self.request_json(
                "POST",
                f"/Collections/{urllib.parse.quote(collection_id, safe='')}/Items",
                {"Ids": ",".join(batch)}, expected=(200, 204),
            )

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
    任何来源失败都会返回 ``ok=False``，同步层会保留旧 BoxSet，不会误清空。
    """

    PLATFORM_PROVIDERS: Dict[str, Tuple[str, ...]] = {
        "netflix": ("Netflix",),
        "hbo": ("Max", "HBO Max", "HBO"),
        "apple_tv": ("Apple TV Plus", "Apple TV+"),
        "disney_plus": ("Disney Plus", "Disney+"),
        "crunchyroll": ("Crunchyroll",),
        "amazon_prime": ("Amazon Prime Video",),
        "amazon": ("Amazon Video",),
        "hulu": ("Hulu",),
        "tencent": ("Tencent Video", "WeTV"),
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
            "User-Agent": "Mozilla/5.0 MoviePilot-MediaVirtualLibrary/3.2",
        }
        merged.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=merged, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            detail = err.read(240).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {err.code} {detail}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(str(err.reason)) from err

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
        for exact in wanted:
            for provider in providers:
                if str(provider.get("provider_name") or "").casefold() == exact.casefold():
                    found = int(provider["provider_id"])
                    break
            if found is not None:
                break
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
            headers={"User-Agent": "MoviePilot-MediaVirtualLibrary/3.2 (private use)"},
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
    """媒体属性专区与独立榜单虚拟库。"""

    plugin_name = "媒体虚拟库"
    plugin_desc = "按媒体属性和热门榜单维护 Emby 原生 BoxSet，原 ItemId 与302播放链路保持不变。"
    plugin_icon = "folder-move.svg"
    plugin_version = "3.2.0"
    plugin_author = "Boss"
    author_url = "https://github.com/ZangBanzi"
    plugin_config_prefix = "mediaarchiver_"
    plugin_order = 50
    auth_level = 1

    ATTRIBUTE_RULES: Dict[str, Dict[str, str]] = {
        "remux": {"name": "Remux专区", "icon": "mdi-disc", "hint": "路径、文件名或媒体源信息包含 Remux"},
        "4k": {"name": "4K专区", "icon": "mdi-video-4k-box", "hint": "视频宽度≥3840或高度≥2160"},
        "dolby_vision": {"name": "Dolby Vision专区", "icon": "mdi-eye-circle", "hint": "视频流 DV/Dolby Vision 信息"},
        "hdr": {"name": "HDR专区", "icon": "mdi-brightness-7", "hint": "HDR10/HDR10+/HLG/PQ/DV"},
        "atmos": {"name": "Atmos专区", "icon": "mdi-surround-sound", "hint": "音频流 Atmos/JOC 信息"},
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
        # 某些前端会把密码框回传为星号；这不是新 Key，沿用内存中的旧值。
        if incoming_key and set(incoming_key) == {"*"}:
            incoming_key = self._manual_key
        self._manual_key = incoming_key
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
            "last_sync": self._state.get("last_sync") or "",
            "selected_rankings": len(self._selected_rankings),
            "active_servers": active_servers,
            "source_status": self._state.get("source_status") or {},
        })
        return schemas.Response(
            success=data.get("state") != "failed", message=str(data.get("message") or ""), data=data
        )

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
        """TgtoDrive 风格的直连配置：普通用户只需填写地址、Key 和功能开关。"""
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
            {"component": "VCard", "props": {"variant": "outlined", "class": "mb-4"}, "content": [
                {"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-overline text-primary"},
                     "text": "EMBY VIRTUAL LIBRARY"},
                    {"component": "div", "props": {"class": "text-h4 font-weight-bold mb-2"},
                     "text": "Emby 媒体虚拟库"},
                    {"component": "div", "props": {"class": "text-body-1 mb-4"},
                     "text": "填写你的 Emby 302 服务器地址和 API Key，勾选需要的虚拟库即可。"},
                    {"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal", "class": "mb-4",
                        "text": "只创建 Emby 原生 BoxSet，不复制文件、不改路径；播放仍使用原 ItemId、MediaSource 与 302 直链。",
                    }},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "manual_url", "label": "Emby 302 服务器地址",
                                "placeholder": "http://emby:8096", "prepend-inner-icon": "mdi-server-network",
                                "hint": "填写打开 Emby 的根地址，不是某个影片的 302 播放链接。",
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
                             "text": "把勾选的平台榜单投射成独立 Emby BoxSet。"},
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
             "text": "先勾选上方“启用榜单虚拟库”，再选择需要的榜单；每一项都会成为独立 BoxSet。"},
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
                            "label": "同时使用 MoviePilot 已配置的 Emby 服务器（多服务器兼容）",
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
                "text": "填写地址和 Key → 勾选需要的功能 → 保存 → 测试连接 → 一键重建。",
            }},
        ]}
        defaults: Dict[str, Any] = {
            "enabled": True, "virtual_enabled": True, "ranking_enabled": False,
            "auto_sync": True, "use_moviepilot_servers": False,
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
        """对一台 Emby 做集合成员差集；从不删除 BoxSet 本身。"""
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
        attribute_collections = state.setdefault("attribute_collections", {})
        ranking_collections = state.setdefault("ranking_collections", {})
        attribute_counts: Dict[str, int] = {}
        ranking_counts: Dict[str, int] = {}
        added = removed = ranking_failed = 0

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
            existing_id = str(attribute_collections.get(key) or "")
            if enabled:
                collection_id, current = self._managed_collection(
                    client, attribute_collections, key, rule["name"], create=True
                )
                change = self._reconcile(
                    client, collection_id, desired_attributes[key], current=current
                )
                added += change["added"]
                removed += change["removed"]
                attribute_counts[key] = change["count"]
            elif existing_id:
                collection_id, current = self._managed_collection(
                    client, attribute_collections, key, rule["name"], create=False
                )
                if collection_id:
                    change = self._reconcile(client, collection_id, set(), current=current)
                    removed += change["removed"]
                attribute_counts[key] = 0
            else:
                attribute_counts[key] = 0

        active_rankings = self._selected_rankings if self._ranking_enabled else set()
        for key in sorted(active_rankings):
            result = ranking_results.get(key)
            name = RANK_META[key]["collection"]
            if not result or not result.ok:
                ranking_failed += 1
                existing_id = str(ranking_collections.get(key) or "")
                if existing_id:
                    collection_id, current = self._managed_collection(
                        client, ranking_collections, key, name, create=False
                    )
                    ranking_counts[key] = len(current) if collection_id else 0
                else:
                    ranking_counts[key] = 0
                # 失败时不创建、不清空，保留上次可用结果。
                continue
            wanted = index.match(result.entries)
            collection_id, current = self._managed_collection(
                client, ranking_collections, key, name, create=True
            )
            change = self._reconcile(client, collection_id, wanted, current=current)
            added += change["added"]
            removed += change["removed"]
            ranking_counts[key] = change["count"]

        # 取消勾选或关闭榜单功能时，只清空本插件记录的集合成员。
        for key in sorted(set(ranking_collections) - set(active_rankings)):
            name = RANK_META.get(key, {}).get("collection", key)
            collection_id, current = self._managed_collection(
                client, ranking_collections, key, name, create=False
            )
            if collection_id:
                change = self._reconcile(client, collection_id, set(), current=current)
                removed += change["removed"]
            ranking_counts[key] = 0

        state.update({
            "attribute_counts": attribute_counts,
            "ranking_counts": ranking_counts,
            "last_sync": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        attribute_hits = sum(attribute_counts.values())
        ranking_hits = sum(ranking_counts.values())
        self._record(
            "INFO",
            f"{server_name}：扫描 {len(items)} 项，属性命中 {attribute_hits}，"
            f"榜单命中 {ranking_hits}，新增 {added}，移除 {removed}",
        )
        return {
            "scanned": len(items), "added": added, "removed": removed,
            "attribute_hits": attribute_hits, "ranking_hits": ranking_hits,
            "ranking_failed": ranking_failed,
        }

    @staticmethod
    def _reconcile(
        client: EmbyClient,
        collection_id: str,
        wanted: Set[str],
        current: Optional[Set[str]] = None,
    ) -> Dict[str, int]:
        current = set(current if current is not None else client.collection_members(collection_id))
        wanted = {str(item_id) for item_id in wanted if item_id}
        to_add = wanted - current
        to_remove = current - wanted
        if to_add:
            client.add_items(collection_id, to_add)
        if to_remove:
            client.remove_items(collection_id, to_remove)
        return {"added": len(to_add), "removed": len(to_remove), "count": len(wanted)}

    @staticmethod
    def _managed_collection(
        client: EmbyClient,
        mapping: Dict[str, str],
        key: str,
        name: str,
        create: bool,
    ) -> Tuple[str, Set[str]]:
        collection_id = str(mapping.get(key) or "")
        if collection_id:
            try:
                return collection_id, client.collection_members(collection_id)
            except EmbyHttpError as err:
                if err.status != 404:
                    raise
                mapping.pop(key, None)
                client._collection_cache = None
            except KeyError:
                # 测试替身与某些封装用 KeyError 表示集合已删除。
                mapping.pop(key, None)
                client._collection_cache = None
        if not create:
            return "", set()
        collection_id = client.ensure_collection(name)
        mapping[key] = collection_id
        return collection_id, client.collection_members(collection_id)

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
                        "请在插件首页填写‘Emby 302 服务器地址’与 API Key"
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
        with self._event_lock:
            self._pending_ids.clear()
