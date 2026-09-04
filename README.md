# 媒体虚拟库（MoviePilot v2）

当前版本：`4.3.3`　作者：`Boss`

本插件把 Remux、4K、Dolby Vision、HDR、Atmos 和可选平台榜单显示为 Emby 首页一级媒体库。它不创建 Collection/BoxSet，不移动、不复制、不重命名媒体文件，也不修改 Symedia、115、STRM 或 Emby 的原始 `MediaSource.Path`。

## 你的正确链路

你当前的 MoviePilot 使用 host 网络：

```text
MoviePilot 前端端口：3333
MoviePilot API 端口：3334
原生 Emby：8096
NextEmby 302：8098
```

插件不再启动任何 HTTP 服务，也不会占用 `8097`、`8098` 或 `8099`。它直接把 Emby 兼容网关注册到 MoviePilot 已经监听的 `3334`：

`4.3.3` 修复部分客户端无法进入虚拟库“查看全部”的问题：混合电影/剧集库按 Emby 协议输出空的 `CollectionType`，并同时兼容用户级和根级 `Items`、`Items/Latest` 浏览接口。4.3.2 的压缩响应修复与 4.3.1 的资源优化均继续保留。

```mermaid
flowchart LR
    A["Emby 客户端 :8098"] --> B["NextEmby 302 :8098"]
    B --> C["MoviePilot 内置网关 :3334"]
    C --> D["原生 Emby :8096"]
```

MoviePilot 自己仍连接原生 Emby `8096`；NextEmby 的普通 Emby API 上游改为 MoviePilot `3334`。客户端始终只访问 `8098`，播放请求仍先经过 NextEmby，因此原有 302 规则不变。

## 安装

仓库结构：

```text
MoviePilot-Plugins-Boss/
├── plugins.v2/mediaarchiver/__init__.py
├── icons/folder-move.svg
├── package.v2.json
└── README.md
```

第三方仓库地址：

```text
https://github.com/ZangBanzi/MoviePilot-Plugins-Boss
```

上传新版 `__init__.py`、`package.v2.json` 和 `README.md` 后，在 MoviePilot 插件市场刷新并升级“媒体虚拟库”。重启 MoviePilot 一次，确保旧版独立端口线程完全退出。

## 配置插件

1. MoviePilot 的“媒体服务器”继续绑定原生 Emby `http://NAS局域网IP:8096`，不要改成 3334 或 8098。
2. 打开插件，选择 MoviePilot 已配置的 Emby；不再重复填写 Emby 地址和 API Key。
3. 勾选需要的属性专区和榜单专区。
4. 建议开启“自动维护新增与删除”。
5. 保存后点击“一键重建”。

一级虚拟库是插件的固定功能，因此没有“启用一级虚拟库”这种多余开关。属性专区、榜单和自动同步仍可分别控制。

## 配置 NextEmby

NextEmby 已经占用并发布 `8098`，这是正确的。不要让插件或 MoviePilot 再监听它。

在 NextEmby 中只修改“原 Emby / 上游 Emby”地址：

| 项目 | 填写内容 |
|---|---|
| 对外访问端口 | `8098`，保持不变 |
| 原 Emby / 上游主机 | NAS 的局域网 IP |
| 原 Emby / 上游端口 | `3334` |
| MoviePilot 中的 Emby | 仍为 NAS 局域网 IP:`8096` |

NextEmby 在 `ne_default` 网络，而 MoviePilot 使用 host 网络，所以 NextEmby 里不能填 `127.0.0.1:3334`。它只会指向 NextEmby 容器自己；必须填写 NAS 的实际局域网 IP，例如：

```text
http://192.168.1.10:3334
```

不要把 NextEmby 上游再指回 `8098`，否则会形成循环。

## 验证顺序

### 1. 验证 MoviePilot 内置网关

在 NAS 终端执行：

```bash
curl http://127.0.0.1:3334/__mediaarchiver__/health
```

正常返回示例：

```json
{"ok":true,"gateway":{"running":true,"api_port":3334,"public_port":8098},"views":5}
```

MoviePilot 日志也应出现：

```text
[媒体虚拟库] 一级虚拟库网关已挂载到 MoviePilot:3334；NextEmby 对外端口保持 8098
```

### 2. 验证 NextEmby 能访问 3334

把示例 IP 换成 NAS 的局域网 IP：

```bash
docker exec nextemby sh -c 'wget -qO- http://192.168.1.10:3334/__mediaarchiver__/health'
```

能够返回 JSON，才能把 NextEmby 上游设置为该地址。

### 3. 重建并刷新客户端

插件点击“一键重建”，等待日志显示扫描完成。然后彻底退出并重新打开 Emby 客户端，继续连接：

```text
http://NAS局域网IP:8098
```

首页应出现 `Remux专区`、`4K专区` 等一级库，并显示插件动态生成的品牌封面。

## 实现说明

### MoviePilot v2 API

- `_PluginBase`：插件生命周期、配置、数据保存和页面/API 注册。
- `MediaServerHelper`：读取 MoviePilot 已配置的 Emby 实例、地址和凭据。
- `EventType.WebhookMessage`：媒体新增、更新和删除事件防抖同步。
- APScheduler：定时全量校准。
- MoviePilot 现有 FastAPI 应用：注册 Emby 根路径兼容网关，不创建新监听端口。

### Emby API

- `GET /System/Info`：连接测试。
- `GET /Items`：扫描电影及媒体源/媒体流属性。
- `GET /Users/{UserId}/Views`：在响应中追加一级虚拟库入口。
- `GET /Users/{UserId}/Items`、`/Items/Latest`：返回虚拟库成员，但成员仍是原 ItemId。
- `GET /Items/{VirtualId}/Images/Primary`：输出动态 PNG 一级库封面。
- 其他 API、图片、字幕和播放请求透明转发给原生 Emby。

### 为什么不影响原库和 302

- 虚拟库不是 Collection/BoxSet，也没有第二份媒体文件。
- 同一影片在原媒体库与属性专区共用同一个 Emby ItemId。
- 插件不写入 Emby 媒体项目，不修改路径或媒体源。
- NextEmby 仍是客户端最外层的 `8098`；原 ItemId 和播放请求仍经过其既有 302 判断。
- 多版本电影会检查该 Item 的全部 `MediaSources`；任一版本命中即加入专区，但同一 ItemId 在一个专区只出现一次。
- 全量重建重新计算全部成员；Webhook 增量事件防抖触发校准；文件删除、版本变化或属性不再命中后会从虚拟视图移除。

## 识别规则

| 专区 | 优先判断 |
|---|---|
| Remux | `MediaSources.Path`、文件名及媒体源字段中的 `Remux`，忽略大小写 |
| 4K | 视频宽度 ≥ 3840 或高度 ≥ 2160，再回退 `2160p/4K/UHD` |
| Dolby Vision | 视频流中的 DV/Dolby Vision 字段，再回退文件名关键词 |
| HDR | 视频流 HDR 类型字段，再回退 HDR10/HDR10+/HLG/PQ 等关键词 |
| Atmos | 音频流 Title/Profile/Codec/JOC 等字段，再回退 Atmos 关键词 |

## 常见问题

| 现象 | 处理方法 |
|---|---|
| `Address already in use` | 仍在运行 4.2.x 旧代码；升级后重启 MoviePilot。4.3.1 不会绑定 8098/8099 |
| `3334` 健康检查正常，`8098` 没有虚拟库 | NextEmby 的原 Emby/上游仍指向 8096；改为 NAS 局域网 IP:`3334` |
| 日志出现 `utf-8 codec can't decode byte` | 升级到 4.3.2；旧版会把上游压缩 JSON 直接按 UTF-8 解码 |
| 部分虚拟库能显示但“查看全部”打不开 | 升级到 4.3.3；已修正混合库类型并兼容客户端使用的根级 Items 路径 |
| NextEmby 内访问 `127.0.0.1:3334` 失败 | 它不是 host 网络，改用 NAS 局域网 IP |
| MoviePilot 自己连接异常或循环 | MoviePilot 的 Emby 必须保持 8096，不能指向 3334/8098 |
| 一级库无封面 | 确认请求经过 3334 网关，随后清理客户端图片缓存或重新登录；成员变化会生成新的 ImageTag |
| Apple TV+ Provider 暂时失败 | 4.3.1 会按名称及 Provider ID 350 兜底；外部请求失败会保留上次结果 |
| Disney+ 等连接被重置 | 插件会重试；当次失败保留上次成员，不会删除现有一级库 |

## 安全边界

本插件只改变经由 3334 网关返回给客户端的逻辑视图。它不删除真实电影、不清理 115、不改 Symedia 目录，不上传资源，也不生成第二份 STRM 或媒体文件。
