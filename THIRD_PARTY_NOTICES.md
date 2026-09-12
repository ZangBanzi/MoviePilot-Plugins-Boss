# 来源与致谢

## 界面和布局参考

参考项目：[justzerock/MoviePilot-Plugins · Yahaha Cover Studio](https://github.com/justzerock/MoviePilot-Plugins/tree/main)。本次查看了其 Vue 页面、配置页面、联邦组件构建方式和封面预设结构，以及用户提供的五张界面截图。

媒体虚拟库的封面工坊由本项目重新实现，采用相近的深色分区卡片、蓝色选中态、大画布预览、方案网格、字体库和历史页。未复制参考项目的 Python/Vue 业务源码、Logo、字体资源或独立 Docker 应用。四个布局的名称、绘制实现及其虚拟库适配属于本项目实现。

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
