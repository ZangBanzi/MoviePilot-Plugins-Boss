# MediaArchiver 4.6.2 验证记录

日期：2026-10-09。作者：Boss。基线：本地 b93ea03 / 4.6.1；GitHub main 基线 4fc86fa / 4.6.0。所有原生图片写入只发生在随机回环 Emby 替身，未连接或部署NAS。

## 复现与修复

新增 tests/test_job_lifecycle.py 共20项：停止/重载 × 取材/绘图/原图备份/POST派发前（8）；普通取消完成当前库；保存方案竞态；生成期间重置拒绝；完整/轻量状态深复制（2）；历史开关固定（2）；读取目标时重载；原生恢复停止/重载（2）；已完成历史保留；线程启动失败释放锁及重试。

首批13项在4.6.1运行结果为12失败、1通过，原生HTTP替身实际记录到停止/重载后POST；另复现检查后保存的竞态、重置绕过生成锁、嵌套状态共享引用和历史开关中途改变。修复没有更改源站诊断、鉴权、虚拟成员/原媒体接口。

针对性66项通过；最后新增边界后生命周期20项全部通过。完整后端与浏览器最终结果见下文。

## 最终验证

最终完整运行 scripts/check.py：175项pytest全部通过（43.49秒），另4个独立脚本通过。包含4.6.1原155项与本次新增20项。原302 Location、ItemId/MediaSource、识别、权限撤销、分页、GIF/静态回退、原生原字节备份及历史清理均继续通过。

正式federation与preview构建成功，插件/引擎/前端/市场索引统一4.6.2。浏览器五套件全部通过：smoke17、templates8、selection10、diagnostics4、reliability3，共42场景。所有pageerror为空，实际API套件apiErrors为空；故障注入套件仅按测试预期模拟失败。桌面及390px简体中文页面截图目视检查可用。

首轮浏览器smoke第12场景因严格定位匹配两处完成文案失败：持续进度节点与临时toast同时出现。将断言限定 .ma-note[role=status] 后全套42场景通过，没有改动应用提示或放宽业务断言。

scripts/release.py生成完整SHA256SUMS并逐文件核对ZIP字节，重复打包校验一致；发布包不包含运行数据、.work、虚拟环境、node_modules或凭据。差异检查通过，原CRLF/混合换行按未修改行保留。

## 复现

```bash
python scripts/check.py
cd frontend
npm ci
npm run build
npm run build:preview
cd ..
python scripts/check_ui.py
python scripts/release.py
python scripts/release.py --check
```

测试沿用 .venv 与现有锁定 node_modules，未安装/更新新依赖；本次不冒充重新执行 npm ci 或 npm audit。4.6.1依赖审计记录见 docs/TEST_REPORT-4.6.1.md。本次浏览器使用本地Chrome，截图和结果位于忽略目录.work/，不覆盖docs旧截图。

发送前检查与生命周期切换串行；已发出POST无法撤回，重载等待请求结束。正在绘图/取材仍需返回到检查点，沿用原请求超时。预算只限制编码缓存，不限制进程RSS。未现场部署、未验证NAS性能或全客户端兼容。

远程同步（2026-10-10）：GitHub集成仍返回403，终端HTTPS仍无凭据；已通过现有 GitHub Desktop 登录将两个提交快进推送main。GitHub读取远程SHA为31fa2db5a8b68080065836784e67c7e1dd238921，与本地一致；未创建PR，未部署NAS。本文中的175项后端/42场景仍为本地结果，远程Actions状态单独核实。


2026-10-10 首次远程Actions的后端、依赖审计、构建和浏览器步骤通过，最后校验失败：Linux重建产物与macOS提交产物的完整清单不一致。CI现先在构建前验证已提交SHA256SUMS，再对本次重建输出打包并校验；二者分别验证，不能用重建清单代替原始提交校验。CI成功后提供完整ZIP artifact，失败诊断显式包含隐藏.work目录。最新修复的远程结果待核实。
