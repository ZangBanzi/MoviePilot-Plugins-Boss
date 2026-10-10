"""MoviePilot's native form/page contract, independent of images and federation."""


def node(component, text=None, content=None, **props):
    result = {'component': component}
    if text is not None: result['text'] = str(text)
    if content is not None: result['content'] = content
    if props: result['props'] = props
    return result


def alert(title, text, kind='info'):
    return node('VAlert', title=title, text=str(text), type=kind, variant='tonal', **{'class': 'mb-3'})


def action(text, path, method='post', disabled=False):
    result = node('VBtn', text, color='primary', variant='outlined', disabled=disabled)
    result['events'] = {'click': {'api': 'plugin/MediaArchiver/' + path, 'method': method, 'params': {}}}
    return result


def field(component, model, label, **props):
    return node(component, model=model, label=label, **props)


def row(children):
    return node('VRow', content=[node('VCol', content=[child], cols=12, md=6) for child in children])


def panel(title, children):
    return node('VExpansionPanel', content=[node('VExpansionPanelTitle', title),
                                          node('VExpansionPanelText', content=children)])


def table(headers, rows):
    return node('VTable', content=[
        node('thead', content=[node('tr', content=[node('th', title) for title in headers])]),
        node('tbody', content=[node('tr', content=[node('td', cell) for cell in cells]) for cells in rows])],
        density='compact', hover=True, style='overflow-x:auto')


def form(plugin, rank_groups, rank_meta, default_rankings):
    attributes, rankings = plugin._aggregate_counts()
    defaults = {'enabled': True, 'virtual_enabled': True, 'ranking_enabled': False,
                'auto_sync': True, 'daily_sync_enabled': True, 'sync_cron': '0 4 * * *',
                'emby_server': '', 'emby_servers': [], 'sync_interval': 60, 'timeout': 30,
                'tmdb_api_key': '', 'tmdb_domain': '', 'ranking_regions': 'US,GB,JP,KR,HK,TW',
                'ranking_limit': 100, 'ranking_language': 'zh-CN', 'ranking_feed_url': '', 'ranking_feed_token': ''}
    defaults.update({f'zone_{key}': True for key in plugin.ATTRIBUTE_RULES})
    defaults.update({f'rank_{key}': key in default_rankings for key in rank_meta})
    groups = []
    for group in rank_groups:
        checks = [field('VCheckbox', f'rank_{key}', f"{rank_meta[key]['label']} · {rankings.get(key, 0)} 项",
                        **{'hide-details': True}) for key, _ in group['items']]
        groups.append(panel(group['name'], [node('p', group['subtitle']), row(checks)]))
    body = [node('div', '媒体虚拟库', **{'class': 'text-h5 mb-2'}),
            node('p', '选择服务器与专区，保存后到功能页同步。只建立首页入口，影片继续使用原 ItemId。'),
            field('VSelect', 'emby_server', 'MoviePilot 已配置的 Emby', items=plugin._server_options(), clearable=True,
                  hint='留空使用第一台 Emby；MoviePilot 应连接原生 Emby 8096。', **{'persistent-hint': True}),
            row([field('VSwitch', 'virtual_enabled', '启用媒体属性专区', color='primary'),
                 field('VSwitch', 'ranking_enabled', '启用榜单虚拟库', color='primary')]),
            node('VExpansionPanels', content=[
                panel('媒体属性专区', [row([
                    node('div', content=[field('VCheckbox', f'zone_{key}', f"{rule['name']} · {attributes.get(key, 0)} 项",
                                              **{'hide-details': True}), node('p', rule['hint'], **{'class': 'text-caption'})])
                    for key, rule in plugin.ATTRIBUTE_RULES.items()])]),
                panel('平台与榜单', [node('p', '源站失败会明确报告并保留可信旧内容；TMDB可播精选不等于平台官方热榜。'),
                                    node('VExpansionPanels', content=groups, multiple=True)]),
                panel('自动维护', [row([
                    field('VSwitch', 'auto_sync', '实时增量维护', color='primary'),
                    field('VSwitch', 'daily_sync_enabled', 'Cron 定时全量更新', color='primary'),
                    field('VTextField', 'sync_cron', '标准五段 Cron', hint='默认每天 04:00；使用 MoviePilot 时区。', **{'persistent-hint': True}),
                    field('VTextField', 'sync_interval', '增量校准间隔（分钟）', type='number', min=10, max=1440)])]),
                panel('榜单连接与高级设置', [row([
                    field('VTextField', 'ranking_regions', '可播地区（逗号分隔）'),
                    field('VTextField', 'ranking_language', '榜单语言'),
                    field('VTextField', 'ranking_limit', '榜单候选数量', type='number', min=20, max=300),
                    field('VTextField', 'timeout', '请求超时（秒）', type='number', min=5, max=120),
                    field('VTextField', 'tmdb_api_key', 'TMDB Key（留空复用 MoviePilot）', type='password'),
                    field('VTextField', 'tmdb_domain', 'TMDB 域名（通常留空）'),
                    field('VTextField', 'ranking_feed_url', '自定义榜单 JSON Feed'),
                    field('VTextField', 'ranking_feed_token', 'Feed Bearer Token', type='password')])]),
            ], multiple=True, variant='accordion'),
            alert('8098 虚拟库链路', f'MoviePilot API 端口 {plugin._moviepilot_api_port()}；NextEmby 上游指向此端口，客户端仍访问 NAS-IP:8098。')]
    return [node('VForm', content=body)], defaults


def page(plugin, rank_meta):
    status = plugin.status().data
    runtime = status.get('state', 'idle')
    label, kind = {'idle': ('等待同步', 'info'), 'running': ('正在同步', 'info'),
                   'completed': ('同步成功', 'success'), 'completed_with_warnings': ('同步完成，有警告', 'warning'),
                   'cancelled': ('同步已停止', 'warning'), 'failed': ('同步失败', 'error')}.get(runtime, ('运行状态', 'info'))
    attributes, rankings = plugin._aggregate_counts()
    source_status = status['source_status']
    results = []
    if plugin._attribute_enabled:
        results.extend(('属性', rule['name'], attributes.get(key, 0), '严格规则')
                       for key, rule in plugin.ATTRIBUTE_RULES.items() if key in plugin._enabled_rules)
    source_rows = []
    if plugin._ranking_enabled:
        for key in rank_meta:
            if key not in plugin._selected_rankings: continue
            source = source_status.get(key) or {}
            label_source = ('正常' if source.get('complete', True) else '部分失败，保留可信旧内容') if source.get('ok') else (
                '源站失败，保留可信旧内容' if source else '等待同步')
            results.append(('榜单', rank_meta[key]['collection'], rankings.get(key, 0), label_source))
            source_rows.append((rank_meta[key]['collection'], label_source, source.get('error') or source.get('source') or '尚未请求'))
    connection = plugin._state.get('connection_test') or {}
    logs = [(item.get('time', ''), item.get('level', 'INFO'), item.get('message', '')) for item in reversed(list(plugin._logs)[-50:])]
    return [node('div', content=[
        node('div', '媒体虚拟库', **{'class': 'text-h5 mb-2'}),
        alert(label, status.get('message') or '保存配置后同步即可显示虚拟库。', kind),
        node('p', f"最后成功同步：{status['last_sync'] or '暂无'} · 版本 {plugin.plugin_version}"),
        node('p', f"定时：{plugin._sync_cron if plugin._daily_sync_enabled else '关闭'} · 增量：{'开启' if plugin._auto_sync else '关闭'}"),
        node('div', content=[action('测试连接', 'test_connection'),
                             action('一键重建', 'rebuild', disabled=plugin._run_lock.locked()),
                             action('刷新进度', 'refresh', 'get')], **{'class': 'd-flex flex-wrap ga-3 mb-3'}),
        alert('Emby 连接', connection.get('message') or '尚未测试，使用 MoviePilot 已保存的服务器配置。',
              'success' if connection.get('ok') else 'error' if connection else 'info'),
        alert('8098 虚拟库入口', plugin._proxy_status.get('message') or '网关尚未注册',
              'success' if plugin._proxy_status.get('running') else 'warning'),
        node('p', f"本次扫描 {plugin._number((status.get('stats') or {}).get('scanned'))} 项 · 属性命中 {sum(attributes.values())} · 榜单命中 {sum(rankings.values())}"),
        node('div', '虚拟库结果', **{'class': 'text-h6 mb-2'}),
        table(['类型', '名称', '命中', '状态'], results or [('—', '尚未启用虚拟库，请到配置页勾选后保存。', '0', '等待配置')]),
        node('p', '以上命中为同步范围；播放端按当前 Emby 用户权限过滤后分页。空库与无权限库不展示。', **{'class': 'text-caption'}),
        node('VExpansionPanels', content=[panel('故障详情与运行日志', [
            node('div', '榜单数据源', **{'class': 'text-h6'}),
            table(['榜单', '状态', '来源或错误'], source_rows or [('—', '暂无榜单记录', '—')]),
            node('div', '最近 50 条日志', **{'class': 'text-h6 mt-3'}),
            table(['时间', '级别', '内容'], logs or [('—', 'INFO', '暂无日志')])])], variant='accordion'),
    ], **{'class': 'pa-3'})]
