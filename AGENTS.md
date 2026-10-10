# 项目接手规则

修改前阅读 HANDOFF.md、README.md 和 TEST_REPORT.md。当前基线5.0.0，作者Boss。

保持 NextEmby8098 → MoviePilot API3334 → Emby8096，不新增监听端口。原媒体文件、ItemId与MediaSource保持不变；仅建立一级虚拟库，禁止Collection/BoxSet模式。

5.0.0按用户要求移除所有媒体库封面功能；使用MoviePilot原生Vuetify配置/功能页。native_ui.py负责宿主结构，catalog.py负责用户授权后的计数分页，__init__.py负责识别、同步和透明网关。发布包含三个模块与requirements.txt。旧coverstudio、fonts、dist仅在Git历史，不恢复到当前运行路径。原生Emby图片与用户数据目录中的备份/字体保留。

管理API只允许MoviePilot管理员；浏览每次使用当前Emby用户凭据校验。权限过滤后分页、计数，不缓存跨用户权限；使用/Users/{Id}，现有Emby不支持/Users/Me。保留搜索、排序、用户过滤、原媒体对象与302。

同步固定批次配置，短锁发布完整成员/精简索引，生命周期核验与发布使用同一锁。旧任务不能写新配置状态；失败保留成功快照。榜单故障明确报告，不改名或混源掩盖。

功能新增提升版本并同步索引与说明。修改后运行scripts/check.py、相关浏览器回归，更新TEST_REPORT与SHA256SUMS并用release.py打包。frontend只用于宿主QA，npm ci && npm run build输出.work；preview_server.py只使用随机回环端口。测试数据/截图在.work，不覆盖docs历史预览。

NAS仅已有只读授权，未经明确授权不部署；无现场验证不能宣称已部署。交接/发布包不得保存账号、密码或Token。archive/original_uploads只供追溯，不覆盖当前虚拟库实现。
