# 媒体虚拟库交接 · 4.6.1

作者 Boss，2026-10-08。接手 GitHub main 基线 4fc86fa（4.6.0），本地分支 codex/plugin-reliability-optimization。保留十款封面、严格识别、增量/Cron、用户权限和 NextEmby 8098 → MoviePilot API3334 → Emby8096。

新增cache.py：成品缓存96项/64MiB，素材缓存48项/32MiB/60秒，按最近使用淘汰。字节预算只计算编码图片，不限制进程整体RSS。命中成品缓存时无需等待冷绘图；冷绘图仍串行。配置更新/清理后旧绘图不填回新缓存。每次权限核验、用户凭据隔离和原302保留。

同步和榜单响应限制16MiB。分页无新增ID则停止保留旧快照；总数字段缺失时完整页继续读，短页结束。重试等待用取消事件及时结束，已发出的请求仍遵守原超时。没有通过改名、放宽识别或混用其他榜单掩盖源站问题。

历史图片先解码生成缩略图再写文件，写入失败尝试清理新文件；保留预算只累加保留记录。数据子目录拒绝越出根目录的符号链接。不删除用户文件；旧安装若把子目录指向根目录之外，应改回真实子目录。原生图仍按原字节备份、精确库ID核验后手动上传，生成/恢复/清理仍共享job_lock。

前端批量预览逐库失败隔离，失败卡片提供原因，剩余库继续；刷新响应绑定请求序号与服务器，页面卸载后轮询不再更新。预览构建、截图及QA数据改为项目内.work/，避免写父目录及覆盖历史docs截图。

Vite7.3.7与Vue构建插件6.0.9，Node需20.19+或22.12+；NAS无需Node。锁文件审计0漏洞。正式dist完整重建，约488KiB降至120KiB。发布必须完整包含cache.py、coverstudio.py、fonts、requirements与dist/assets。

155项pytest、4个独立脚本、42个浏览器场景验证见TEST_REPORT.md。scripts/check.py隔离旧脚本污染；check_ui.py逐套件启动全新回环服务；release.py生成完整SHA256SUMS与可复现ZIP，并逐文件验证包内字节。GitHub Actions同时执行检查、构建、依赖审计和浏览器回归。

原4.6.0交接与报告保存在docs/。本次未连接或部署NAS，没有使用远程凭据、重启MoviePilot或更新真实Emby封面。本地测试不能代表现场部署或全部客户端兼容。完整发布包：releases/MediaArchiver-v4.6.1.zip。上传须解压并按原目录更新；不用把ZIP直接当市场插件安装。

远程交付未完成：终端HTTPS缺少登录凭据，已连接GitHub插件创建分支返回403 Resource not accessible by integration。未上传GitHub、未创建PR、未合并main。本地提交、完整源码和ZIP可用，需要仓库写入权限才能继续同步。
