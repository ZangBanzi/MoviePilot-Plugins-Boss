# MediaArchiver 4.6.1 验证记录

日期：2026-10-08。作者：Boss。基线：GitHub main 4fc86fa7d9806e021aeda5ba629b798814bfd7c7 / 4.6.0。

原有137项pytest在修改前全部通过。新版完整回归155项（原137项+新增18项资源/故障回归），另4个独立脚本、42个浏览器场景通过。正式federation与preview构建成功，npm ci成功，依赖审计0漏洞。所有媒体接口操作只发生在随机回环Emby替身，未连接或部署NAS。

## 后端

新增tests/test_resource_limits.py覆盖：LRU按字节淘汰/替换/超限不缓存；嵌套海报计数和固定TTL；四线程读写清理预算；冷GIF期间已缓存图片及时返回；配置重置期间旧绘图不填入新缓存；同步与榜单响应在解析前截断（2项）；重复分页停止；正常分页；缺总数的完整页继续读取；重试等待即时取消；无效历史图片不落盘；缩略图写入失败清理新原图；保留预算仅计算入选记录；超限记录不阻止后续小图；history/fonts/backups目录符号链接越界拒绝（3项）。

原有识别/排名、用户权限与票据、静态/GIF回退、大图限额、原生备份恢复、批量选择、历史清理、压缩协议、流式断开与302回归继续通过。四个旧脚本用独立Python进程运行，避免导入时替换FastAPI造成pytest收集失败。

冷画廊测试曾连接失败，定位为本地ThreadingHTTPServer默认backlog=5，八库并发超出队列。改为服务器激活前声明request_queue_size=128，不放宽断言或插件超时；最终完整回归通过。运行时未增加监听端口。

## 前端与浏览器

| 套件 | 场景数 | 验证 |
|---|---:|---|
| ui_smoke.mjs | 17 | 画布/字体/方案/配置/历史、federation共享Vue、原生发布恢复、批量与真实GIF |
| ui_templates.mjs | 8 | 十款实际输出、旧方案配色、适用控件、独立草稿、批量与390px页面 |
| ui_selection.mjs | 10 | 全选/反选/逐库、草稿、当前/保存方案、历史删除/清空/故障重试 |
| ui_output_diagnostics.mjs | 4 | 固定响应展示空库/单图/旧记录/备份失败，手机无横向溢出 |
| ui_reliability.mjs | 3 | 单库失败仍处理余库、重试恢复、延迟旧状态不覆盖新选择 |

共42项：原34项实际回环API流程、原5项展示响应、新3项故障/延迟流程。pageerror全部为空；真实API套件无非预期apiErrors。异常套件注入一次预期400验证错误处理。桌面与390px截图已目视，简体中文文字和方案布局可用。

浏览器使用本机Chrome，截图/结果保存在忽略目录.work/，不替换docs里4.6.0示例。check_ui.py为每个套件重启独立QA服务并关闭。旧报告的39场景不是本次验证的替代。

## 构建、审计与打包

Python3.12.14、Pillow12.3.0、HTTPX0.28.1、FastAPI0.115.14、Node24.19.0、Vite7.3.7、@vitejs/plugin-vue6.0.9。测试环境隔离在.venv，不修改MoviePilot依赖。

基线npm audit为3项（1 moderate/2 high）；升级Vite/Vue构建插件并更新source-map-js后，npm ci与audit为0漏洞。锁文件与正式组件一起提交，NAS无需Node。正式dist约488KiB → 120KiB，清理过时JS/CSS，只保留当前组件；federation通过真实浏览器加载。

scripts/release.py检查插件/引擎/前端/市场索引统一4.6.1，生成完整SHA256SUMS与固定ZIP时间戳，逐文件验证包内原字节。相同源码重复打包SHA256一致。GitHub Actions已配置；本地通过不冒充远程Actions已运行。

## 复现与边界

```bash
python scripts/check.py
cd frontend
npm ci
npm audit --audit-level=moderate
npm run build
npm run build:preview
npx playwright install chromium
cd ..
python scripts/check_ui.py
python scripts/release.py
python scripts/release.py --check
```

原CRLF/混合换行保留，差异检查用cr-at-eol处理CR，没有整仓格式化。cache.py必须随完整插件更新。预算只约束常驻编码缓存，不是Pillow绘制/Python对象/网络开销的总RSS；冷图仍串行，没有NAS负载或量化播放性能保证。未改变媒体文件、ItemId、MediaSource、Collection/BoxSet、端口和302 Location。现场验收见ACCEPTANCE.md。

远程同步结果：GitHub插件create_branch返回403 Resource not accessible by integration，终端HTTPS没有可用登录凭据，因此未上传代码、未创建PR；已配置的Actions尚未远程执行。
