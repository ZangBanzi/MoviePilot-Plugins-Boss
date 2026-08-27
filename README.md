MoviePilot v2 媒体一键归档插件

当前版本：1.2.0

安装

1. 在你的第三方插件仓库中创建 plugins.v2/mediaarchiver/。
2. 将本目录的 __init__.py 放入上述目录。
3. 将 folder-move.svg 放入该仓库的顶层 icons/ 目录；原代码引用了这个图标，但没有附带文件会导致图标加载失败。
4. 在该仓库的 package.v2.json 合并随附的 package.v2.fragment.json 内容。
5. MoviePilot 中添加/刷新第三方插件仓库，安装“媒体一键归档”并保存配置。
6. 打开插件的“数据”页面，点击“一键归档”；任务会在后台执行，可点击“刷新状态”查看结果。
7. MoviePilot 容器必须以可写方式映射 /mnt/115，且容器内路径与 Emby Items 返回的 Path 一致。

重要说明

• 电影 Remux 是复制；平台剧集和伦理内容是移动。
• 插件会读取全部已启用的 Emby 服务，并分页获取 Movie,Series,Episode 条目；同一路径只处理一次。
• 开启“剧集必须通过 TMDb 在线验证”后，插件优先读取 Emby Series 的 TMDb 编号；没有编号时使用剧名和年份在线搜索。
• 在线查询 TMDb 的发行网络和制作公司，确认属于 Netflix、Disney、HBO/Max 或 Apple TV+ 后，才会移动到对应专区。无法得到唯一结果、查询失败或未匹配到专区时保留原目录。
• 剧集归档以整部剧集文件夹为单位，例如把完整的 剧名/Season 1/... 一次移动到专区；不是逐集零散移动。
• 关闭在线验证开关时，才会回退到文件路径关键词：Netflix/NF/奈飞、Disney/Disney+/D+/迪士尼、HBO/HBO Max、Apple TV+/ATV+/APTV/苹果。
• 匹配优先级依次为奈飞、迪士尼、HBO、APTV；成人关键词优先级高于全部平台专区。
• 为避免 rclone 挂载失效时误写容器层，插件不会自动创建挂载根目录或两个源目录；它只自动创建六个归档目标目录。
• TMDb 在线请求复用 MoviePilot 已配置的 TMDb API 域名、密钥和代理，不需要在插件中重复填写 API Key。
• Remux 使用 shutil.copy2 复制；平台剧集及伦理内容使用 shutil.move 移动。发生重名时自动添加 _1、_2 等序号。
• 不建议直接移动仍在做种的文件；移动后请刷新 Emby 媒体库。
• 三个配置路径都必须是容器内绝对路径。源文件或剧集目录不存在、不在配置源目录内时不会处理。

API（可选）

数据页按钮使用 MoviePilot v2 支持的 events.click API 事件。也可以手动调用：

POST /api/v1/plugin/MediaArchiver/run

GET /api/v1/plugin/MediaArchiver/status

两个接口均使用 Bearer 鉴权。插件分身会使用分身自身的插件 ID，不会写死 MediaArchiver。