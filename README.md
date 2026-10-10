# 媒体虚拟库 · MoviePilot v2

版本 **5.0.0** · 作者 **Boss**

将 Emby 已入库影片按媒体属性或榜单显示为首页一级虚拟库。影片继续使用原 ItemId 与 MediaSource，播放沿用 NextEmby 8098 → MoviePilot API3334 → Emby8096 和原 302 链路。

## 5.0.0 重写

按用户要求删除媒体库封面功能：封面工坊、生成/预览、GIF、字体、封面历史操作及原生图片写入接口均已移除。插件没有绘图依赖，也不再提供远程 Vue 组件。功能页与配置页使用 MoviePilot 内置 Vuetify 渲染，避开原来的“组件加载错误”路径。

功能页提供连接测试、一键重建、刷新进度、专区计数、真实榜单来源状态和运行日志。配置页管理服务器、属性专区、平台榜单、增量维护、Cron 与连接参数。保存由 MoviePilot 原生 PUT 接口完成。

- 播放端每次使用当前用户凭据检查 Emby `/Users/{Id}` 与成员可见性，权限过滤之后才计数、分页；身份不符与登录失效保留真实错误。空库和无权限库不展示。
- 全库权限查询每批最多200个 ID；只保留 ID，当前页才读取完整条目。没有跨用户权限缓存。大库翻页会产生多次 Emby 读取，以保证撤权后不继续暴露旧成员。
- 同步先完成全部快照计算，再在短锁内发布成员与精简索引。扫描/构建失败保留原快照；停止或重载使旧任务失效。线程启动失败释放任务锁，可重试。
- 批次固定规则、榜单与服务器选项；过期任务失败不会覆盖新状态。重启后恢复精简元数据，搜索、排序无需等下一次扫描。
- 旧持久数据做结构校验，损坏记录不会拖垮功能页。筛选缓存最多64项、合计250000个 ID 引用；身份权限每次重查。

## 使用

1. MoviePilot 媒体服务器连接原生 Emby8096，选择服务器与专区并保存。
2. NextEmby 上游保持指向 MoviePilot API3334，客户端继续访问8098。
3. 在功能页测试连接、点击一键重建，随后点击刷新进度。
4. Cron 使用 MoviePilot 时区；默认每天04:00。增量校准复用榜单缓存，定时/手动全量更新刷新来源。

只维护虚拟成员，不创建 Collection/BoxSet，不移动、复制或重命名媒体。管理员管理接口与当前 Emby 用户播放权限分别校验，页面不包含宿主 API Token。未配置 UserId 的根级虚拟查询返回明确错误；客户端需传 UserId 或认证头中的 UserId。

## 识别与榜单

Remux、4K、Dolby Vision、HDR、Atmos 按单个媒体版本的实际参数优先识别；TVB 使用明确厂牌/标签，伦理使用明确情色题材或手工标签。文件名不会覆盖已知冲突参数。手工标签 `虚拟库:remux`、`排除remux`、`伦理`、`排除伦理` 在下一次同步生效。

外部榜单使用类型与 Provider ID，或类型/年份/完整标题唯一候选匹配；身份冲突不按同名猜测。TMDB 可播精选不代表平台官方热榜。源站失败明确展示，保留严格识别产生的可信旧成员，不换源冒充成功。部分源失败与完全空榜分别处理。

## 更新与验证

安装时完整替换 `plugins.v2/mediaarchiver/` 代码目录，市场索引版本为5.0.0。旧 `coverstudio.py`、`cache.py`、`fonts/`、`dist/` 已不属于发布文件。仅删除旧插件代码与构建产物，保留 MoviePilot 插件数据目录内的历史备份、字体和已应用 Emby 图片；此版不提供旧图恢复按钮，历史数据仍可自行保留或回退旧版处理。

ZIP 是项目完整发布包，解压后按目录安装，不能直接当市场插件 ZIP 上传。部署后重新载入插件并重开功能页；客户端若缓存旧虚拟图片标签需刷新首页。

```sh
python -m pip install -r requirements-dev.txt
python scripts/check.py
cd frontend
npm ci
npm run build
npx playwright install chromium
cd ..
python scripts/check_ui.py
python scripts/release.py
python scripts/release.py --check
```

前端仅用于本地原生宿主测试，构建输出 `.work/preview`，不会随插件安装；运行无需Node。`check.py`执行pytest与三组隔离旧回归，`check_ui.py`启动随机回环QA，`release.py`同步SHA256SUMS并生成完整包。

实际验证见 TEST_REPORT.md；交接见 HANDOFF.md，现场步骤见 ACCEPTANCE.md。4.6.2封面历史说明存于docs。此版尚未在用户NAS部署或现场验证。
