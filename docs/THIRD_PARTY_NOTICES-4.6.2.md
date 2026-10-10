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


## 历史内置字体完整许可（当前版本已移除字体）

Copyright 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
https://scripts.sil.org/OFL


-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded, 
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
