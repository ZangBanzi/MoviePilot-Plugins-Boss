# MediaArchiver 4.6.0 验证记录

日期：2026-09-13，作者Boss。升级基线为4.5.2，需求是参考GitHub增加不同构图的封面模板。

## 结果

**137项不重复pytest、4个独立脚本、39个Edge浏览器场景通过。** 原有117项完整后端回归通过（56.77秒）；新增20项模板回归及原23项工坊测试在最终两行排版和缺Pillow回退修正后联合通过，43项用时12.31秒。

浏览器39项包括34项真实回环API流程和5项固定响应展示；errors均为空，真实API的apiErrors均为空。正式federation和preview构建成功。所有图片/接口写入都在临时测试目录或本地Emby替身完成，未连接或改动NAS，未部署。

## 新增模板验证

| 范围 | 实际验证 |
|---|---|
| 目录与预览 | 10个不同ID，均为可解码320×180 JPEG缩略图；不读取用户影片来构造方案示例；旧4款缺省参数与原默认值完全一致 |
| 六款新构图 | 银幕、胶片、三联、刊物、黑胶、拼色分别输出1280×720 PNG及真实GIF；图像内容大区域改变，非微小进度条 |
| GIF | 每款用3张不同素材得到15帧、960×540、loop=0、6秒总时长，三段停留及渐变循环；沿用原共享调色板编码 |
| 空库/单图 | 六款各验证0张和1张素材均为可解码PNG，actual_format及empty_library/single_artwork原因准确 |
| 配置兼容 | 十款完整参数单库保存、自定义方案和配置备份校验回读后相同；用户颜色和位置值不被静默重置；未知ID拒绝 |
| 原生/虚拟批量 | 银幕与刊物各通过真实本地两库批量：原生备份后base64 POST，虚拟历史字节与同方案绘图一致，未选原生图片保持原样 |
| 长标题 | 刊物、黑胶、拼色实际Pillow绘制记录验证两行、字号至少28、Netflix/TMDB完整、字形框与副标题/计数无重叠且不出画布 |
| 缺少依赖 | 模拟所有Pillow导入失败，状态接口仍返回10款目录和配置，缩略图为空，避免工坊配置页整体打不开 |
| 控件 | 预设defaults只合并构图参数；保留素材、字体、输出设置；固定布局隐藏无效位置控件，银幕保留标题拖动 |
| 手机 | 390px两列可滚动方案网格，卡片不被固定高度压扁，说明文字完整，选中项可点击，页面无横向溢出 |

## 后端套件

| 文件 | 项数 |
|---|---:|
| test_template_catalog.py | 20 |
| test_batch_selection.py | 15 |
| test_history_cleanup.py | 15 |
| test_output_diagnostics.py | 13 |
| test_cover_size_limits.py | 6 |
| test_server_cover_workflow.py | 9 |
| test_native_covers.py | 10 |
| test_coverstudio.py | 23 |
| test_gateway_runtime.py | 26 |

四个独立脚本test_virtual_library.py、test_accuracy.py、test_animated_cover.py、test_ranking_failures.py全部通过。保留识别、增量/Cron、分页、权限、历史清理、大图备份与静态回退测试。

对比4.5.2 ZIP，主模块在统一换行并替换版本字符串后源代码完全一致；主业务方法只涉及3处User-Agent字符串及plugin_version升级，没有播放、ItemId、MediaSource、302或鉴权改动。

## 浏览器与视觉

- ui_templates.mjs新增8项真实API场景：10款点击和实际预览；浅色切回旧款的配色；适用控件；自定义JSON导入导出；原生/虚拟独立草稿；银幕批量实际发布；真实GIF；390px可滚动选择。
- ui_smoke.mjs原17项真实API场景通过：federation宿主共享Vue和编译资源，画布/字体/方案/配置/历史、原生发布/恢复、逐库批量、移动页面与GIF。
- ui_selection.mjs原10项通过，其中9项真实API、1项固定响应：选择范围、草稿、统一/已保存模式、历史单删与全删、失败重试。
- ui_output_diagnostics.mjs原4项固定诊断响应通过：空库、单图、实际MIME、原生失败阶段、旧记录提示和手机文字。

已目视真实renderer输出的十款总览、长标题对照、桌面和手机页面。新增素材为原创程序风景图，未使用用户影片或第三方海报。窄栏长标题改为两行；连续英文保持完整；手机卡片自然高度修正后重测。源码与发布资源清单见SHA256SUMS，构建版本和哈希见docs/BUILDINFO.json。

## 复现

```bash
python -m pytest tests/test_template_catalog.py tests/test_batch_selection.py tests/test_history_cleanup.py tests/test_output_diagnostics.py tests/test_cover_size_limits.py tests/test_server_cover_workflow.py tests/test_native_covers.py tests/test_coverstudio.py tests/test_gateway_runtime.py -q -p no:cacheprovider
python tests/test_virtual_library.py
python tests/test_accuracy.py
python tests/test_animated_cover.py
python tests/test_ranking_failures.py
python tests/render_template_gallery.py
cd frontend
npm ci
npm run build
npm run build:preview
cd ..
python tests/preview_server.py
# 另一个终端；每个真实写入套件使用重新启动的干净preview_server
node tests/ui_templates.mjs
node tests/ui_smoke.mjs
node tests/ui_selection.mjs
node tests/ui_output_diagnostics.mjs
```

Windows、Python3.12.14、Pillow12.3.0、fonttools4.65、HTTPX0.28.1、FastAPI0.115.14、Node24.19.0、Vite5.4.21、Edge153.0.4234.32。QA仅随机回环端口，插件没有增加监听器。

## 交付边界

新包未部署；用户现场验收见ACCEPTANCE.md。保留8098→3334→8096及原302。至少两幅不同本库海报才输出GIF，空库不混入示例或其他库素材。客户端请求静态格式或不支持动画时仍可能显示静态图/首帧；不把本地测试等同NAS客户端兼容性保证。模板数量增加不改变榜单可用性或严格匹配规则。
