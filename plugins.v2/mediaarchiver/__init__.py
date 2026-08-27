"""MoviePilot v2：Boss 媒体一键归档（STRM / CloudDrive2 双模式）。"""
from __future__ import annotations

import base64
import os
import posixpath
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app import schemas
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


@dataclass(frozen=True)
class DavEntry:
    path: str
    name: str
    is_dir: bool
    size: int = 0


class CloudDriveWebDAV:
    """通过 CD2 内置 WebDAV 获取目录并执行服务端 MOVE/COPY。"""
    def __init__(self, url: str, username: str, password: str, timeout: int = 20):
        url = (url or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("CD2 WebDAV 地址必须以 http:// 或 https:// 开头")
        self.base_url = url
        self.timeout = max(5, min(int(timeout), 120))
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "User-Agent": "MoviePilot-MediaArchiver/2.0"}

    @staticmethod
    def normalize(path: str) -> str:
        value = urllib.parse.unquote(str(path or "/")).replace("\\", "/")
        return posixpath.normpath("/" + value.lstrip("/"))

    def url(self, path: str) -> str:
        return self.base_url + urllib.parse.quote(self.normalize(path), safe="/!$&'()*+,;=:@")

    def request(self, method: str, path: str, headers: Optional[dict] = None,
                data: bytes = b"", expected: Sequence[int] = (200, 201, 204, 207)) -> bytes:
        merged = dict(self.headers)
        merged.update(headers or {})
        req = urllib.request.Request(self.url(path), data=data, headers=merged, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                if response.status not in expected:
                    raise RuntimeError(f"CD2 返回 HTTP {response.status}")
                return response.read()
        except urllib.error.HTTPError as err:
            detail = err.read(300).decode("utf-8", "replace")
            raise RuntimeError(f"CD2 {method} {path} 失败：HTTP {err.code} {detail}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"无法连接 CD2 WebDAV：{err.reason}") from err

    def list(self, path: str) -> List[DavEntry]:
        body = b'''<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>
<d:displayname/><d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>'''
        raw = self.request("PROPFIND", path, {"Depth": "1", "Content-Type": "application/xml"}, body, (207,))
        requested = self.normalize(path).rstrip("/") or "/"
        base_path = urllib.parse.urlsplit(self.base_url).path.rstrip("/")
        result: List[DavEntry] = []
        for response in ET.fromstring(raw).findall("{DAV:}response"):
            href = urllib.parse.unquote(urllib.parse.urlsplit(response.findtext("{DAV:}href") or "").path)
            if base_path and href.startswith(base_path + "/"):
                href = href[len(base_path):]
            item_path = self.normalize(href)
            if item_path.rstrip("/") == requested.rstrip("/"):
                continue
            prop = response.find(".//{DAV:}prop")
            if prop is None:
                continue
            rtype = prop.find("{DAV:}resourcetype")
            is_dir = rtype is not None and rtype.find("{DAV:}collection") is not None
            name = prop.findtext("{DAV:}displayname") or posixpath.basename(item_path.rstrip("/"))
            try:
                size = int(prop.findtext("{DAV:}getcontentlength") or 0)
            except ValueError:
                size = 0
            result.append(DavEntry(item_path, name, is_dir, size))
        return result

    def exists(self, path: str) -> bool:
        try:
            self.request("PROPFIND", path, {"Depth": "0"}, expected=(207,))
            return True
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return False
            raise

    def mkdirs(self, path: str) -> None:
        current = ""
        for part in self.normalize(path).strip("/").split("/"):
            if part:
                current += "/" + part
                if not self.exists(current):
                    self.request("MKCOL", current, expected=(201, 204))

    def transfer(self, source: str, target: str, copy: bool) -> None:
        self.request("COPY" if copy else "MOVE", source,
                     {"Destination": self.url(target), "Overwrite": "F"}, expected=(201, 204))


class MediaArchiver(_PluginBase):
    plugin_name = "Boss 媒体一键归档"
    plugin_desc = "低占用整理 STRM，或通过 CloudDrive2 服务端整理 115 网盘文件。"
    plugin_icon = "folder-move.svg"
    plugin_version = "2.2.0"
    plugin_author = "Boss"
    author_url = ""
    plugin_config_prefix = "mediaarchiver_"
    plugin_order = 50
    auth_level = 1

    CATEGORIES = ("电影Remux归档", "奈飞专区", "迪士尼专区", "HBO其他专区", "APTV专区", "伦理专区")
    ADULT = ("adult", "jav", "xxx", "伦理", "情色", "18+")
    RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("奈飞专区", ("netflix", "web-dl.nf", " nf ", "奈飞")),
        ("迪士尼专区", ("disney+", "disney", " dsnp ", " d+ ", "迪士尼")),
        ("HBO其他专区", ("hbo max", "hbomax", " hbo ", " max ", "amazon", "prime video", " amzn ", "paramount+", "hulu", "peacock")),
        ("APTV专区", ("apple tv+", "apple tv", "atvp", "atv+", "aptv", "苹果tv")),
    )
    _lock = threading.Lock()
    _status_lock = threading.Lock()
    _status: Dict[str, Any] = {"state": "idle", "message": "尚未执行", "stats": {}}

    def __init__(self):
        super().__init__()
        self._run_logs = deque(maxlen=500)
        self._logs_lock = threading.Lock()
        self._log_unsaved = 0

    def init_plugin(self, config: Optional[dict] = None) -> None:
        c = config or {}
        run_strm_once = bool(c.get("run_strm_once", False))
        run_cd2_once = bool(c.get("run_cd2_once", False))
        self._enabled = bool(c.get("enabled", True))
        self._mode = str(c.get("mode") or "strm")
        self._dry_run = bool(c.get("dry_run", True))
        self._strm_root = Path(c.get("strm_root") or "/mnt/strm")
        self._strm_movie = str(c.get("strm_movie_root") or "")
        self._strm_series = str(c.get("strm_series_root") or "")
        self._cd2_url = str(c.get("cd2_url") or "")
        self._cd2_user = str(c.get("cd2_username") or "")
        self._cd2_password = str(c.get("cd2_password") or "")
        self._remote_root = str(c.get("cd2_remote_root") or "/")
        self._cd2_movie = str(c.get("cd2_movie_root") or "")
        self._cd2_series = str(c.get("cd2_series_root") or "")
        self._depth = max(1, min(int(c.get("scan_depth") or 2), 3))
        self._max_tasks = max(1, min(int(c.get("max_tasks") or 500), 5000))
        self._timeout = max(5, min(int(c.get("timeout") or 20), 120))
        try:
            saved_logs = self.get_data("run_logs") or []
            with self._logs_lock:
                self._run_logs = deque(saved_logs[-500:], maxlen=500)
        except Exception:
            pass

        # MoviePilot V2 最稳定的一次性任务触发方式：配置页打开开关后点保存，
        # init_plugin 收到配置并由后端直接启动，不依赖前端自定义 API 按钮。
        if run_strm_once or run_cd2_once:
            requested_mode = "cd2" if run_cd2_once else "strm"
            self._mode = requested_mode
            reset_config = dict(c)
            reset_config["mode"] = requested_mode
            reset_config["run_strm_once"] = False
            reset_config["run_cd2_once"] = False
            try:
                self.update_config(reset_config)
            except Exception as err:
                logger.warning("[Boss媒体归档] 一次性开关复位失败：%s", err)
            self._record_log("INFO", f"已收到保存触发，准备运行{'STRM' if requested_mode == 'strm' else '原网盘/CD2'}任务",
                             persist=True)
            timer = threading.Timer(1.0, self._start_task, kwargs={"force": True})
            timer.daemon = True
            timer.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/run", "endpoint": self.run_archive, "methods": ["POST"], "auth": "bear", "summary": "执行归档"},
            {"path": "/status", "endpoint": self.get_archive_status, "methods": ["GET"], "auth": "bear", "summary": "归档状态"},
            {"path": "/logs/clear", "endpoint": self.clear_logs, "methods": ["GET"], "auth": "bear", "summary": "清空运行日志"},
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def text_field(model: str, label: str, cols: int = 12, **extra) -> dict:
            props = {"model": model, "label": label}
            props.update(extra)
            return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": [
                {"component": "VTextField", "props": props}]}

        form = {
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VSwitch", "props": {"model": "dry_run", "label": "演练模式（首次必须开启）"}}]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                            "text": "进入目标分页，打开“立即运行一次”，再点击底部保存。"}}]},
                ]},
                {"component": "VTabs", "props": {"model": "mode", "fixed-tabs": True,
                                                     "style": {"margin-top": "8px", "margin-bottom": "16px"}},
                 "content": [
                     {"component": "VTab", "props": {"value": "strm", "prepend-icon": "mdi-file-link"}, "text": "STRM 文件整理"},
                     {"component": "VTab", "props": {"value": "cd2", "prepend-icon": "mdi-cloud-sync"}, "text": "原网盘整理（CD2）"},
                 ]},
                {"component": "VWindow", "props": {"model": "mode"}, "content": [
                    {"component": "VWindowItem", "props": {"value": "strm"}, "content": [
                        {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                            "text": "只整理本地STRM标题目录，不移动115中的真实视频。"}},
                        {"component": "VRow", "props": {"style": {"margin-top": "8px"}}, "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VSwitch", "props": {"model": "run_strm_once", "color": "success",
                                    "label": "立即运行一次STRM整理（开启后点击保存）"}}]},
                            text_field("strm_root", "STRM 总根目录，例如 /mnt/strm"),
                            text_field("strm_movie_root", "STRM 电影目录（留空自动识别）", 6),
                            text_field("strm_series_root", "STRM 电视剧目录（留空自动识别）", 6),
                        ]},
                    ]},
                    {"component": "VWindowItem", "props": {"value": "cd2"}, "content": [
                        {"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                            "text": "这里填写CD2的WebDAV地址。服务端MOVE/COPY不会让大文件经过MoviePilot容器。"}},
                        {"component": "VRow", "props": {"style": {"margin-top": "8px"}}, "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VSwitch", "props": {"model": "run_cd2_once", "color": "success",
                                    "label": "立即运行一次原网盘整理（开启后点击保存）"}}]},
                            text_field("cd2_url", "CD2 WebDAV地址，例如 http://192.168.1.2:19798/dav"),
                            text_field("cd2_username", "CD2 WebDAV用户名", 6),
                            text_field("cd2_password", "CD2 WebDAV密码", 6, type="password"),
                            text_field("cd2_remote_root", "CD2扫描根目录，通常填 / 或 /115"),
                            text_field("cd2_movie_root", "原网盘电影目录（留空自动识别）", 6),
                            text_field("cd2_series_root", "原网盘电视剧目录（留空自动识别）", 6),
                        ]},
                    ]},
                ]},
                {"component": "VDivider", "props": {"class": "my-4"}},
                {"component": "div", "props": {"class": "text-subtitle-1 mb-2"}, "text": "通用运行设置"},
                {"component": "VRow", "content": [
                    text_field("scan_depth", "识别深度 1-3", 4, type="number"),
                    text_field("max_tasks", "单次最多任务", 4, type="number"),
                    text_field("timeout", "CD2连接超时（秒）", 4, type="number"),
                ]},
                {"component": "VAlert", "props": {"type": "success", "variant": "tonal",
                    "text": "运行记录请进入插件的数据页面查看；任务运行中刷新数据页即可看到最新日志。"}},
            ],
        }
        return ([form], {
            "enabled": True, "dry_run": True, "mode": "strm", "run_strm_once": False,
            "run_cd2_once": False, "strm_root": "/mnt/strm",
            "strm_movie_root": "", "strm_series_root": "", "cd2_url": "http://127.0.0.1:19798/dav",
            "cd2_username": "", "cd2_password": "", "cd2_remote_root": "/", "cd2_movie_root": "",
            "cd2_series_root": "", "scan_depth": 2, "max_tasks": 500, "timeout": 20,
        })

    def get_page(self) -> Optional[List[dict]]:
        with self._status_lock:
            status = dict(self._status)
        try:
            persisted = self.get_data("run_logs") or []
        except Exception:
            persisted = []
        with self._logs_lock:
            current = list(self._run_logs)
        logs = current or persisted
        rows = [{"component": "tr", "content": [
            {"component": "td", "text": str(item.get("time", ""))},
            {"component": "td", "text": str(item.get("level", "INFO"))},
            {"component": "td", "text": str(item.get("mode", ""))},
            {"component": "td", "text": str(item.get("message", ""))},
        ]} for item in reversed(logs[-300:])]
        stats = status.get("stats") or {}
        stat_text = (f"扫描 {stats.get('scanned', 0)} ｜ 识别 {stats.get('matched', 0)} ｜ "
                     f"移动 {stats.get('moved', 0)} ｜ 复制 {stats.get('copied', 0)} ｜ "
                     f"跳过 {stats.get('skipped', 0)} ｜ 失败 {stats.get('failed', 0)}")
        return [{"component": "div", "props": {"class": "pa-3"}, "content": [
            {"component": "VAlert", "props": {
                "type": "error" if status.get("state") == "failed" else "info", "variant": "tonal",
                "title": f"任务状态：{status.get('state', 'idle')}",
                "text": f"{status.get('message', '尚未执行')}\n{stat_text}"}},
            {"component": "div", "props": {"class": "d-flex justify-space-between align-center my-3"}, "content": [
                {"component": "div", "props": {"class": "text-h6"}, "text": "最近运行日志（刷新本页查看最新内容）"},
                {"component": "VBtn", "props": {"color": "error", "variant": "outlined", "size": "small"},
                 "text": "清空日志", "events": {"click": {"api": "plugin/MediaArchiver/logs/clear",
                                                              "method": "get", "params": {"token": settings.API_TOKEN}}}},
            ]},
            {"component": "VTable", "props": {"hover": True, "density": "compact"}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": "时间"}, {"component": "th", "text": "级别"},
                    {"component": "th", "text": "模式"}, {"component": "th", "text": "内容"}]}]},
                {"component": "tbody", "content": rows or [{"component": "tr", "content": [
                    {"component": "td", "props": {"colspan": 4, "class": "text-center"}, "text": "暂无日志"}]}]},
            ]},
        ]}]

    def stop_service(self) -> None:
        pass

    def run_archive(self) -> schemas.Response:
        return self._start_task(force=False)

    def _start_task(self, force: bool = False) -> schemas.Response:
        if not self._enabled and not force:
            return schemas.Response(success=False, message="插件未启用")
        if not self._lock.acquire(False):
            return schemas.Response(success=False, message="任务正在运行，请勿重复点击")
        with self._status_lock:
            self._status = {"state": "running", "message": "任务已启动", "stats": {}, "started_at": int(time.time())}
        self._record_log("INFO", f"任务启动：模式={'STRM' if self._mode == 'strm' else '原网盘/CD2'}，"
                                  f"演练={'是' if self._dry_run else '否'}", persist=True)
        threading.Thread(target=self._worker, name="mediaarchiver-worker", daemon=True).start()
        return schemas.Response(success=True, message="任务已在后台启动，请进入插件数据页查看运行日志")

    def get_archive_status(self) -> schemas.Response:
        with self._status_lock:
            data = dict(self._status)
        return schemas.Response(success=data.get("state") != "failed", message=str(data.get("message")), data=data)

    def clear_logs(self) -> schemas.Response:
        with self._logs_lock:
            self._run_logs.clear()
            self._log_unsaved = 0
        try:
            self.save_data("run_logs", [])
        except Exception as err:
            return schemas.Response(success=False, message=f"清空日志失败：{err}")
        return schemas.Response(success=True, message="运行日志已清空，请刷新数据页")

    def _record_log(self, level: str, message: str, persist: bool = False) -> None:
        entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "level": level.upper(),
                 "mode": "STRM" if self._mode == "strm" else "原网盘/CD2",
                 "message": str(message)}
        with self._logs_lock:
            self._run_logs.append(entry)
            self._log_unsaved += 1
            snapshot = list(self._run_logs)
            should_save = persist or self._log_unsaved >= 20
            if should_save:
                self._log_unsaved = 0
        if should_save:
            try:
                self.save_data("run_logs", snapshot)
            except Exception as err:
                logger.warning("[Boss媒体归档] 保存插件日志失败：%s", err)
        text = f"[Boss媒体归档][{entry['mode']}] {message}"
        if level.upper() == "ERROR":
            logger.error(text)
        elif level.upper() == "WARNING":
            logger.warning(text)
        else:
            logger.info(text)

    def _worker(self) -> None:
        try:
            stats = self._archive_strm() if self._mode == "strm" else self._archive_cd2()
            message = (f"{'演练' if self._dry_run else '整理'}完成：识别{stats['matched']}，移动{stats['moved']}，"
                       f"复制{stats['copied']}，跳过{stats['skipped']}，失败{stats['failed']}")
            with self._status_lock:
                self._status = {"state": "completed", "message": message, "stats": stats, "finished_at": int(time.time())}
            self._record_log("INFO", message, persist=True)
        except Exception as err:
            self._record_log("ERROR", f"执行失败：{err}", persist=True)
            logger.exception("[Boss媒体归档] 失败堆栈")
            with self._status_lock:
                self._status = {"state": "failed", "message": f"执行失败：{err}", "stats": {}}
        finally:
            self._lock.release()

    @staticmethod
    def _stats() -> Dict[str, int]:
        return {"scanned": 0, "matched": 0, "moved": 0, "copied": 0, "skipped": 0, "failed": 0}

    def _archive_strm(self) -> Dict[str, int]:
        if not self._strm_root.is_dir():
            raise FileNotFoundError(f"STRM 根目录不存在：{self._strm_root}")
        movie = self._local_root(self._strm_root, self._strm_movie, ("电影", "Movies", "媒体库/电影"))
        series = self._local_root(self._strm_root, self._strm_series, ("电视剧", "剧集", "TV", "媒体库/电视剧", "媒体库/剧集"))
        self._record_log("INFO", f"已识别STRM目录：电影={movie}，电视剧={series}")
        stats, tasks, seen = self._stats(), [], set()
        for kind, root in (("movie", movie), ("series", series)):
            if root is None:
                continue
            for current, dirs, files in os.walk(root):
                dirs[:] = [x for x in dirs if x not in self.CATEGORIES and not x.startswith(".")]
                for name in files:
                    if not name.casefold().endswith(".strm"):
                        continue
                    stats["scanned"] += 1
                    strm = Path(current) / name
                    rel = strm.relative_to(root)
                    unit = root / rel.parts[0] if len(rel.parts) > 1 else strm
                    if unit in seen:
                        continue
                    seen.add(unit)
                    category = self._classify(self._local_text(unit, strm), kind)
                    if not category:
                        stats["skipped"] += 1
                        continue
                    tasks.append((unit, root / category / unit.name))
                    stats["matched"] += 1
                    if len(tasks) >= self._max_tasks:
                        break
        for source, wanted in tasks:
            try:
                target = self._unique_local(wanted)
                self._record_log("INFO", f"{'[演练]' if self._dry_run else ''}移动：{source} -> {target}")
                if not self._dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.rename(source, target)
                    except OSError:
                        shutil.move(str(source), str(target))
                stats["moved"] += 1
            except Exception as err:
                stats["failed"] += 1
                self._record_log("ERROR", f"STRM处理失败：{source}：{err}")
        return stats

    def _archive_cd2(self) -> Dict[str, int]:
        if not self._cd2_url or not self._cd2_user:
            raise ValueError("请填写 CD2 WebDAV 地址和用户名")
        dav = CloudDriveWebDAV(self._cd2_url, self._cd2_user, self._cd2_password, self._timeout)
        movie = self._dav_root(dav, self._cd2_movie, ("115/电影", "电影", "媒体库/电影", "Movies"))
        series = self._dav_root(dav, self._cd2_series, ("115/电视剧", "115/剧集", "电视剧", "剧集", "媒体库/电视剧", "媒体库/剧集", "TV"))
        self._record_log("INFO", f"CD2连接成功，已识别原网盘目录：电影={movie}，电视剧={series}", persist=True)
        stats, tasks = self._stats(), []
        for kind, root in (("movie", movie), ("series", series)):
            if not root:
                continue
            entries = dav.list(root)
            stats["scanned"] += len(entries)
            for entry in entries:
                if entry.name in self.CATEGORIES:
                    continue
                text = f"{entry.path} {entry.name}"
                category = self._classify(text, kind)
                if not category and entry.is_dir and self._depth > 1:
                    category = self._classify(text + " " + self._dav_names(dav, entry.path, self._depth - 1), kind)
                if not category:
                    stats["skipped"] += 1
                    continue
                copy = kind == "movie" and category == "电影Remux归档"
                tasks.append((entry, posixpath.join(root, category, entry.name), copy))
                stats["matched"] += 1
                if len(tasks) >= self._max_tasks:
                    break
        for entry, wanted, copy in tasks:
            try:
                target = self._unique_dav(dav, wanted)
                self._record_log("INFO", f"{'[演练]' if self._dry_run else ''}{'复制' if copy else '移动'}：{entry.path} -> {target}")
                if not self._dry_run:
                    dav.mkdirs(posixpath.dirname(target))
                    dav.transfer(entry.path, target, copy)
                stats["copied" if copy else "moved"] += 1
            except Exception as err:
                stats["failed"] += 1
                self._record_log("ERROR", f"CD2处理失败：{entry.path}：{err}")
        return stats

    @staticmethod
    def _local_root(base: Path, configured: str, candidates: Sequence[str]) -> Optional[Path]:
        if configured:
            path = Path(configured)
            if not path.is_dir():
                raise FileNotFoundError(f"配置目录不存在：{path}")
            return path
        return next((base / x for x in candidates if (base / x).is_dir()), None)

    def _dav_root(self, dav: CloudDriveWebDAV, configured: str, candidates: Sequence[str]) -> Optional[str]:
        if configured:
            path = dav.normalize(configured)
            if not dav.exists(path):
                raise FileNotFoundError(f"CD2 配置目录不存在：{path}")
            return path
        base = dav.normalize(self._remote_root)
        for item in candidates:
            path = dav.normalize(posixpath.join(base, item))
            if dav.exists(path):
                return path
        return None

    @staticmethod
    def _local_text(unit: Path, first: Path) -> str:
        chunks, files = [str(unit), str(first)], [first]
        if unit.is_dir():
            files = []
            for current, dirs, names in os.walk(unit):
                dirs[:] = dirs[:8]
                for name in names[:80]:
                    chunks.append(name)
                    if name.casefold().endswith(".strm") and len(files) < 12:
                        files.append(Path(current) / name)
                if len(chunks) >= 80:
                    break
        for path in files[:12]:
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as stream:
                    chunks.append(stream.read(4096))
            except OSError:
                pass
        return " ".join(chunks)

    def _dav_names(self, dav: CloudDriveWebDAV, root: str, depth: int) -> str:
        names, queue = [], [(root, 0)]
        while queue and len(names) < 80:
            current, level = queue.pop(0)
            for item in dav.list(current):
                names.extend((item.name, item.path))
                if item.is_dir and level + 1 < depth:
                    queue.append((item.path, level + 1))
                if len(names) >= 80:
                    break
        return " ".join(names[:80])

    def _classify(self, text: str, kind: str) -> Optional[str]:
        text = f" {text.casefold()} "
        if self._contains(text, self.ADULT):
            return "伦理专区"
        if kind == "movie" and "remux" in text:
            return "电影Remux归档"
        for category, keywords in self.RULES:
            if self._contains(text, keywords):
                return category
        return None

    @staticmethod
    def _contains(text: str, keywords: Iterable[str]) -> bool:
        return any(x.casefold() in text for x in keywords)

    @staticmethod
    def _unique_local(path: Path) -> Path:
        if not path.exists():
            return path
        for number in range(1, 10000):
            candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"无法生成无重名路径：{path}")

    @staticmethod
    def _unique_dav(dav: CloudDriveWebDAV, path: str) -> str:
        if not dav.exists(path):
            return path
        parent, name = posixpath.split(path)
        stem, suffix = posixpath.splitext(name)
        for number in range(1, 10000):
            candidate = posixpath.join(parent, f"{stem}_{number}{suffix}")
            if not dav.exists(candidate):
                return candidate
        raise RuntimeError(f"无法生成 CD2 无重名路径：{path}")
