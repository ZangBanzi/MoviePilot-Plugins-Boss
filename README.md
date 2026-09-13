# 媒体虚拟库 · MoviePilot v2

版本 **4.5.1** · 作者 **Boss**

在 Emby 首页展示属性专区与平台榜单，继续使用原媒体 ItemId。4.5.0 把封面生成改为整台 Emby 服务器逐库处理，并用真实海报轮播替代旧版进度条动画。

## 4.5.1 修复

- 原生库旧封面备份使用独立 **64 MiB** 限额，按原始字节保存 GIF，恢复保留全部帧与时长；不再被海报的旧 3 MiB 限额拦截。超限、无法解码或备份失败时保留原图，并显示失败发生在备份阶段。
- 本库海报读取限额为 **16 MiB**，校验像素后缩至最长边 960 像素缓存；原生库、虚拟库和播放端采用同一处理方式。元数据响应另有 16 MiB 限额。
- 预览、历史和逐库结果显示真实输出格式及原因：空库、仅一幅不同海报、取图失败、品牌模式或 GIF 编码失败。历史旧记录不猜测原因，重新生成后补齐诊断。
- **0 部影片的库没有本库海报可轮播**。截图中的伦理、猫眼空库保持静态品牌图；需先有符合既有规则的本库成员。不会混入其他库海报或放宽身份匹配来制造 GIF。

![手机逐库结果示例](docs/previews/studio-output-diagnostics-mobile.png)

## 封面工坊

界面参考 [justzerock/MoviePilot-Plugins](https://github.com/justzerock/MoviePilot-Plugins/tree/main) 的呀哈哈封面工坊与用户提供的截图：深色卡片、蓝色分段导航、大画布、四格方案和分组配置。页面与 Python 绘图代码为本项目实现；来源和字体许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

![工坊实际浏览器截图](docs/previews/studio-server.png)

- **四种封面**：叠影（旋转海报）、光幕（斜切画面）、映墙（多图拼排）、留白（居中标题）。方案缩略图和大画布使用同一个真实绘图器。
- **画布编辑**：主/副标题、自定义文本、独立字体、字号、颜色、位置、海报缩放、模糊与压暗；支持拖动和方向键移动。只显示当前布局适用的控制项。
- **素材**：Emby 横版 Backdrop、竖版 Primary 或本地品牌画面；按最新入库、名称或固定随机种子排序。素材不可用时显示品牌画面及提示。
- **输出**：静态 PNG/JPEG/WebP，尺寸可选 640×360、960×540、1280×720、1920×1080；GIF 轮播最多 6 幅不同的本库海报，每幅停留 1680ms，再用 4 帧共 320ms 渐变过渡，包含末幅到首幅的过渡；最高 960×540。素材少于两幅时返回静态 PNG。高分辨率静态输出由统一画布缩放生成。
- **方案管理**：各虚拟库可独立设置，也可跟随默认方案；最多 20 个自定义方案，支持 JSON 导入/分享。
- **历史封面**：当前库/全部库后台生成、进度、停止、预览、原图下载、恢复方案、删除。默认保留 30 批，可设 1–100 批，图片总量上限 256 MiB；页面展示最近 60 张。
- **字体库**：内置 Noto Sans SC 中文字体；支持 TTF/TTC/OTF/WOFF/WOFF2 文件和公网 HTTPS 直链导入，单文件最多 24 MiB、最多 20 个上传字体。缺失字形回退到内置字体。
- **配置页**：运行/Cron、事件维护、Emby 与专区范围、榜单、默认标题字体、历史保存、配置备份/导入和缓存清理。桌面、390px 手机页面均已实测。

工坊同时支持两类媒体库：

| 目标 | 如何生成和应用 |
|---|---|
| 本插件虚拟媒体库 | 选择已有专区，“应用封面方案”更新 ImageTag，播放器下次请求按方案取材绘制；“生成当前封面”另保存管理端历史 |
| Emby 原生媒体库 | 在“Emby 服务器”中选择服务器，点选对应的媒体库卡片。预览/保存方案/“生成当前封面”不改原库图片；点击“生成并更新原生库封面”才更新所选库的 Primary 图片 |

主选择框以已配置的 Emby 服务器为单位。“预览整台服务器”逐库读取素材但不写入 Emby；“生成并应用整台服务器”逐库生成，先备份再更新该服务器各原生库图片，同时生成当前播放网关服务器的虚拟库历史。点选卡片可调整单库方案，同名库会显示 ID。服务器范围沿用插件已选择的 Emby；不会因封面操作切换播放网关。

两类库共用四种布局、字体、海报优先级、Backdrop→Primary 回退、图片解码与去重。虚拟库保留图片标签并先从全部成员中挑选有图候选；原生库还通过 Emby ImageTypes 过滤寻找靠后的有图条目，避免前排缺图就直接放弃。图片请求仍有数量/时间/大小上限，源站超时或素材确实不可用时显示品牌画面及提示。

原生库更新前重新校验库 ID，并备份当前图片到历史；备份失败、目标失效、鉴权失败或指定海报来源完全无图时不覆盖。历史弹窗中的“恢复原生库图片”可恢复实际旧图，“恢复此方案”仅恢复设计参数。即使关闭常规历史保存，原生库更新前备份仍执行；新图与旧图共用批次。历史总量仍限 256 MiB，大批次也可能淘汰较早记录，需长期保留的原图可从历史下载。原生库封面更新为手动操作，原有 Cron/增量仅继续维护虚拟库。

原生图片接口依据 [Emby 官方上传规范](https://dev.emby.media/reference/RestAPI/ImageService/postItemsByIdImagesByType.html)发送 base64 图片与真实 MIME；不调用媒体库创建/删除、条目修改、媒体源修改等接口。

播放端素材始终使用当前 Emby 用户凭据读取；管理端预览和历史需要 MoviePilot 管理员登录。**虚拟库恢复历史恢复的是设计参数**，播放端重新按当前用户权限取图，不把管理员生成的图片直接公开。无素材时使用静态品牌图。Emby 的图片元素可能不带登录头：经认证的首页请求签发每用户、每库独立的短期 ImageTag 凭证（内存保存一小时，重载失效），只用于读取这一张封面。每次仍核验原用户和可见成员，撤销权限后不返回旧海报；不会把 API Token 写进图片 URL。

静态模式强制静态输出；动态模式遇客户端 PNG/JPEG/WebP 请求自动返回对应静态格式，各格式独立缓存/ETag。Pillow 缺失或 GIF 编码失败时，网关仍有 PNG 回退；完整工坊需要安装绘图依赖。部分电视/手机客户端可能只显示 GIF 首帧。

配置备份 JSON 包含当前插件配置（包括自行填写的 Key/Token），不打包字体和历史图片。导入先载入表单，点击“保存配置”后由 MoviePilot 保存并重新注册定时任务。缓存清理保留上传字体、配置与历史原图。文件保存在 MoviePilot 插件数据目录的 `cover_studio/`。

[原生库界面](docs/previews/studio-native.png) · [原图备份](docs/previews/studio-native-history.png) · [配置页截图](docs/previews/studio-config.png) · [手机截图](docs/previews/studio-mobile.png) · [历史页截图](docs/previews/studio-history.png) · [GIF 演示](cover-preview.gif)

截图和 GIF 使用构造元数据/原创示例图，不包含用户影片。新增诊断截图使用本地固定状态展示空库、单图和备份失败。此前曾对 NAS 做 4.4.1 的只读核验；用户本次截图显示已安装 4.5.0。本次 4.5.1 未连接或改动 NAS。4.3.11 的完整历史说明见 [docs/HISTORY-4.3.11.md](docs/HISTORY-4.3.11.md)。

## 4.3.10 整体识别优化

继承 **4.3.10** 的识别策略。主要目标是减少误收，不把“命中数量增加”当作识别准确。

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
- 混合榜部分子源失败时，成功子源可新增成员，并保留严格规则生成的旧成员；仍清理 Emby 已删除的条目。所有子源恢复后才按完整榜单移除过时成员。全部失败则仅保留可信旧结果。
- 旧版本快照没有严格规则标记，不继续信任。升级后如来源失败，对应专区可能暂时为空；待来源恢复后重新同步。
- 猫眼等网页数据缺少可靠 ID、年份时可能无法匹配。可通过既有自定义 Feed 补充媒体类型、Provider ID 等信息，不按相似标题猜测。

元数据不足或本身错误仍可能造成漏收/误收。可先修正 Emby 元数据或补充属性标签；本包没有新增“待确认”页面，也不承诺识别准确率百分比。

## 端口和原播放链路

| 组件 | 保留配置 |
|---|---|
| 客户端 / NextEmby | 对外 8098 |
| MoviePilot | 前端 3333，API 3334 |
| 原生 Emby | 8096 |
| MP 媒体服务器 | 继续连接 Emby 8096 |
| NextEmby 普通 API 上游 | NAS 局域网 IP:3334 |

普通 API：客户端 → NextEmby:8098 → MoviePilot:3334 → Emby:8096。插件复用 MP API，不新增监听端口。NextEmby 原有 302 规则保留，网关不跟随或改写 Location；不改变 ItemId、MediaSource，不创建 Collection/BoxSet，不移动、复制或重命名原媒体。已经接通该链路的用户无需改地址。

## 上传和升级

本地发布包：`releases/MediaArchiver-v4.5.1.zip`。解压后按原目录上传文件，不要只把 ZIP 放进仓库。

1. **完整覆盖 `plugins.v2/mediaarchiver/`**：包括 `__init__.py`、`coverstudio.py`、`requirements.txt`、`fonts/` 和 **`dist/assets/` 全部文件**。只上传主 Python 文件会缺失工坊组件。
2. 更新根目录 **`package.v2.json`** 的 `MediaArchiver` 条目到 **4.5.1**；仓库有其他插件时保留它们。同步图标、说明、字体许可与校验文件。`frontend/` 是可复现源码，NAS 运行不需要 Node。
3. 刷新 MoviePilot 插件市场并升级，使 MP 安装插件依赖。随后重启 MoviePilot，确保旧网关代码退出。
4. 检查 `http://NAS地址:3334/__mediaarchiver__/health`：`version` 和 `cover_engine` 应为 `4.5.1`，`code_sha256` 对照 `SHA256SUMS` 中主文件；`decoders.br/zstd` 应启用。
5. 打开插件配置保存原有专区设置，执行“一键重建”，再进入封面工坊。客户端继续连接 8098，并刷新媒体库/图片缓存。

新版页面使用 MoviePilot v2 的 Vue 远程组件机制，管理接口校验管理员身份。仍保留旧的原生配置表单/状态页供兼容调用。完整工坊需宿主支持 Vue 插件页面和管理员依赖；本包未在你的实际 MP 镜像中安装验证。

插件运行依赖只添加绘图/字体工具，不固定或替换宿主 FastAPI/HTTPX。`requirements-dev.txt` 仅供独立测试环境，不能安装进运行中的 MoviePilot。

## 同步与已知外部限制

Cron 使用 APScheduler 五段格式，按 MoviePilot 时区运行；建议 `0 4 * * *`。每六小时可用 `0 */6 * * *`，周日 03:30 可用 `30 3 * * sun`。星期推荐英文，数字 0 在 APScheduler 中是周一。新版配置页会拒绝无效 Cron。

原有事件/周期校准、榜单更新、严格识别和分页保留。升级后重建一次，以便旧虚拟库索引补齐图片标签。混合榜部分失败时成功子源可新增，保留可信旧成员，来源完整恢复后再清理。4.5.0 没有修复源站网络问题：豆瓣 `tv_global` 404、IMDb 访问/结构变化、AniList/Bangumi 超时、猫眼只有标题缺少可靠身份仍需分别处理；不会用其他榜单冒充。

## 开发与复现

Python 3.12、Node 20+。在独立虚拟环境执行（Windows 激活 `.venv/Scripts/Activate.ps1`，Linux 使用 `source .venv/bin/activate`）：

```bash
python -m venv .venv
# 激活后执行
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_output_diagnostics.py tests/test_cover_size_limits.py tests/test_server_cover_workflow.py tests/test_native_covers.py tests/test_coverstudio.py tests/test_gateway_runtime.py -q -p no:cacheprovider
python tests/test_virtual_library.py
python tests/test_accuracy.py
python tests/test_animated_cover.py
python tests/test_ranking_failures.py
cd frontend
npm ci
npm run build
npm run build:preview
cd ..
python tests/preview_server.py
```

另一个终端在项目根目录执行 `node tests/ui_smoke.mjs` 和 `node tests/ui_output_diagnostics.mjs`。Windows 自动使用已安装的 Edge；其他环境先在 `frontend/` 执行 `npx playwright install chromium`。测试服务仅监听随机回环端口，使用构造 Emby 数据；关闭终端即可退出，不是插件新增服务。

前端构建输出到插件 `dist/assets/`；`build:preview` 输出到项目父目录 `.work/preview`。本地测试文件和数据不会进入运行组件。

本次 **87 项 pytest、4 个独立回归脚本、21 个浏览器场景**通过。浏览器包括 17 项真实本地 API 流程和 4 项固定诊断响应展示检查。详情见 [TEST_REPORT.md](TEST_REPORT.md)，交接说明见 [HANDOFF.md](HANDOFF.md)。不代表新包已部署或所有播放器、榜单均可用。本包未上传 GitHub、未部署。逐项现场验收见 [ACCEPTANCE.md](ACCEPTANCE.md)。
