# 媒体虚拟库 · MoviePilot v2

版本 **4.3.10** · 作者 **Boss**

在 Emby 首页显示 Remux、4K、Dolby Vision、HDR、Atmos、TVB港剧、伦理和已选电影/剧集榜单。每个专区包含现有媒体的原 ItemId；支持动态封面、分页、增量维护和 Cron 定时更新。

## 4.3.10 整体识别优化

本次仍使用 **4.3.10**。主要目标是减少误收，不把“命中数量增加”当作识别准确。

| 专区 | 新的判断依据 | 不再作为自动入选证据 |
|---|---|---|
| Remux | 当前媒体源名称、容器字段、文件名中的独立 Remux 标记 | 简介、父目录、notremux 等连续单词 |
| 4K | 视频流尺寸优先，其次媒体源尺寸；尺寸缺失才使用 2160p/4K/UHD 文件名标记 | 字幕流尺寸、与实际 1080p 冲突的文件名 |
| Dolby Vision | DV Profile、Dolby Vision/DOVI 等格式信息；格式未知才回退文件名 | 已知 SDR/HDR 格式时仅凭文件名覆盖实际参数 |
| HDR | 视频流 HDR/HLG/PQ 等格式信息或 DV；格式未知才回退文件名 | Main10、10bit 本身 |
| Atmos | 音频流 Title/Profile/Codec 等字段中的 Atmos/JOC；无音频描述时回退文件名 | TrueHD、7.1、声道数本身 |
| TVB港剧 | Tags/Studios 中完整的 TVB、Television Broadcasts、无线电视等标记 | 港剧、myTVSUPER、埋堆堆、简介或路径中的 TVB |
| 伦理 | 明确情色题材 Tags/Genres，或手工 Tags“伦理” | 年龄分级、简介、父目录、家庭伦理类型、长大成人等词语 |
| 外部榜单 | 媒体类型及 Provider ID；没有可靠 ID 命中时，仅接受类型、年份、完整标题一致且唯一的候选 | 无年份同名、电影与剧集跨类型、已知 ID 冲突后继续按标题匹配 |

4K 接受宽度至少 3840 或高度至少 2160，兼容裁掉黑边的宽银幕片。不同 MediaSources 分开判断；任一版本满足即可入选，但不会把一个版本的尺寸与另一个版本的文件名混用。同一 ItemId 在专区中只保留一次，播放版本仍由客户端和 Emby 决定。

技术属性专区目前面向电影；TVB、伦理支持电影和剧集，平台专区根据选择包含电影、剧集或二者。未增加逐集读取视频的扫描。

### 手动纠正

在 Emby 条目编辑页面修改 **标签 Tags**，下一次同步生效：

| 操作 | 标签示例 |
|---|---|
| 加入 Remux | 虚拟库:remux |
| 排除 Remux | 排除remux |
| 加入 TVB | 虚拟库:tvb |
| 排除 TVB | 排除tvb |
| 加入伦理 | 伦理 |
| 排除伦理 | 排除伦理 |

其他技术属性键为 `4k`、`dolby_vision`、`hdr`、`atmos`；也支持完整专区名称，例如 `虚拟库:HDR专区`。排除标签优先。手动标签用于属性专区，不改变外部榜单。

伦理自动题材值包括“情色”“情色片”“情色电影”（含繁体）、erotic、erotica、softcore、sexploitation、pink film、roman porno、jav；采用完整标签匹配。泛指“伦理”的 Genres 类型不作为证据，Tags 中的“伦理”视为人工确认。不会排除全部动画或按片名硬编码黑名单。

### 榜单来源与缓存

- IMDb 只从页面结构化榜单节点取 ID，不扫描整页推荐链接。
- IMDb/AniList 获取失败时，不再用 TMDB 热门冒充原榜单。
- Netflix、Apple TV+ 等 TMDB Watch Provider 数据属于所选地区的可播精选，不代表平台官方热度榜或原创出品。显示名称注明“TMDB可播”，先合并各地区候选，再按热度统一截取；不代表完整平台片库。
- Apple TV+ 不再以泛指 Apple TV 的商店名称匹配。
- 混合榜任一子源失败时，不提交残缺的新快照。已有严格规则生成的结果可保留，并移除 Emby 中已经删除的条目。
- 旧版本快照没有严格规则标记，不继续信任。升级后如来源失败，对应专区可能暂时为空；待来源恢复后重新同步。
- 猫眼等网页数据缺少可靠 ID、年份时可能无法匹配。可通过既有自定义 Feed 补充媒体类型、Provider ID 等信息，不按相似标题猜测。

元数据不足或本身错误仍可能造成漏收/误收。可先修正 Emby 元数据或补充属性标签；本包没有新增“待确认”页面，也不承诺识别准确率百分比。

## 保留的 4.3.7 优化

- 媒体去重统一在同步入口完成，后续直接复用字典视图，减少整库列表复制。
- 只在有成功获取的启用榜单需要匹配时构建榜单索引；属性专区全部取消勾选时跳过属性识别。
- Provider 别名改为固定字典查找，同名标题只建立一次索引记录。
- 新媒体快照在共享锁外构建，持锁时只交换引用，让同步与浏览减少相互等待。
- 封面绘图交给后台线程；同一封面的并发请求复用一次绘图。冷封面暂时串行生成以限制 CPU，缓存仍最多保留 96 张。
- 删除无调用的 SVG 生成器和旧响应头字典转换方法，保留实际使用的 PNG 封面及完整响应头转发。

## 保留的 4.3.6 修复

4.3.5 的测试遗漏了实际代理中的多种异常响应，不能据此保证所有客户端可用。4.3.6 已在真实 HTTPX、FastAPI 和本地 HTTP 上游中复现并修复：

- Brotli/deflate 压缩体丢失 `Content-Encoding` 后被当作 UTF-8 解析。
- 上游已解压却遗留 gzip 头，以及没有声明原始长度的 Zstd 帧解压失败。
- 1 MiB 聚合分块等待后续数据，延迟首块交付。现在 HTTPX 按上游到达的块立即转发；标准库回退使用 `read1(64 KiB)`。
- 多个 `Set-Cookie` 被字典合并。现在保留原始头字节和全部 Cookie；共享连接池不保存用户会话。
- 客户端断开前后、响应还未开始迭代时的资源释放；WebSocket 转发任务完整取消和回收。
- 中文设备名等请求头及编码路径的转发；HEAD、304、204、401、Range、302 的协议处理。

只对需要注入/改写的 JSON 进行有上限的缓冲、解码与校验；不使用忽略错误字符的方式掩盖数据损坏。JSON 损坏时最多重试一次只读请求，仍失败就返回可定位的错误。新增运行版本、源码指纹和脱敏诊断，可判断服务器究竟加载了哪份文件。

## 端口和访问链路

| 组件 | 端口/配置 |
|---|---|
| 原生 Emby `emby-sa` | `8096` |
| MoviePilot `moviepilot-v2` | host 网络；前端 `3333`，API `3334` |
| MoviePilot 的媒体服务器配置 | 继续连接原生 Emby `8096` |
| NextEmby `nextemby` | 普通 API 上游使用 NAS 局域网 IP:`3334` |
| 所有客户端 | 继续连接 NextEmby `8098` |

普通 API 经过 **客户端 → NextEmby:8098 → MoviePilot 网关:3334 → Emby:8096**。

插件注册在 MoviePilot 已有 API 端口，不创建任何新监听。NextEmby 继续处理其原有 302 规则。转发响应不跟随或改写 Location；播放器使用原媒体 ItemId 和 MediaSource。本插件不创建 Collection/BoxSet，也不移动、复制、重命名真实媒体文件或改动 115、STRM、Symedia 目录。

NextEmby 使用容器网络，配置其上游时使用 NAS 局域网 IP，`127.0.0.1` 指向的是 NextEmby 容器自己。已经接通上述链路的用户无需更改地址。

## 上传 GitHub 和升级

解压发布包，将文件按下列位置覆盖上传；不要只把 ZIP 放进仓库。

| 文件 | GitHub 中的位置 |
|---|---|
| 插件代码 | `plugins.v2/mediaarchiver/__init__.py` |
| **解码依赖** | `plugins.v2/mediaarchiver/requirements.txt` |
| 插件索引 | 根目录 `package.v2.json` |
| 说明 | 根目录 `README.md` |
| 图标 | `icons/folder-move.svg` |
| 可选测试与验证记录 | 根目录 `tests/`、`TEST_REPORT.md`、`requirements-dev.txt` |

仓库若包含其他插件，保留其索引，只合并 `MediaArchiver` 条目。发布索引的文件名必须是 `package.v2.json`。

MoviePilot v2 支持插件目录的 `requirements.txt`。沿用 Brotli 和 Zstandard 解码依赖，不替换 MoviePilot 自带的 FastAPI/HTTPX。请把依赖文件和代码一并上传，再通过插件市场升级/重新安装，使宿主执行依赖安装。[MoviePilot 官方插件仓库规范](https://github.com/jxxghp/MoviePilot-Plugins)

升级步骤：

1. 在 GitHub 提交新版文件；在 MoviePilot 插件市场刷新并升级至 **4.3.10**。
2. 重启 MoviePilot，确保旧网关代码退出：`docker restart moviepilot-v2`。
3. 等待 MoviePilot 启动，执行下方健康检查。确认版本和依赖正确，再点击插件“一键重建”。
4. 退出并重新打开客户端，继续使用 `8098`。

升级前备份已有插件文件和配置。本包未自动上传你的 GitHub，也未部署到 NAS。

本次仍叫 **4.3.10**。若已安装同版本，仅刷新市场不一定加载新代码，需要重新安装该版本或覆盖已安装的插件文件后重启。用健康接口的 `code_sha256` 对照包内 `SHA256SUMS`，不能只看版本号；加载成功后执行一次“一键重建”，再刷新播放器的媒体库列表。

## 确认实际运行版本

在 NAS 上执行：

```bash
curl -s http://127.0.0.1:3334/__mediaarchiver__/health
```

检查：

| 字段 | 含义 |
|---|---|
| `version` | 必须为 `4.3.10`；缺少此字段不能证明新版已加载 |
| `code_sha256` | 当前加载代码的指纹，可与发布包 `SHA256SUMS` 对照 |
| `performance.async_pool` | `true` 表示支持 HTTPX 连接池；这不是 Emby 连通性结论 |
| `decoders.br` / `decoders.zstd` | 应为 `true`，否则查看 MoviePilot 插件依赖安装日志 |
| `performance.inflight` / `peak_inflight` | 当前/峰值的请求处理数，统计到响应对象创建完成 |
| `performance.active_streams` | 尚未发送结束的响应流数量；空闲后应回落 |
| `performance.failures` | 本次加载后累计网关异常数 |
| `decode_recoveries` / `decode_retries` | 成功解码/规范化响应次数及 JSON 只读重试次数，位于 `performance` 内 |
| `last_error` | 最后一次异常的请求方法、无查询串路径、阶段、状态、压缩标记和代码位置 |

`ok=true` 仅表示健康接口可响应，不能单凭它断言客户端可用。错误阶段可能为 `read_response`、`decode_json`、`transform_json`、`open_stream` 或 `stream_body`。日志和诊断不包含 API Key、Cookie、查询参数及响应正文；错误最多每 30 秒输出一次，累计失败数仍完整记录。

如果 `3334` 已是新版、`8098` 表现仍不同，再检查 `8098/__mediaarchiver__/health` 是否也返回同一版本与指纹；NextEmby 若拦截自定义路径，此项只能作为路由诊断，需同时检查实际 `/Users/{id}/Views` 请求。正常浏览时原始媒体 API 仍由 Emby 按入站用户凭据判断权限，不使用插件的管理 Key 替代客户端 Key。

## 配置和同步

选择 MoviePilot 已配置的 Emby，勾选属性专区和平台榜单，保存后点击“一键重建”。不需要重新填写 Emby 地址或 Key。

- 实时增量维护：合并新增/更新/删除事件，并按校准间隔扫描。使用缓存的榜单，减少外部请求。
- 定时更新全部专区：刷新 Emby 条目和启用的榜单，处理新增、删除和版本变化。外部榜单获取失败时仅保留严格规则生成的上次有效结果，旧规则快照不继续沿用。
- Cron 为 APScheduler 五段格式：分钟、小时、日期、月份、星期，按 MoviePilot 时区执行；建议使用 `TZ=Asia/Shanghai`。
- 星期建议写 `mon` 到 `sun`。APScheduler 的数字 `0` 是星期一，不能套用 Linux crontab 的星期日编号。无效表达式回退为 `0 4 * * *` 并记日志。

| 时间 | Cron |
|---|---|
| 每天 04:00 | `0 4 * * *` |
| 每天 04:00、16:00 | `0 4,16 * * *` |
| 每六小时 | `0 */6 * * *` |
| 每周日 03:30 | `30 3 * * sun` |

Remux 检查文件名和指定媒体源字段；4K、Dolby Vision、HDR、Atmos 依据上述结构化字段与冲突规则判断。多版本电影遍历所有 MediaSources，任一版本符合即可入选，同一 ItemId 只列一次。播放版本仍由客户端和 Emby 决定。

## 性能与测试边界

共享 HTTPX 池上限为 128 条连接、64 条保活连接、保活 30 秒；这些数值是连接数，不代表可承载 128 名用户。连接池不保存任何用户 Cookie，客户端原始鉴权头原样转发。

缓存只保留最多 192 组不包含用户会话的筛选/排序 ID 结果，30 秒过期、索引更换时失效。海报缓存保持 ETag/304，避免每次重新绘图。媒体/普通响应按块转发，后台榜单刷新与浏览数据互不混用。

`TEST_REPORT.md` 记录本次识别优化的验证。测试使用真实 HTTPX/FastAPI、回环 HTTP 上游和 WebSocket，并保留媒体识别与定时维护回归；未使用你的实际 NextEmby、Emby、客户端或网盘，不能替代 NAS 实测，也没有保证所有第三方客户端均已兼容。

独立测试环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python tests/test_virtual_library.py
.venv/bin/python tests/test_accuracy.py
.venv/bin/python -m pytest tests/test_gateway_runtime.py -q
```

连接池与流式关闭设计参照 [HTTPX 异步支持文档](https://www.python-httpx.org/async/)。MoviePilot 的生命周期、配置、事件、定时服务和媒体服务器 Helper 沿用现有插件实现。
