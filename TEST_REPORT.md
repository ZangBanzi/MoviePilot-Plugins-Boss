# MediaArchiver 4.5.0 验证记录

日期：2026-09-13。作者：Boss。修复基线为已交付的4.4.1；4.3.11原始快照用于播放/识别回归比较。

## 结论

**68项pytest通过（26.65秒），4个独立回归脚本通过，17个Edge浏览器场景通过。** Vue正式federation与预览构建成功。最终浏览器 `errors=[]`、`apiErrors=[]`。

新版本只在本地实现、测试和打包，没有上传GitHub或部署NAS。所有原生图片写入/恢复测试均在随机回环端口的Emby测试实例完成。

## NAS只读核验

用户授权后，仅登录并读取健康接口、服务器信息、当前用户/会话、首页及少量图片。未触发远程同步、封面发布、重启、配置保存或影片播放。地址和凭据不进入报告或发布包。

| 现场项目 | 实测结果 |
|---|---|
| 插件 | 4.4.1；主文件SHA-256为 `7c50922189a3de1c47f3cc622da61c6b83302ccfad23157719963203dccdb45a`，与旧发布包一致 |
| Emby | 4.9.5.0 |
| `/Users/Me` | HTTP500，旧版因此无法确认用户并取材 |
| `/Users/{实际用户Id}` | HTTP200 |
| 图片抽查 | Remux、Netflix等返回有效GIF；Edge实际图片元素可解码这两张当前GIF，WebP抽查也可解码 |

不能把截图中所有历史破图都归因于一个已证实的现场故障。4.5.0补齐身份接口问题、图片无登录头场景、传输压缩、鉴权/坏图失败回退；新包在NAS的最终效果仍需升级后验收。

日志中的腾讯“混合榜子源不完整，整榜放弃”提示不在核对过的4.4.1源码中；健康接口不能证明所有旧后台任务均已退出。新版给同步任务独立取消标记，停止/重载后不继续提交旧结果。豆瓣失效榜、猫眼身份不足、IMDb/Bangumi访问异常未被伪装成修复成功。

## 后端覆盖

| 文件 | 覆盖 |
|---|---|
| `test_server_cover_workflow.py`（9项） | 整台服务器逐库取材/备份/发布；第二服务器同名同ID不切换网关；Users/Me 500；无登录头图片凭证、其他显式用户隔离、权限撤销/恢复/到期；gzip图片；0/1张静态；旧同步取消；8张冷封面生成时原302和健康接口在断言时限内响应；原生列表刷新与预览并发不出现空快照，失败使旧目标失效 |
| `test_native_covers.py`（10项） | 原生/虚拟共同筛选、70项缺图前缀后找到海报、坏图和Backdrop→Primary回退；真实base64 PNG/GIF发布、备份/恢复；同名库按ID定位；无虚拟库也可生成；鉴权/磁盘/无图失败保留原图；生成与原MediaSource/302联合验证 |
| `test_coverstudio.py`（23项） | 四布局真实海报轮播，变化覆盖大幅画面；3张素材6秒循环及停留时长；格式/分辨率、字体真实解码、缓存/ETag、配置/历史、管理员权限和用户素材隔离 |
| `test_gateway_runtime.py`（26项） | FastAPI/HTTPX网关、压缩、Cookie隔离、流式关闭、Range/302、32并发、WebSocket、同步不阻塞、图片格式与真正GIF编码失败回退 |

四个独立脚本 `test_virtual_library.py`、`test_accuracy.py`、`test_animated_cover.py`、`test_ranking_failures.py` 均通过，包含原ItemId、分页/Latest、Cron/增量、严格榜单匹配、腾讯确认空榜与畸形响应区分、混合榜部分新增/旧成员保留/完整恢复清理。

与4.3.11比较，以下9个方法AST一致：`_gateway_stream_response`、`_gateway_buffered_response`、`_virtual_items_response`、`_select_view_item_ids`、`_forward_request_headers`、`_upstream_target`、`get_service`、`_scheduled_sync`、`_cron_trigger`。网关分发增加用户封面凭证，生命周期增加取消检查；整个主文件并非未修改。

## 浏览器覆盖

Playwright + Edge153.0.4234.32，1440px桌面和390px手机，真实FastAPI工坊API。

原有13个流程保留：真实PNG画布、预设/标题/键盘布局、自定义方案、导出导入、历史生成/下载/恢复、配置备份/导入/缓存、字体上传、宿主保存、手机布局、正式federation Page、原生取图、原生发布、原图恢复。

新增4个流程：一个服务器选择框和6张各自预览的库卡片；一次处理2个原生库和4个虚拟库；手机整台服务器按钮无横向溢出；真实GIF预览播放及暂停回到PNG。最终无页面异常、无失败的工坊API操作。

测试发现并修正：选择框标签名称、刷新列表时立即点选导致预览失败、发布重查列表时并发预览读到空快照。

截图见 `docs/previews/`。`cover-preview.gif` 是当前引擎输出：3张原创构造素材、15帧、960×540、6秒循环，另提供三个停留画面PNG并人工核对海报切换。示例不包含用户影片或凭据。

## 复现

```bash
python -m pytest tests/test_server_cover_workflow.py tests/test_native_covers.py tests/test_coverstudio.py tests/test_gateway_runtime.py -q -p no:cacheprovider
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
# 另一个终端，使用新的测试服务实例
node tests/ui_smoke.mjs
```

环境：Windows、Python3.12.14、Pillow12.3.0、fonttools4.65.0、HTTPX0.28.1、FastAPI0.115.14、Node24.19.0、Vite5.4.21。回环服务仅用于开发，插件没有新增监听端口。

逐文件校验见SHA256SUMS。ZIP包含主程序、封面模块、字体/许可、编译前端、索引、文档和测试；不包含node_modules、虚拟环境、凭据、临时数据或旧包。

## 验收边界

保留8098→3334→8096、原ItemId、MediaSource和302。原生图片只在用户明确点击更新/整台服务器按钮时发布，Cron不覆盖原生库。GIF自动播放取决于客户端；明确请求PNG/JPEG/WebP或素材少于两幅时静态输出。现场步骤见ACCEPTANCE。
