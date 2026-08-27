"""MoviePilot v2 插件：Emby 媒体一键归档。

目录名必须为 ``mediaarchiver``（发布到插件仓库时），主类名/插件 ID 为
``MediaArchiver``。本插件只处理 Emby 已入库且其 Path 位于配置源目录中的实体文件。
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

from app import schemas
from app.log import logger
from app.plugins import _PluginBase

try:
    # MoviePilot v2 常用入口；不同 v2 小版本可能移动了该 Helper。
    from app.helper.mediaserver import MediaServerHelper
except ImportError:  # pragma: no cover - 为部分 v2 分支保留兼容
    MediaServerHelper = None  # type: ignore

try:
    # 使用 MoviePilot 已配置好的 TMDb 域名、密钥及代理，无需插件单独填写 API Key。
    from app.modules.themoviedb.tmdbapi import TmdbApi
    from app.schemas.types import MediaType
except ImportError:  # pragma: no cover - 兼容少数改动过模块路径的 v2 分支
    TmdbApi = None  # type: ignore
    MediaType = None  # type: ignore


class MediaArchiver(_PluginBase):
    """将 Emby 已入库媒体按规则归档到 115 挂载目录。"""

    plugin_name = "媒体一键归档"
    plugin_desc = "在线验证 Emby 剧集所属平台，并归档 Remux、平台剧集及伦理内容。"
    plugin_icon = "folder-move.svg"
    plugin_version = "1.2.0"
    plugin_author = "李明宇"
    author_url = ""
    plugin_config_prefix = "mediaarchiver_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _mount_root = Path("/mnt/115")
    _movie_root = Path("/mnt/115/媒体库/电影")
    _series_root = Path("/mnt/115/媒体库/剧集")

    ADULT_KEYWORDS = ("adult", "jav", "xxx", "伦理", "18+")
    SERIES_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("奈飞专区", ("netflix", "nf", "奈飞")),
        ("迪士尼专区", ("disney", "disney+", "d+", "迪士尼")),
        ("HBO其他专区", ("hbo",)),
        ("APTV专区", ("apple tv+", "atv+", "aptv", "苹果")),
    )
    ONLINE_ZONE_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("奈飞专区", ("netflix",)),
        ("迪士尼专区", ("disney",)),
        ("HBO其他专区", ("hbo",)),
        ("APTV专区", ("apple tv",)),
    )

    _EMPTY_STATS = {
        "emby_items": 0,
        "remux_copied": 0,
        "series_moved": 0,
        "adult_moved": 0,
        "online_verified": 0,
        "online_unverified": 0,
        "skipped": 0,
        "failed": 0,
        "cancelled": 0,
    }

    def init_plugin(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._ensure_runtime()
        self._enabled = bool(config.get("enabled", True))
        self._mount_root = Path(config.get("mount_root") or "/mnt/115")
        self._movie_root = Path(config.get("movie_root") or (self._mount_root / "媒体库/电影"))
        self._series_root = Path(config.get("series_root") or (self._mount_root / "媒体库/剧集"))
        self._online_verify = bool(config.get("online_verify", True))

    def _ensure_runtime(self) -> None:
        """建立实例级运行状态，避免插件分身共享同一把类锁。"""
        if not hasattr(self, "_run_lock"):
            self._run_lock = threading.Lock()
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.Lock()
        if not hasattr(self, "_stop_event"):
            self._stop_event = threading.Event()
        if not hasattr(self, "_worker"):
            self._worker: Optional[threading.Thread] = None
        if not hasattr(self, "_status"):
            self._status = "idle"
            self._status_message = "尚未执行归档"
            self._last_stats = dict(self._EMPTY_STATS)
        if not hasattr(self, "_tmdb_client"):
            self._tmdb_client = None
        if not hasattr(self, "_online_cache"):
            self._online_cache: Dict[str, Tuple[Optional[str], Optional[int], str]] = {}

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.run_archive,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "执行一键归档",
            },
            {
                "path": "/status",
                "endpoint": self.get_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询归档状态",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return ([{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VSwitch", "props": {
                            "model": "enabled", "label": "启用插件"
                        }}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                        {"component": "VSwitch", "props": {
                            "model": "online_verify", "label": "剧集必须通过 TMDb 在线验证"
                        }}]},
                ]},
                {"component": "VTextField", "props": {
                    "model": "mount_root", "label": "115 挂载根目录"
                }},
                {"component": "VTextField", "props": {
                    "model": "movie_root", "label": "电影源目录"
                }},
                {"component": "VTextField", "props": {
                    "model": "series_root", "label": "剧集源目录"
                }},
                {"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": "剧集优先按 TMDb 编号验证；没有编号时按剧名在线搜索。"
                }},
                {"component": "VAlert", "props": {
                    "type": "warning", "variant": "tonal",
                    "text": "平台剧集与伦理内容使用移动操作；首次运行前请先做好备份。"
                }},
            ],
        }], {
            "enabled": True,
            "mount_root": "/mnt/115",
            "movie_root": "/mnt/115/媒体库/电影",
            "series_root": "/mnt/115/媒体库/剧集",
            "online_verify": True,
        })

    def get_page(self) -> List[dict]:
        """数据页提供可工作的 API 按钮，并展示最近一次任务状态。"""
        self._ensure_runtime()
        with self._state_lock:
            status = self._status
            message = self._status_message
            stats = dict(self._last_stats)
        running = status == "running"
        alert_type = {
            "completed": "success",
            "failed": "error",
            "cancelled": "warning",
            "running": "info",
        }.get(status, "info")
        plugin_id = self.__class__.__name__
        summary = (f"Emby条目 {stats['emby_items']} · 在线确认 {stats['online_verified']} · "
                   f"未确认 {stats['online_unverified']} · Remux复制 {stats['remux_copied']} · "
                   f"剧集文件夹移动 {stats['series_moved']} · 伦理移动 {stats['adult_moved']} · "
                   f"跳过 {stats['skipped']} · 失败 {stats['failed']}")
        return [{
            "component": "div",
            "content": [
                {"component": "VAlert", "props": {
                    "type": alert_type, "variant": "tonal", "text": message,
                }},
                {"component": "VCard", "props": {"class": "mt-3"}, "content": [
                    {"component": "VCardText", "text": summary},
                    {"component": "VCardActions", "content": [
                        {"component": "VBtn", "props": {
                            "color": "primary", "variant": "flat",
                            "prepend-icon": "mdi-archive-arrow-down",
                            "disabled": running,
                        }, "text": "归档中…" if running else "一键归档", "events": {
                            "click": {"api": f"plugin/{plugin_id}/run", "method": "post"}
                        }},
                        {"component": "VBtn", "props": {
                            "variant": "tonal", "prepend-icon": "mdi-refresh",
                        }, "text": "刷新状态", "events": {
                            "click": {"api": f"plugin/{plugin_id}/status", "method": "get"}
                        }},
                    ]},
                ]},
            ],
        }]

    def stop_service(self) -> None:
        self._ensure_runtime()
        self._stop_event.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=2)
            if worker.is_alive():
                logger.warning("[媒体一键归档] 插件停止时任务仍在结束当前文件操作")

    def run_archive(self) -> schemas.Response:
        """启动后台归档，避免大文件复制导致前端 HTTP 请求超时。"""
        self._ensure_runtime()
        if not self._enabled:
            return schemas.Response(success=False, message="插件未启用")
        if not self._run_lock.acquire(blocking=False):
            return schemas.Response(success=False, message="归档任务正在执行，请勿重复点击")
        self._stop_event.clear()
        with self._state_lock:
            self._status = "running"
            self._status_message = "归档任务正在后台执行，请稍后刷新状态"
            self._last_stats = dict(self._EMPTY_STATS)
        try:
            self._worker = threading.Thread(
                target=self._archive_worker,
                name=f"{self.__class__.__name__}-worker",
                daemon=True,
            )
            self._worker.start()
            return schemas.Response(success=True, message="归档任务已启动，请稍后刷新状态")
        except Exception as err:
            self._run_lock.release()
            with self._state_lock:
                self._status = "failed"
                self._status_message = f"任务启动失败：{err}"
            logger.exception("[媒体一键归档] 任务启动失败：%s", err)
            return schemas.Response(success=False, message=f"任务启动失败：{err}")

    def get_status(self) -> schemas.Response:
        """返回任务状态；数据页利用该请求触发页面重新渲染。"""
        self._ensure_runtime()
        with self._state_lock:
            data = {
                "status": self._status,
                "message": self._status_message,
                "stats": dict(self._last_stats),
            }
        return schemas.Response(success=True, message=data["message"], data=data)

    def _archive_worker(self) -> None:
        try:
            stats = self._archive()
            if stats["cancelled"]:
                status = "cancelled"
                message = "归档任务已停止"
            else:
                status = "completed" if stats["failed"] == 0 else "failed"
                message = (f"归档完成：Remux复制 {stats['remux_copied']}，"
                           f"剧集移动 {stats['series_moved']}，伦理移动 {stats['adult_moved']}，"
                           f"跳过 {stats['skipped']}，失败 {stats['failed']}")
            with self._state_lock:
                self._status = status
                self._status_message = message
                self._last_stats = stats
            logger.info("[媒体一键归档] %s", message)
        except Exception as err:
            logger.exception("[媒体一键归档] 执行失败：%s", err)
            with self._state_lock:
                self._status = "failed"
                self._status_message = f"执行失败：{err}"
        finally:
            client = self._tmdb_client
            self._tmdb_client = None
            if client and callable(getattr(client, "close", None)):
                try:
                    client.close()
                except Exception as err:
                    logger.debug("[媒体一键归档] 关闭 TMDb 客户端失败：%s", err)
            self._run_lock.release()

    def _archive(self) -> Dict[str, int]:
        """生成电影文件任务和剧集文件夹任务，再统一执行。"""
        mount_root = self._mount_root
        movie_root = self._movie_root
        series_root = self._series_root
        self._validate_roots(mount_root, movie_root, series_root)
        items = list(self._get_emby_items())
        logger.info("[媒体一键归档] 从 Emby 获取到 %d 个电影/剧集条目", len(items))
        stats = dict(self._EMPTY_STATS)
        stats["emby_items"] = len(items)
        self._online_cache = {}
        if self._online_verify and (TmdbApi is None or MediaType is None):
            raise RuntimeError("当前 MoviePilot 版本找不到 TMDb 内部接口，无法在线验证")

        # 电影仍按文件处理。
        tasks: List[Tuple[str, Path, Path]] = []
        seen_sources = set()
        for item in items:
            if self._stop_event.is_set():
                stats["cancelled"] = 1
                return stats
            kind = str(item.get("Type") or item.get("type") or "").lower()
            if kind != "movie":
                continue
            source = Path(str(item.get("Path") or item.get("path") or ""))
            name = str(item.get("Name") or item.get("name") or source.name)
            haystack = f"{source} {name}".casefold()
            if not source.is_file():
                logger.warning("[媒体一键归档] 跳过不存在的电影文件：%s", source)
                stats["skipped"] += 1
                continue
            try:
                source_key = os.path.normcase(str(source.resolve(strict=True)))
                if source_key in seen_sources:
                    continue
                seen_sources.add(source_key)
                if self._contains_keywords(haystack, self.ADULT_KEYWORDS):
                    tasks.append(("adult", source,
                                  mount_root / "伦理专区" / self._safe_relative(source, movie_root)))
                elif "remux" in source.name.casefold():
                    tasks.append(("remux", source,
                                  mount_root / "电影Remux归档" / source.name))
                else:
                    stats["skipped"] += 1
            except ValueError as err:
                logger.error("[媒体一键归档] 拒绝处理越界路径 %s：%s", source, err)
                stats["failed"] += 1

        # 剧集按 Emby SeriesId/TMDb ID 聚合，最终移动整部剧集文件夹。
        for series in self._build_series_groups(items, series_root):
            if self._stop_event.is_set():
                stats["cancelled"] = 1
                return stats
            source = series["path"]
            haystack = series["haystack"]
            try:
                relative = self._safe_relative(source, series_root)
                if self._contains_keywords(haystack, self.ADULT_KEYWORDS):
                    tasks.append(("adult", source, mount_root / "伦理专区" / relative))
                    continue
                if self._online_verify:
                    zone, tmdb_id, evidence = self._verify_series_online(series)
                    if not zone:
                        stats["online_unverified"] += 1
                        stats["skipped"] += 1
                        logger.warning("[媒体一键归档] 在线未确认，保留原目录：%s（%s）",
                                       source, evidence)
                        continue
                    stats["online_verified"] += 1
                    logger.info("[媒体一键归档] 在线确认：%s，TMDb=%s，专区=%s，依据=%s",
                                series["name"], tmdb_id, zone, evidence)
                else:
                    zone = self._match_series_zone(haystack)
                    if not zone:
                        stats["skipped"] += 1
                        continue
                tasks.append(("series", source, mount_root / zone / relative))
            except ValueError as err:
                stats["failed"] += 1
                logger.error("[媒体一键归档] 拒绝处理越界剧集目录 %s：%s", source, err)

        for operation, source, requested_target in tasks:
            if self._stop_event.is_set():
                stats["cancelled"] = 1
                break
            try:
                requested_target.parent.mkdir(parents=True, exist_ok=True)
                target = self._unique_path(requested_target)
                if operation == "remux":
                    # 按需求使用 copy2：复制文件内容，并尽可能保留时间戳等元数据。
                    shutil.copy2(str(source), str(target))
                    stats["remux_copied"] += 1
                    logger.info("[媒体一键归档] 复制：%s -> %s", source, target)
                else:
                    # 剧集任务的 source 是整部剧集目录；成人电影任务仍可能是单文件。
                    shutil.move(str(source), str(target))
                    key = "adult_moved" if operation == "adult" else "series_moved"
                    stats[key] += 1
                    logger.info("[媒体一键归档] 移动：%s -> %s", source, target)
            except Exception as err:
                stats["failed"] += 1
                logger.exception("[媒体一键归档] 文件操作失败 %s -> %s：%s",
                                 source, requested_target, err)
        return stats

    @staticmethod
    def _build_series_groups(items: Iterable[Dict[str, Any]], series_root: Path) -> List[Dict[str, Any]]:
        """按 SeriesId 聚合 Episode，并确定需要整体移动的剧集根目录。"""
        groups: Dict[str, Dict[str, Any]] = {}
        series_by_id: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if str(item.get("Type") or "").casefold() != "series":
                continue
            series_id = str(item.get("Id") or "")
            server_name = str(item.get("_MediaServer") or "")
            path = Path(str(item.get("Path") or ""))
            if not series_id or not path.is_dir():
                continue
            group = {
                "id": series_id,
                "name": str(item.get("Name") or path.name),
                "path": path,
                "provider_ids": item.get("ProviderIds") or {},
                "year": item.get("ProductionYear"),
                "texts": [str(path), str(item.get("Name") or "")],
            }
            groups[f"id:{server_name}:{series_id}"] = group
            series_by_id[f"{server_name}:{series_id}"] = group

        for item in items:
            if str(item.get("Type") or "").casefold() != "episode":
                continue
            episode_path = Path(str(item.get("Path") or ""))
            if not episode_path.is_file():
                continue
            series_id = str(item.get("SeriesId") or "")
            server_name = str(item.get("_MediaServer") or "")
            group = series_by_id.get(f"{server_name}:{series_id}")
            if group is None:
                try:
                    relative = episode_path.resolve(strict=True).relative_to(series_root.resolve(strict=True))
                except ValueError:
                    continue
                if len(relative.parts) < 2:
                    continue
                folder = series_root / relative.parts[0]
                key = f"path:{os.path.normcase(str(folder.resolve(strict=True)))}"
                group = groups.setdefault(key, {
                    "id": series_id,
                    "name": str(item.get("SeriesName") or relative.parts[0]),
                    "path": folder,
                    "provider_ids": item.get("SeriesProviderIds") or {},
                    "year": item.get("SeriesProductionYear"),
                    "texts": [str(folder), str(item.get("SeriesName") or "")],
                })
            group["texts"].extend([str(episode_path), str(item.get("Name") or "")])

        result = []
        seen_paths = set()
        for group in groups.values():
            path = group["path"]
            if not path.is_dir():
                continue
            path_key = os.path.normcase(str(path.resolve(strict=True)))
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            group["haystack"] = " ".join(group.pop("texts")).casefold()
            result.append(group)
        return result

    def _verify_series_online(self, series: Dict[str, Any]) \
            -> Tuple[Optional[str], Optional[int], str]:
        """优先使用 TMDb ID；没有 ID 时按剧名精确搜索，再检查网络/制作公司。"""
        tmdb_id = self._provider_id(series.get("provider_ids") or {}, "tmdb")
        title = str(series.get("name") or "").strip()
        year = str(series.get("year") or "").strip()
        cache_key = f"tmdb:{tmdb_id}" if tmdb_id else f"title:{title.casefold()}:{year}"
        if cache_key in self._online_cache:
            return self._online_cache[cache_key]

        client = self._get_tmdb_client()
        if not tmdb_id:
            results = client.search_tvs(title=title, year=year) or []
            selected = self._select_title_result(title, results)
            if not selected or not selected.get("id"):
                result = (None, None, f"按剧名未找到唯一结果：{title}")
                self._online_cache[cache_key] = result
                return result
            tmdb_id = int(selected["id"])

        try:
            detail = client.get_info(mtype=MediaType.TV, tmdbid=int(tmdb_id)) or {}
        except Exception as err:
            result = (None, int(tmdb_id), f"TMDb 查询失败：{err}")
            self._online_cache[cache_key] = result
            return result
        names = []
        for key in ("networks", "production_companies"):
            values = detail.get(key) or []
            if isinstance(values, list):
                names.extend(str(value.get("name") or "") for value in values if isinstance(value, dict))
        evidence = " / ".join(name for name in names if name) or "TMDb 未提供发行网络"
        zone = self._match_online_zone(" ".join(names))
        result = (zone, int(tmdb_id), evidence)
        self._online_cache[cache_key] = result
        return result

    def _get_tmdb_client(self) -> Any:
        if self._tmdb_client is None:
            self._tmdb_client = TmdbApi()
        return self._tmdb_client

    @staticmethod
    def _provider_id(provider_ids: Dict[str, Any], name: str) -> Optional[int]:
        for key, value in provider_ids.items():
            if str(key).casefold() == name.casefold() and str(value).isdigit():
                return int(value)
        return None

    @staticmethod
    def _normalize_title(title: str) -> str:
        return "".join(char for char in str(title).casefold() if char.isalnum())

    @classmethod
    def _select_title_result(cls, title: str, results: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        results = list(results)
        wanted = cls._normalize_title(title)
        exact = [item for item in results
                 if wanted in {cls._normalize_title(item.get("name") or ""),
                               cls._normalize_title(item.get("original_name") or "")}]
        if len(exact) == 1:
            return exact[0]
        return results[0] if len(results) == 1 else None

    def _match_online_zone(self, text: str) -> Optional[str]:
        for zone, keywords in self.ONLINE_ZONE_RULES:
            if self._contains_keywords(text, keywords):
                return zone
            if zone == "HBO其他专区" and re.search(
                    r"(?<![a-z0-9])max(?![a-z0-9])", text.casefold()):
                return zone
        return None

    @staticmethod
    def _validate_roots(mount_root: Path, movie_root: Path, series_root: Path) -> None:
        for target in (mount_root, movie_root, series_root):
            if not target.is_absolute():
                raise ValueError(f"必须配置绝对路径：{target}")
            if not target.exists():
                # 目标归档目录可创建，但配置的挂载/源目录不存在通常意味着容器没映射，
                # 不能默默创建，否则会把文件写进容器本地层。
                raise FileNotFoundError(f"路径不存在，请检查 Docker 映射：{target}")
            if not target.is_dir():
                raise NotADirectoryError(str(target))
        for folder in ("电影Remux归档", "奈飞专区", "迪士尼专区",
                       "HBO其他专区", "APTV专区", "伦理专区"):
            (mount_root / folder).mkdir(parents=True, exist_ok=True)

    def _get_emby_items(self) -> Iterable[Dict[str, Any]]:
        """从全部已启用的 Emby 服务读取 Movie/Episode 文件条目。"""
        if MediaServerHelper is None:
            raise RuntimeError("当前 MoviePilot 版本找不到 MediaServerHelper")
        helper = MediaServerHelper()
        services = helper.get_services()
        if isinstance(services, dict):
            candidates = list(services.values())
        elif isinstance(services, (list, tuple, set)):
            candidates = list(services)
        else:
            candidates = [services]

        all_items: List[Dict[str, Any]] = []
        emby_count = 0
        for wrapper in candidates:
            server = getattr(wrapper, "instance", None) or wrapper
            identity = (f"{getattr(wrapper, 'type', '')} {type(wrapper).__name__} "
                        f"{type(server).__name__} {getattr(wrapper, 'name', '')}").casefold()
            if "emby" not in identity:
                continue
            emby_count += 1
            service_name = str(getattr(wrapper, "name", "Emby"))
            result = self._call_emby_items(server, service_name)
            for item in result:
                item["_MediaServer"] = service_name
            all_items.extend(self._expand_media_sources(result))
        if not emby_count:
            raise RuntimeError("未找到已启用的 Emby 媒体服务器")
        return all_items

    @staticmethod
    def _call_emby_items(server: Any, service_name: str) -> List[Dict[str, Any]]:
        """分页读取 Movie/Series/Episode，并把 Series 在线识别字段补到 Episode。"""
        method = getattr(server, "get_data", None)
        if not callable(method):
            raise RuntimeError(f"Emby 服务 {service_name} 不支持 get_data(url)")
        page_size = 500
        start_index = 0
        collected: List[Dict[str, Any]] = []
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": "Path,MediaSources,ProviderIds,SeriesId,SeriesName,ProductionYear",
                "StartIndex": start_index,
                "Limit": page_size,
            }
            url = ("[HOST]emby/Users/[USER]/Items?" + urlencode(params)
                   + "&api_key=[APIKEY]")
            try:
                response = method(url)
                if response is None:
                    raise RuntimeError("接口未返回响应")
                status_code = getattr(response, "status_code", 200)
                if status_code != 200:
                    raise RuntimeError(f"HTTP {status_code}")
                payload = response.json() if callable(getattr(response, "json", None)) else response
            except Exception as err:
                raise RuntimeError(f"读取 Emby 服务 {service_name} 失败：{err}") from err
            if not isinstance(payload, dict):
                raise RuntimeError(f"Emby 服务 {service_name} 返回了无法识别的数据")
            page = payload.get("Items") or []
            if not isinstance(page, list):
                raise RuntimeError(f"Emby 服务 {service_name} 的 Items 不是列表")
            collected.extend(item for item in page if isinstance(item, dict))
            start_index += len(page)
            total = payload.get("TotalRecordCount")
            if not page or len(page) < page_size or (total is not None and start_index >= int(total)):
                break
        series_map = {
            str(item.get("Id")): item
            for item in collected
            if str(item.get("Type") or "").casefold() == "series" and item.get("Id")
        }
        for item in collected:
            if str(item.get("Type") or "").casefold() != "episode":
                continue
            parent = series_map.get(str(item.get("SeriesId") or ""))
            if not parent:
                continue
            item.setdefault("SeriesProviderIds", parent.get("ProviderIds") or {})
            item.setdefault("SeriesName", parent.get("Name"))
            item.setdefault("SeriesProductionYear", parent.get("ProductionYear"))
        logger.info("[媒体一键归档] Emby 服务 %s 返回 %d 个条目", service_name, len(collected))
        return collected

    @staticmethod
    def _expand_media_sources(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Path 缺失或一条目含多个媒体源时，为每个实体路径生成一个任务条目。"""
        expanded: List[Dict[str, Any]] = []
        for item in items:
            paths: List[str] = []
            direct_path = item.get("Path") or item.get("path")
            if direct_path:
                paths.append(str(direct_path))
            media_sources = item.get("MediaSources") or item.get("mediaSources") or []
            if isinstance(media_sources, list):
                for media_source in media_sources:
                    if isinstance(media_source, dict) and media_source.get("Path"):
                        paths.append(str(media_source["Path"]))
            if not paths:
                expanded.append(item)
                continue
            seen = set()
            for path in paths:
                key = os.path.normcase(path)
                if key in seen:
                    continue
                seen.add(key)
                normalized = dict(item)
                normalized["Path"] = path
                expanded.append(normalized)
        return expanded

    @staticmethod
    def _contains_keywords(text: str, keywords: Iterable[str]) -> bool:
        """按需求执行不区分大小写的“包含”匹配。"""
        folded = text.casefold()
        return any(keyword.casefold() in folded for keyword in keywords)

    def _match_series_zone(self, text: str) -> Optional[str]:
        for zone, keywords in self.SERIES_RULES:
            if self._contains_keywords(text, keywords):
                return zone
        return None

    @staticmethod
    def _safe_relative(source: Path, root: Path) -> Path:
        source_resolved = source.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
        return source_resolved.relative_to(root_resolved)

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        if path.is_dir():
            stem, suffix = path.name, ""
        else:
            stem, suffix = path.stem, path.suffix
        index = 1
        while True:
            candidate = path.with_name(f"{stem}_{index}{suffix}")
            if not candidate.exists():
                return candidate
            index += 1
