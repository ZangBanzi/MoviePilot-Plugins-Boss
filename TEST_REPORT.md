# 5.0.0 验证报告

2026-10-10，作者Boss。本机隔离Python3.12、FastAPI/HTTPX与随机回环Emby替身；没有连接NAS或真实榜单源。

## 原因与范围

用户反馈“组件加载错误”。4.6.2使用远程Vue组件，功能页同时依赖封面工坊；未拿到现场MoviePilot版本、网络日志和组件响应，无法区分资源鉴权、缓存、安装缺文件或宿主共享Vue问题。5.0.0按用户要求移除封面，改用MoviePilot内置Vuetify渲染，消除整个远程组件加载路径，不声称复现了现场具体加载异常。

## 回归

- 首批9项新回归在4.6.2全部失败：原生页面/无封面依赖、损坏持久状态、失败状态可读取、线程启动锁释放、精简索引恢复、服务器范围恢复、构建失败保留快照、生命周期失效禁止发布、无生成图片标签。5.0.0均通过。
- pytest覆盖当前用户权限过滤后分页、受限用户总数、匿名/失效凭据拒绝、用户范围不符、无权详情404、空库授权、配置重载后旧任务失败、管理接口管理员依赖、不嵌入宿主Token，以及已有压缩恢复、KeepAlive并发、Cookie隔离、流式首块/断连、HEAD、WebSocket、原ItemId/MediaSources与302。
- 三组隔离旧脚本保留识别冲突、多版本参数、TVB/伦理、Provider身份、严格榜单源失败/部分恢复、Cron/增量、3334路由、Views/Items/Latest/分页与302。
- 封面专属测试随功能移除；有价值的元数据限额、异常分页与排名回归保留。新版本测试数不能与175项封面版本直接比较。

## 原生宿主浏览器

已对照MoviePilot-Frontend v2 PluginDataDialog、PluginConfigDialog、PageRender、FormRender官方结构；本地宿主替身使用真实Vue3/Vuetify3，不加载远程插件组件。

完整浏览器流程通过：打开功能/配置、后台重建、刷新结果、测试连接、PUT保存Cron并重载、平台/榜单展开、503加载失败重试、桌面与390px手机页面无横向溢出、无JavaScript异常。截图在.work/virtual-native-desktop.png、virtual-native-mobile.png、virtual-native-config-mobile.png。

Node24的npm ci及Vite构建通过，依赖安装报告0漏洞。Vite提示QA包较大；仅本地测试使用，插件运行和安装不包含该包。构建不会生成remoteEntry或写入插件dist。

## 发布边界

最终后端检查：42项pytest通过，三组隔离旧回归通过。SHA256SUMS与ZIP由scripts/release.py生成并复核。插件目录仅__init__.py、catalog.py、native_ui.py和requirements.txt。无需Pillow、fonttools、内置字体或Node运行时。

未执行NAS部署、MoviePilot重启、真实Emby图片修改或现场播放器测试。NAS现场仍需检查插件重载、8098首页、用户撤权、Cron以及真实302播放。大库每次权限查询按200个ID分批，比缓存管理员计数有更多上游请求；不提供未经测量的性能提升比例。
