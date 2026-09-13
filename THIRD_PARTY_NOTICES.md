# 来源与致谢

## 界面和布局参考

参考项目：[justzerock/MoviePilot-Plugins · Yahaha Cover Studio](https://github.com/justzerock/MoviePilot-Plugins/tree/main)。本次查看了其 Vue 页面、配置页面、联邦组件构建方式和封面预设结构，以及用户提供的五张界面截图。

媒体虚拟库的封面工坊由本项目重新实现，采用相近的深色分区卡片、蓝色选中态、大画布预览、方案网格、字体库和历史页。未复制参考项目的 Python/Vue 业务源码、Logo、字体资源或独立 Docker 应用。各布局的名称、绘制实现及其虚拟库适配属于本项目实现。

## 内置字体

- Noto Sans SC，来自 [Google Fonts 字体仓库](https://github.com/google/fonts/tree/main/ofl/notosanssc)。
- 原始可变字体：[NotoSansSC[wght].ttf](https://github.com/google/fonts/blob/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf)。发布包中的文件重命名为 `NotoSansSC.ttf`，内容未修改。
- 使用 SIL Open Font License 1.1；完整许可证随字体提供于 `plugins.v2/mediaarchiver/fonts/OFL.txt`。
- 字体不是插件作者创作，插件作者仍为 Boss。

## MoviePilot 接入依据

- [插件基类](https://github.com/jxxghp/MoviePilot/blob/v2/app/plugins/__init__.py)：Vue 渲染模式和插件数据目录。
- [插件 API](https://github.com/jxxghp/MoviePilot/blob/v2/app/api/endpoints/plugin.py)：插件静态资源、鉴权、配置保存与调度重新注册。

前端通过宿主共享 Vue；构建与浏览器测试依赖仅用于开发，不安装到 MoviePilot Python 环境。NAS 端需要随发布包部署已经编译好的 `dist/assets`。

## 4.4.1 原生媒体库与海报接口

继续查阅参考插件的媒体库读取、取图及图片上传实现；本次新增方法仍为独立实现。

- [原生库查询及 Id / ItemId 字段](https://dev.emby.media/reference/RestAPI/LibraryStructureService/getLibraryVirtualfoldersQuery.html)。
- [Items 的 ParentId、ImageTypes、EnableImages 和 Fields 参数](https://dev.emby.media/reference/RestAPI/ItemsService/getItems.html)。
- [管理员上传图片：base64 请求体](https://dev.emby.media/reference/RestAPI/ImageService/postItemsByIdImagesByType.html)。

Emby 的接口名 VirtualFolders 在这里表示原生服务器媒体库列表；与本插件网关合成的虚拟专区分开处理。仅使用列表 GET 及已核验库 Primary 图片 POST，不创建 VirtualFolder。

## 4.5.0 轮播与用户接口

进一步阅读了 [Yahaha style_animated_1.py](https://github.com/justzerock/MoviePilot-Plugins/blob/main/plugins.v2/yahahacoverstudio/style/style_animated_1.py) 的多图轮播、图片去重和稳定画布设计。本项目用 Pillow 单次构图加共享调色板渐变实现，不复制其 NumPy/FFmpeg 动画源码。

[Emby UserService](https://betadev.emby.media/reference/RestAPI/UserService.html) 提供 `/Users/{Id}`。现场确认当前服务器不支持旧代码使用的 `/Users/Me`；新代码优先使用认证首页已确认的用户 ID。没有取得可信用户身份时，只返回静态品牌图。

## 4.6.0 构图扩展参考

2026-09-13 阅读 GitHub 说明、预设和绘图源码，对照现有四款后新增六款不同构图。下列项目仅用于设计研究；本次六款 Pillow 绘制代码、几何缩略图及预览风景图由本项目独立编写，未纳入其源码、Logo 或影视海报。

- [justzerock 预设](https://github.com/justzerock/MoviePilot-Plugins/blob/main/plugins.v2/yahahacoverstudio/style/preset_templates.py)：核对叠卡、斜切、海报墙和标题四种原有结构；仓库根 LICENSE 为 GPLv3。
- [g-steven037 EmbyLibraryCover renderer](https://github.com/g-steven037/MoviePilot-Plugins/blob/main/plugins.v2/embylibrarycover/renderer.py)：参考宽幅剧照、底部六张海报和满幅拼接的构图关系，发展为银幕、胶片及三联。当前仓库根和插件目录未见许可证，不搬入源码。
- [AutoFilm LibraryPoster](https://github.com/AkimioJR/AutoFilm#libraryposter)：对照 blur/card/collage/split 的竖海报比例与文字留白。其 LICENSE 附带非商业等条款，未复制实现。
- [HappyQuQu jellyfin-library-poster](https://github.com/HappyQuQu/jellyfin-library-poster)：参考多海报与主题色平衡；MIT 许可，未复制其生成代码或示例图片。

刊物、黑胶和拼色使用常见的杂志分栏、唱片和四宫格构图独立扩展。方案列表的示例图片仅用于展示构图；实际生成严格使用当前媒体库的影片资源，空库仍为静态品牌画面。
