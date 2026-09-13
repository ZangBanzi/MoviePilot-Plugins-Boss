# MediaArchiver 4.5.1 验证记录

日期：2026-09-13。作者：Boss。修复基线为已交付的 4.5.0。

## 结论

**87 项 pytest 通过（42.35 秒），4 个独立回归脚本通过，21 个 Edge 浏览器场景通过。** 浏览器包括 17 项真实回环 API 操作和 4 项固定诊断响应展示检查；最终页面 errors=[]，真实 API 流程 apiErrors=[]。Vue federation 正式构建与预览构建成功。

本次未连接 NAS、未上传 GitHub、未部署。全部原生图片写入与恢复发生在本地随机回环端口的 Emby 测试实例中。用户截图显示 4.5.0，不据此宣称新版本已现场验证。

## 故障与验证证据

| 情况 | 实现与验证 |
|---|---|
| 旧封面 GET 被通用 3 MiB 限制拦住 | 单独使用 64 MiB 原图备份上限；以真实超过 3 MiB、960×540、6 帧 GIF 复现，上传请求到达上游前检查备份已经落盘 |
| 恢复大 GIF | 实际 base64 图片 POST 后原图字节完全一致，6 帧、循环参数及每帧 300/400/500/600/700/800ms 时长一致 |
| 大海报被丢弃后仅剩静态图 | 原生、虚拟管理端与播放端分别用超过 3 MiB 的 PNG 和第二幅海报生成真实多帧 GIF；缓存最长边不超过 960，文件小于 3 MiB |
| 超限备份 | 测试中降低备份限额，确认失败发生在 POST 前，原图保持不变，结果明确指出备份阶段；不制造 65 MiB 测试文件 |
| 元数据超过 3 MiB | 本地响应包含超过 3 MiB 的附加元数据，仍正确读出成员并生成 GIF，响应受独立 16 MiB 上限约束 |
| 空库、单图、重复图 | 根据实际解码去重后的素材数返回 PNG 和准确原因，不伪造轮播 |
| 本库有成员但海报不可用 | 与空库分开显示 no_artwork，提示检查海报、访问权限及连接 |
| GIF 编码失败 | 强制真实 Pillow GIF save 失败，确认 PNG 可解码，encoder_error 与实际 MIME 一致，任务统计为静态输出 |
| 历史和运行状态 | 每库保存 render_info，GIF/静态/失败计数与实际字节一致；备份失败有独立失败结果 |

## 后端覆盖

| 文件 | 数量 | 范围 |
|---|---:|---|
| test_cover_size_limits.py | 6 | 大原图精确备份/恢复、三条大海报取材链路、超限保留原图、大元数据 |
| test_output_diagnostics.py | 13 | 空库/单图/重复/坏图/品牌/静态方案/真实 GIF、预览静态与播放区分、编码失败、历史与任务计数、失败阶段 |
| test_server_cover_workflow.py | 9 | 整台服务器、第二服务器、ImageTag 权限/撤销/到期、压缩、同步取消、冷封面与原 302 并行、原生刷新并发 |
| test_native_covers.py | 10 | 每库取材、同名 ID、真实图片发布/备份/恢复、鉴权/磁盘/无图失败、原 MediaSource/302 |
| test_coverstudio.py | 23 | 四布局真实海报轮播、格式/分辨率、字体、缓存/ETag、配置/历史、管理员及用户权限 |
| test_gateway_runtime.py | 26 | FastAPI/HTTPX、压缩、Cookie 隔离、Range/302、32 并发、WebSocket、同步不阻塞、图像失败回退 |

4 个独立脚本：test_virtual_library.py、test_accuracy.py、test_animated_cover.py、test_ranking_failures.py 均通过，覆盖原 ItemId、分页/Latest、Cron/增量、严格属性和榜单匹配、混合榜部分更新/旧成员保留/恢复清理。

独立对比 4.5.0 ZIP：主文件 141 个方法中 137 个 AST 相同，3 个仅 User-Agent 版本变化，1 个为 _studio_gateway_cover 的海报限额和缓存缩小。播放/302、权限分发、同步、识别方法未改动。完整文件有版本和 import 变化，并非主文件完全不变。

## 浏览器覆盖

Playwright + Edge 153.0.4234.32，1440px 桌面与 390px 手机。

17 项真实 API 场景：画布/布局编辑、自定义方案与导入导出、历史生成/下载/方案恢复、配置备份导入/缓存、字体上传、宿主配置保存、手机配置、正式 federation 加载、原生读取/发布/旧图恢复、服务器逐库预览/批量处理及 GIF 播放暂停。

4 项新增展示场景使用本地 API 拦截构造状态：空库画布与卡片原因；历史和弹窗真实格式、旧 JPEG/WebP 标签及未知原因；逐库 GIF/静态/失败计数和备份阶段；390px 长说明无横向溢出。它们验证 UI 展示，不替代前述真实后端测试。

截图 docs/previews/studio-output-diagnostics-mobile.png 和 studio-output-history-mobile.png 已人工查看。其余工坊截图由真实回环工作流重新生成，GIF 演示沿用现有三幅原创素材，动画算法未更改；全部不包含用户影片或凭据。

回归过程中调整了两个旧断言：缓存从原始 PNG 改为缩小 JPEG，改为检查允许的素材身份；任务文案改为 GIF/静态计数，浏览器同步检查真实任务结果。首次相关断言失败后，最终完整套件通过。

## 复现

```bash
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
# 另一个终端；ui_smoke 每次从新启动的 QA 服务开始
node tests/ui_smoke.mjs
node tests/ui_output_diagnostics.mjs
```

环境：Windows、Python 3.12.14、Pillow 12.3.0、fonttools 4.65、HTTPX 0.28.1、FastAPI 0.115.14、Node 24.19.0、Vite 5.4.21。测试监听仅回环，插件没有新增端口。

## 发布与验收边界

完整包包含主模块、coverstudio、字体及许可、编译前端、索引、源码和测试文档，SHA256SUMS 逐文件校验。版本 4.5.1 未部署，现场步骤见 ACCEPTANCE.md。

保留 8098→3334→8096、原 ItemId/MediaSource/302。原生图片只在用户明确生成并更新时写入。历史仍有 256 MiB 总量上限，大批次较早备份可能被淘汰。空库/单图正常静态，部分客户端只显示 GIF 首帧；外部榜单缺少可靠身份或不可访问不在此次封面修复中伪装成成功。
