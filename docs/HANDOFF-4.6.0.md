# 媒体虚拟库交接 · 4.6.0

作者 Boss，2026-09-13。项目无 Git 元数据。基线为已交付的4.5.2，当前主模块、封面引擎、市场索引和前端统一为4.6.0。用户要求参考 GitHub 丰富封面模板，不再扩展同质的叠卡/斜墙。

## 本次实现

- 保留叠影、光幕、映墙、留白，新增银幕、胶片、三联、刊物、黑胶、拼色，共10款真实 Pillow 构图。参考项目及许可核查见 THIRD_PARTY_NOTICES.md，未复制第三方源码或海报。
- PRESETS 统一返回 defaults、hidden_fields、fixed_layout、artwork_count。选择内置方案合并构图/配色预设，保留标题内容、字体、素材来源、排序、分辨率和动画开关；自定义方案仍完整恢复参数。已有配置不会自动重置，部分参数接口保持原有合并语义。
- 新增5款固定构图仅显示适用编辑控件；银幕仍可移动标题。方案缩略图为原创几何演示，实际生成只使用当前库的授权素材。样图异常/缺绘图依赖时，状态接口仍可返回配置和空缩略图。
- 刊物、黑胶、拼色长标题优先两行，最小28基准像素（用户主动设24时遵从24），按实际高度放置副标题与计数。连续英文和ID按词换行，超长内容省略；旧4款文字逻辑保持。
- 静态胶片最多取6张本库图片，三联/刊物/拼色最多3张；所有动态模式继续最多6张不同本库海报轮播。0/1张仍诚实返回PNG及原因。
- 桌面与手机方案网格可滚动，手机两列且不压缩卡片文字。保留批量当前/已保存方案、全选反选、逐库草稿和历史清理。

## 既有边界

保持8098→MoviePilot API3334→Emby8096，MP前端3333。不增加插件监听端口，不操作原媒体文件，不改ItemId/MediaSource/302 Location，不恢复Collection/BoxSet。

原生图片只在明确发布时重新核验库ID、备份后上传。Cron/增量不发布原生图片。取材权限、ImageTag、缓存、真实GIF、原图64MiB/素材16MiB/元数据16MiB和历史256MiB限额保留。历史清空仍含原生备份，并保留Emby已应用图片、方案和字体。

## 测试及交付

137项不重复pytest（旧117项及新增20项）、4个独立脚本、39个浏览器场景通过。新增文件tests/test_template_catalog.py、tests/render_template_gallery.py、tests/ui_templates.mjs。长标题和缺依赖修正后又运行43项相关测试。详细证据见TEST_REPORT.md与docs/BUILDINFO.json。

主模块对比4.5.2发布包，统一换行和版本后源代码完全相同。播放、识别、同步、权限分发没有业务改动。新模板通过真实本地Emby接口验证原生与虚拟批量输出及未选库保留。

发布包releases/MediaArchiver-v4.6.0.zip须完整覆盖plugins.v2/mediaarchiver/（含fonts、requirements、dist/assets）及根package.v2.json的MediaArchiver条目；其他插件条目保留。包内SHA256SUMS逐文件验证，NAS不需要Node。health的version与cover_engine应均为4.6.0。

仅本地开发、回环测试与打包，没有连接或改动NAS，没有上传GitHub、生成远程封面、重启或部署。旧NAS只读授权不等于部署授权。账号、密码、Token不写项目、包或记忆。4.5.2旧报告已保存在docs/HANDOFF-4.5.2.md与docs/TEST_REPORT-4.5.2.md。
