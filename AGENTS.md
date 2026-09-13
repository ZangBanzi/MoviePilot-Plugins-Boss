# 项目接手规则

先阅读 HANDOFF.md、README.md 和 TEST_REPORT.md，再修改源代码。
当前有效基线是 plugins.v2/mediaarchiver/__init__.py，版本4.5.0。
archive/original_uploads 是过时的实体归档附件，只供追溯，不得覆盖当前虚拟库实现。

保持 NextEmby 对外8098、MoviePilot API3334、Emby原生8096链路。
不得新增监听端口、移动/复制/重命名原媒体、改原ItemId/MediaSource或恢复Collection/BoxSet模式。
保留一级虚拟库、识别、增量/Cron、动态GIF及静态回退、权限隔离、分页、302。
作者使用Boss。
新增功能应提升版本并同步索引/说明；用户明确要求同版本修复时以该次要求为准。
修改后运行相关测试，更新TEST_REPORT和SHA256SUMS；无NAS访问时不得宣称已部署或现场验证。
不要把日志里的源站故障通过改名或混用其他榜单掩盖。

4.4.0 新增 coverstudio.py、fonts/、dist/assets/；发布时必须完整包含插件目录。
前端源码在 frontend/，npm ci && npm run build；仅开发时需要 Node。
工坊API只允许MoviePilot管理员；播放端海报只能用当前Emby用户凭据读取，不能复用管理员生成的历史图片。
本地浏览器测试可使用 tests/preview_server.py 的随机回环端口，不能给插件增加独立监听器。

4.4.1 支持原生与虚拟库封面。原生发布必须重新校验库ID、成功备份旧Primary图后才写图片接口；不允许按同名猜库。自动同步和全虚拟库生成不得覆盖原生封面。新增 tests/test_native_covers.py 联合检查素材、备份、权限及原302。

4.5.0 主选择框以服务器为单位；用户明确点击整台服务器生成时，逐个备份并更新原生库，同时生成当前网关的虚拟库。Cron/同步仍不发布原生图片。各库必须只取本库素材，动画必须轮播不同海报，不以进度条或抖动代替。播放端短期 ImageTag 凭证必须绑定用户和库，并逐次重新验证权限；不能复用管理端图片。已获 NAS 只读检查授权，未获远程部署授权；不要在交接或发布包中保存账号、密码、Token。
