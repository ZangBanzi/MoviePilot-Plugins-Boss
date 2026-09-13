# 媒体虚拟库项目交接

## 当前基线

版本 **4.4.1**，作者 Boss，日期 2026-09-13。由迁移包的 4.3.11 同版本榜单修复版升级；修改前原有 13 条 SHA256SUMS 全部核对一致。工作目录没有 Git 元数据/远程历史，未推送 GitHub，也未连接 NAS。

用户仓库：https://github.com/ZangBanzi/MoviePilot-Plugins-Boss 。参考 UI：https://github.com/justzerock/MoviePilot-Plugins/tree/main 。用户要求尽力模仿其封面工坊和配置页，完成代码并先测试，再交付用户上传。

已阅读用户提供的旧任务预览，未取得完整聊天逐字记录；不能把此前附件引用视为当前工作区以外的已验证代码。

## 本次交付

- 主程序 `plugins.v2/mediaarchiver/__init__.py`：版本、宿主 Vue 接入、管理员工坊 API、配置校验、按当前 Emby 用户取图及缓存隔离。
- 新增 `coverstudio.py`：四种 Pillow 布局、动态/静态、上传字体、预览、独立方案、后台批量生成、历史、备份与缓存操作。
- 新增 `frontend/` 原创 Vue 源码、锁文件，以及 `dist/assets/` 已编译生产远程组件。运行不需要 Node。
- 内置 `fonts/NotoSansSC.ttf` 和 OFL 许可；新增 fonttools 依赖。
- 完整发布包、截图、GIF 演示、回归脚本、版本索引和 SHA256SUMS 已同步。发布时必须上传完整插件目录，不能仅覆盖主文件。

## 不变的架构边界

客户端 → NextEmby8098 → MoviePilot API3334 → Emby8096；MP 前端3333。用户既有容器为 moviepilot-v2（host）、nextemby、emby-sa、nextfind 等；这些是迁移记录中的环境，不是本次现场核验结果。

MP 读取其已配置的原生 Emby，提供首页一级虚拟专区；不增加插件监听器，不改原 ItemId/MediaSource/302，不创建 Collection/BoxSet，不修改真实影片、网盘或 Symedia 文件。属性识别、严格榜单匹配、Cron/事件校准、分页和流式代理保持原实现。

## 封面与权限

工坊支持本插件虚拟专区与网关所用 Emby 的原生库；原生库通过“读取原生媒体库”单独加载。预览/保存/普通生成只读 Emby，只有明确的原生更新/恢复图片操作写入已核验库 ID 的 Primary 图片，更新前备份原图。原生操作不创建库、不改影片条目和媒体源；顶部批量生成仍只针对虚拟库。配置中每库 override 优先于 defaults；应用后切换 ImageTag。静态 PNG/JPEG/WebP 与 GIF 分开缓存，生成中使用冻结的参数/标签快照。

管理页预览/历史使用管理员范围且需要 MP 管理员身份；播放端用入站 Emby 凭据验证用户并读取其可见条目/图片，不复用管理端海报缓存。凭据撤销后在返回缓存/304 前重新确认权限。无可用素材时回退品牌画面。

虚拟库历史恢复的是方案参数，播放端仍按当前用户权限取材；原生库另有实际旧图片恢复入口。历史默认30批、最多256 MiB，页面最近60张；新索引写成功后才清理淘汰图片。生成只允许一个任务，可取消；生成期间阻止改生效配置/恢复方案。

字体文件最大24 MiB，允许20个，格式由 fonttools/Pillow 实际解码。公网 HTTPS 字体导入校验地址并固定解析 IP，不跟随跳转。备份 JSON 只含配置（含已填写凭据），不含字体和历史图片；导入先放入表单，MP 的 save 事件完成保存和重初始化。

## 验证与交付状态

本次59项 pytest、4个独立回归脚本、13个 Edge 浏览器场景通过，详情和命令见 TEST_REPORT/README。浏览器实际检查1440px桌面和390px手机；还通过 MP 风格资源路径加载生产 federation Page，确认共享 Vue 和 CSS/JS 路径；原生更新/恢复经过独立回环 Emby HTTP 实例。与 4.3.11 比较，10个原播放、转发、分页、识别和调度方法 AST 一致，网关分发只换虚拟图片分支。

测试服务 tests/preview_server.py 仅用随机回环端口和构造数据，不能带入运行架构。用户 NAS 实际 MP 宿主生命周期、Emby海报、NextEmby链路与电视/手机GIF仍需上传后验收；没有吞吐、兼容率或识别准确率承诺。

## 继承的外部限制

- 豆瓣 tv_global 404 未找到可靠同义替代。
- IMDb 访问/结构、AniList 与 Bangumi 超时不保证恢复。
- 猫眼仅有标题时不放宽身份匹配，可靠 Feed 可补充类型/ID/年份。
- 混合榜部分成功期间会保留仍在 Emby 的可信旧成员，完整恢复后清理。

## 下次接手

先读 AGENTS、README、TEST_REPORT 并校验 SHA256SUMS。新功能继续升版并同步索引/说明；旧 archive/original_uploads、4.3.10/4.3.11 ZIP 仅供追溯。历史说明保存在 docs/HISTORY-4.3.11.md 与 docs/TEST_REPORT-4.3.11.md。

本任务授权本地实现、测试和打包，未授权自动推送/部署；用户自行上传。健康接口需同时核对 version 和 code_sha256，不能只看 ok=true。

## 4.4.1 本次修正重点

- Emby 图片字段不是 Fields 枚举，改用 EnableImages=true，附加 Fields 仅用 DateCreated/SortName。
- 虚拟成员紧凑索引保留 ImageTags/BackdropImageTags，先全成员优选有图项，再限制请求规模。
- 播放端候选缺图/坏图继续尝试，Backdrop失败取Primary，重复图去重；当前用户身份与可见成员在缓存前确认。
- 图片字节也参与封面缓存范围，恢复素材后不会永久复用先前品牌回退。
- 原生/虚拟都有后排海报匹配测试，原生发布失败不会误报成功；原图与新图同批次保留。
- 原生能力不在定时/同步回调中自动运行，不会因升级或读取页面覆盖原生库封面。

验收步骤见 ACCEPTANCE.md，4.4.1 ZIP 是最新交付。旧4.4.0包只支持虚拟库，不再建议上传。
