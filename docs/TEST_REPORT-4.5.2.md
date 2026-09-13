# MediaArchiver 4.5.2 验证记录

日期：2026-09-13。作者：Boss。修复基线为已交付的4.5.1。

## 结果

**117项pytest通过（54.76秒），4个独立脚本通过，31个Edge浏览器场景通过。** 浏览器共26项真实回环API操作、5项固定响应展示检查；最终页面errors=[]，真实API操作apiErrors=[]。Vue正式federation与preview构建成功。

本次没有连接或改动NAS、上传GitHub或部署。原生图片发布/恢复、配置变更和历史清理均在本地独立测试目录与回环Emby实例完成。

## 本次问题与证据

| 需求/边界 | 验证结果 |
|---|---|
| 选择光幕后批量仍叠影 | 新接口对全部选中库一次保存当前参数，任务固定逐库方案；选中原生和虚拟的真实输出字节均与光幕绘图一致，历史options一致 |
| 全选、反选、单独选择 | 浏览器操作实际checkbox；反选至0时批量预览/生成禁用，刷新保留选择；编辑按钮与勾选分开 |
| 未选库与服务器范围 | 未选原生图片、虚拟库ImageTag/设置保持不变；空/畸形/失效/跨服务器列表在任何配置写入或POST前拒绝；重复keys只处理一次 |
| 使用各库已保存方案 | 不传统一options，每库按开始时的已保存参数生成；草稿不会自动保存，明确保存后再用于该模式 |
| 任务中配置变化 | 当前方案和逐库已保存模式均固定开始时的参数，不在处理中途跳回默认样式 |
| 单库当前方案 | 单库生成直接提交options，即使应用范围选“默认方案”，本次仍按当前草稿生成并保存该库覆盖 |
| 切换编辑 | 切换媒体库后再返回保留未保存标题/布局，刷新媒体库也不丢失；页面草稿不冒充服务器已保存方案 |
| 历史卡片删除 | 只删所选记录及其图片，其他记录/已应用封面/方案保留 |
| 全部历史清理 | 后端构造73条、页面仅显示60条，仍清理完整索引；含旧原生备份。只触及history下已校验UUID的.image/.jpg，保留字体/配置备份/无关文件 |
| 清理与生成并发 | job_lock互斥，活动生成/原图恢复期间拒绝清理，任务结束后不会把已删记录写回 |
| 文件系统故障 | 索引写失败不删文件；部分unlink失败保留可重试记录；索引恢复失败留下的残留可再次全量清理；空索引仍有“清理历史残留”入口 |
| 权限和播放 | 原生更新仍先复核ID并备份再POST；历史清理不调用Emby；原302、ItemId/MediaSource及用户权限回归通过 |

## 后端套件

| 文件 | 项数 | 范围 |
|---|---:|---|
| test_batch_selection.py | 15 | 真实光幕输出/选择范围/配置事务边界/单库方案/快照/跨服务器/失败解锁 |
| test_history_cleanup.py | 15 | 超过60条清空/单张/确认/路径与文件别名/孤立图片/互斥/故障恢复/不改Emby |
| test_output_diagnostics.py | 13 | 空库、单图、重复、坏图、品牌、GIF/静态原因及历史/任务计数 |
| test_cover_size_limits.py | 6 | 超过3MiB原GIF精确备份恢复、大海报三条取材链路、限额与大元数据 |
| test_server_cover_workflow.py | 9 | 整台服务器、第二服务器、ImageTag权限、同步取消、冷封面与302、刷新并发 |
| test_native_covers.py | 10 | 每库素材、原生备份/发布/恢复、鉴权/磁盘/无图保留、播放联合验证 |
| test_coverstudio.py | 23 | 四布局动画、格式/分辨率、字体、缓存/配置/历史、管理员和用户隔离 |
| test_gateway_runtime.py | 26 | FastAPI/HTTPX、压缩、Cookie/Range/302、32并发、WebSocket、同步、图片回退 |

完整后端通过后补充了残留清理数量文案，相关15项清理测试再次通过（1.94秒）。四个独立脚本test_virtual_library.py、test_accuracy.py、test_animated_cover.py、test_ranking_failures.py全部通过。

对比4.5.1 ZIP，主文件141个方法中138个AST完全相同；3个方法只更新User-Agent版本。没有新增或移除主文件方法，播放、权限分发、识别、同步代码未改动。

## 浏览器与复审

- 原有17项真实API流程通过：画布/预设/布局、方案保存与导入导出、历史下载/恢复、字体/配置/缓存、federation、原生发布与恢复、服务器逐库流程、手机、GIF播放暂停。
- 原有4项固定诊断响应场景通过：空库/单图原因、真实MIME与旧记录、逐库失败阶段/计数、390px无横向溢出。
- 新增9项真实API流程通过：全反选和单勾、草稿切换/刷新、所选方案只读预览、两库光幕且未选不变、已保存模式不自动保存草稿、单库直接使用草稿、手机勾选、卡片删除、全部清空。
- 新增1项固定响应场景：界面显示65条总数而非可见卡片数，文件删除失败保留卡片并提示，索引为0仍可发起残留清理。

复审发现并修正了两处问题：已保存模式先单独保存草稿会在其他目标失效时造成部分配置写入，现改为严格使用已保存方案；空索引禁用清理按钮会阻止残留重试，现允许明确清理残留。另外修正新控件的可访问标签、当前服务器首库选择、卡片显示的真实将生成方案。

最终正式资源为Studio-ntL7o6KT.js及Studio-DcGndFoB.css，remoteEntry指向完整构建资源。截图docs/previews/studio-selection.png、studio-selection-mobile.png、studio-history-cleanup.png使用构造内容和真实回环操作；已检查390px无横向溢出。

## 复现

```bash
python -m pytest tests/test_batch_selection.py tests/test_history_cleanup.py tests/test_output_diagnostics.py tests/test_cover_size_limits.py tests/test_server_cover_workflow.py tests/test_native_covers.py tests/test_coverstudio.py tests/test_gateway_runtime.py -q -p no:cacheprovider
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
# 另一个终端
node tests/ui_smoke.mjs
node tests/ui_output_diagnostics.mjs
# 关闭并重新启动干净的preview_server后执行
node tests/ui_selection.mjs
```

环境：Windows、Python3.12.14、Pillow12.3.0、fonttools4.65、HTTPX0.28.1、FastAPI0.115.14、Node24.19.0、Vite5.4.21、Edge153.0.4234.32。QA只监听随机回环端口，插件没有新增端口。

## 交付边界

包内有主模块、coverstudio、字体/许可、全部编译前端、索引、源码、测试和文档，SHA256SUMS逐文件校验。需完整覆盖插件目录与市场索引，health的version和cover_engine均应为4.5.2。

保留8098→3334→8096、原ItemId/MediaSource/302、大图处理及静态回退。清空历史也会删除其中的原生旧图备份，执行前有明确确认，Emby现有图片不变。256MiB历史总量限制仍存在；空库/单图不会伪造GIF。新包未部署，现场步骤见ACCEPTANCE.md。
