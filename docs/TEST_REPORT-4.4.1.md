# MediaArchiver 4.4.1 验证记录

日期：2026-09-13。作者：Boss。基线：4.3.11 最后一次同版本榜单故障修复。修改前旧 SHA256SUMS 的 13 个文件全部匹配。

## 结论

**59 项 pytest 通过（29.62 秒）、4 个独立回归脚本通过、13 个真实浏览器场景通过。** 前端生产构建和本地预览构建成功，10 个 Python 源文件 AST 解析通过，插件/市场索引/前端包与锁文件版本均为 4.4.1。

这是本地构造数据与回环上游的测试结果。本次未访问用户 NAS，未上传仓库、未安装到 MoviePilot 容器。原生库图片写入仅发生于独立回环测试上游，不把测试通过表述为所有实际播放器/公网榜单均无问题。

## 实现范围

- 新增原创 Vue 封面工坊/配置页和四种 Pillow 封面方案，参考用户截图与呀哈哈封面工坊的页面组织。
- 实现真实预览、静态/GIF、画布参数、独立/默认方案、自定义方案导入导出、字体导入、历史生成/恢复/下载、配置备份和缓存操作。
- 封面与配置 API 仅供 MP 管理员；播放器素材按入站 Emby 用户身份重新验证和获取。
- 保留原属性/榜单识别、Cron/事件维护、分页、ItemId、MediaSource、8098 和原302透传。

## 后端验证

| 测试 | 结果及实际覆盖 |
|---|---|
| tests/test_native_covers.py | 10项：两类库在70项缺图前缀后找到相同海报、坏图回退、同名不同ID、原生PNG/GIF真实base64上传、无虚拟库也可生成、原图备份/恢复、仅保留1批的备份可用、列表旧接口、目标移除/鉴权失败/磁盘失败/无图保护、生成期间完整网关302/原MediaSource、素材恢复后缓存失效 |
| tests/test_coverstudio.py | 23 项：四布局各自真实12帧GIF与帧变化、四种不同静态输出、1280×720导出、参数校验、方案保存不丢旧配置、ImageTag更新、全局/独立优先级、历史/下载/恢复/删除、单任务及取消、备份/路径限制、TTF/WOFF2实际解码与缺字回退、管理员API、按用户取图/权限撤销/缓存隔离 |
| tests/test_gateway_runtime.py | 原有26项：真实HTTPX/FastAPI、回环HTTP上游、压缩解码、Cookie隔离、流式关闭/断连、Range/302、32并发、WebSocket、同步/封面不阻塞浏览、GIF/静态格式与缓存 |
| tests/test_virtual_library.py | 通过：全量/增量/Cron、首页View、原ItemId、15组分页请求、Latest、动态封面、302以及18组伦理样本等 |
| tests/test_accuracy.py | 通过：21组属性样本、多媒体版本冲突、TVB、身份消歧、榜单主体、来源失败和旧快照迁移等 |
| tests/test_animated_cover.py | 通过：12帧640×360循环GIF、不同帧像素、缓存/304、成员变更失效、真实编码异常PNG回退 |
| tests/test_ranking_failures.py | 通过：空榜与畸形响应、混合榜部分新增/旧成员保留/完整恢复清理、豆瓣北美区块隔离、IMDb嵌套数据、猫眼身份不足 |

封面网关测试使用真实请求/响应对象、实际Pillow解码和两组Emby用户构造响应：非管理员被拒绝；撤销用户权限后不复用旧图片或返回旧304；不同用户可见海报不串用。不是实际 NAS 帐号测试。

## 浏览器验证

Playwright + Edge 153.0.4234.32，真实 FastAPI 测试服务，只绑定随机回环地址。桌面1440px、手机390px；无未捕获页面错误。

1. 大画布显示后端真实PNG。
2. 切换预设、修改中文标题、键盘调整画布位置。
3. 新建自定义方案、保存并应用到当前虚拟库。
4. 下载真实方案JSON后重新导入。
5. 后台生成完成、查看历史、下载原图、恢复设计参数。
6. 配置备份/文件导入、清理图片与字体缓存。
7. 浏览器FileReader读取字体文件，经API导入真实字体库。
8. 配置校验和save事件，模拟宿主保存并重载状态。
9. 手机生成按钮可见，工坊和配置页均无横向溢出。
10. 通过MP风格静态资源路径加载**正式构建的 federation Page**，加载宿主共享Vue及正确的哈希JS/CSS，而非只验证开发页面。
11. 读取两个同名但不同ID的原生库，选中实际库并预览其海报和总数。
12. 点击原生更新按钮，向回环Emby测试服务器发出真实base64图片POST并收到成功结果。
13. 历史弹窗展示更新前备份，执行“恢复原生库图片”，回环服务器收到原图。

截图在 docs/previews/，人工查看了桌面工坊、配置页、手机工坊/配置页及历史页。截图使用构造海报，不是源站下载或真实用户影片。cover-preview.gif 用当前绘图器生成，115项Remux为构造样例，已解码验证12帧640×360。

## 验证中发现并修正

- 生产远程组件的资源基路径：修正 assets/assets 重复，浏览器验证实际MP路径加载。
- 内置可变字体默认笔画过细：设置标题/正文相应字重；缺失字形回退内置中文字体。
- 静态方案遇Format=original或gif仍可能动图：现在遵守方案静态设置。
- 配置更新过程中旧绘图可能带新标签：冻结每次绘图选项并以相同快照计算ETag，加入回归。
- 历史淘汰先删图再写索引的失败窗口：先成功提交索引，再清理淘汰图片。
- 封面/管理锁的反向获取风险：清缓存通过交换引用，避免持管理锁等待绘图锁。
- 手机端生成按钮被样式隐藏：已补回并在浏览器断言可见。
- 移动/缩放控制项与布局不适用时：隐藏无效控制，方向键与拖动采用一致限制。
- 历史测试可能在生成完成前读到旧记录：等待启动响应和实际完成；测试服务每次隔离数据，生成中禁用恢复按钮。4.4.0修正后10个场景通过；本次加入原生库3项后，最终13项全部通过。

## 复现及发布校验

安装独立 requirements-dev.txt，执行：

```bash
python -m pytest tests/test_native_covers.py tests/test_coverstudio.py tests/test_gateway_runtime.py -q -p no:cacheprovider
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
```

实测环境：Windows、Python3.12.14、Pillow12.3.0、fonttools4.65.0、HTTPX0.28.1、FastAPI0.115.14、Node24.19.0、Vite5.4.21。测试依赖独立安装，没有修改运行中的MP。

主源码SHA-256：`7c50922189a3de1c47f3cc622da61c6b83302ccfad23157719963203dccdb45a`。

SHA256SUMS逐项覆盖本次交付的源码、编译资源、字体/许可、索引、文档与测试；发布ZIP逐文件与该清单核对。归档旧包、node_modules、临时数据和虚拟环境不进入发布ZIP。

## 仍需用户环境验收

- 真实MP镜像中的安装依赖、管理员会话、宿主Vue页面/配置保存生命周期。
- Emby实际海报读取与不同权限帐户、NextEmby8098及原302播放、TV/手机客户端GIF显示。
- 公网字体直链的实际可达性；测试覆盖文件上传/解析和不允许的地址，未承诺所有HTTPS字体站点可用。
- 豆瓣tv_global、IMDb、AniList/Bangumi、猫眼等此前外部限制保持原状。

历史报告见 docs/TEST_REPORT-4.3.11.md。该历史文件记录旧版本结果，不作为本次NAS现场验证证据。

## 4.4.1 专项验收：原生、虚拟、播放同时验证

本轮扩展模拟上游为真实 ThreadingHTTPServer：包含73个媒体条目、前70项无图、一个坏图、两个有效海报、两个同名原生库、两种用户权限、可撤销凭据和会保留实际图片的上传接口。不是给所有请求无条件返回成功的静态页面。

- 原生库通过 Library/VirtualFolders/Query 分页读取，兼容旧列表接口；ParentId限定库范围，ImageTypes寻找后排素材。
- 虚拟库仅用自身成员ID，保留图片标签优选候选；两类库得到相同的两幅有效图片，未改变原成员。
- 以前请求Fields=ImageTags/BackdropImageTags会被模拟Emby按无效枚举拒绝；新实现通过EnableImages=true取得自动图片信息。
- PNG和12帧GIF分别完成预览→生成历史→base64发布→备份恢复，验证上传MIME和实际图片帧数。没有虚拟库时原生功能照常可用。
- 原生库移除、非法目标、POST403、备份写盘失败或没有可用素材时，不覆盖原图片或误报成功；配置关闭历史时仍强制更新前备份的实现路径保持独立。
- 原生生成过程中，真实FastAPI网关仍返回原302 Location（含原签名参数）和原MediaSource ID，并能读取受限用户虚拟封面；管理员图片不会复用到普通用户缓存。
- 图片源恢复但成员/方案不变时，真实图片字节进入封面缓存标识，旧品牌图ETag不能使恢复结果被错误304拦住。

另外直接比较了4.3.11基线ZIP与当前AST：_gateway_stream_response、_gateway_buffered_response、_forward_request_headers、_upstream_target、_fetch_gateway_bytes、_virtual_items_response、_inject_views、_scheduled_sync、_sync_server、_source_attributes 共10个方法完全一致。_emby_gateway_dispatch唯一改动是虚拟Primary图片分支委派到工坊图片函数，其余路由分支未改。

### 仍需现场核对

原生库GIF能否由实际Emby版本接收并被客户端播放，要以NAS为准；若服务端拒绝图片格式，任务会明确显示HTTP失败，可选择静态PNG后再生成。公网海报网络、真实影视元数据与所有播放器表现不能由回环样本保证。4.4.1的原生发布是手动按钮，不进入既有自动同步/Cron流程。

原生库界面与备份截图：docs/previews/studio-native.png、studio-native-history.png。安装后验收步骤见 ACCEPTANCE.md。旧4.4.0报告归档至 docs/TEST_REPORT-4.4.0.md。
