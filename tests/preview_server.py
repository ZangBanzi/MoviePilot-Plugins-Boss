"""Random loopback native MoviePilot QA host, never installed in the plugin."""
import json
import socket
import sys
import time
import types
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
import uvicorn
from test_gateway_runtime import load_plugin

ROOT = Path(__file__).resolve().parents[1]


def build_app():
    module = load_plugin()
    plugin = module.MediaArchiver()
    data = [{'Id': 'm1', 'Name': 'Alpha', 'Type': 'Movie', 'Path': '/Alpha.Remux.mkv',
             'MediaSources': [{'MediaStreams': [{'Type': 'Video', 'Width': 3840, 'Height': 2160}]}]}]
    class Client:
        api_root = 'http://qa.invalid'
        def library_items(self): time.sleep(.15); return data
        def request_json(self, *args, **kwargs): return {'ServerName': '本地 Emby 替身', 'Version': 'fixture'}
    plugin._create_clients = lambda: [(Client(), '本地 Emby 替身')]
    plugin._server_options = lambda: [{'title': '本地 Emby 替身', 'value': '本地 Emby 替身'}]
    plugin._install_gateway_routes = lambda: None  # QA tests host-rendering only.
    plugin.init_plugin({'virtual_enabled': True, 'auto_sync': False, 'daily_sync_enabled': True})
    app = FastAPI()
    failure = {'page': False}
    def admin(request: Request):
        if request.headers.get('Authorization') != 'Bearer local-test': raise HTTPException(403)
    sys.modules['app.db.user_oper'].get_current_active_superuser = admin
    for api in plugin.get_api():
        route = dict(api)
        route.pop('auth')
        route['path'] = '/api/v1/plugin/MediaArchiver' + route['path']
        app.add_api_route(**route, response_model=None)

    @app.get('/api/v1/plugin/page/MediaArchiver', dependencies=[Depends(admin)])
    def page():
        if failure['page']:
            failure['page'] = False
            raise HTTPException(503)
        return {'render_mode': plugin.get_render_mode()[0], 'page': plugin.get_page()}

    @app.get('/api/v1/plugin/form/MediaArchiver', dependencies=[Depends(admin)])
    def form():
        fields, defaults = plugin.get_form()
        return {'render_mode': plugin.get_render_mode()[0], 'conf': fields, 'model': {**defaults, **plugin._saved_config}}

    @app.put('/api/v1/plugin/MediaArchiver', dependencies=[Depends(admin)])
    async def save(request: Request):
        plugin.init_plugin(await request.json())
        return {'success': True, 'message': '配置已保存'}

    @app.post('/api/v1/qa/fail-page', dependencies=[Depends(admin)])
    def fail_page(): failure['page'] = True; return {'success': True}

    app.mount('/', StaticFiles(directory=ROOT / '.work/preview', html=True), name='qa-static')
    app.add_event_handler('shutdown', plugin.stop_service)
    return app


if __name__ == '__main__':
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    (ROOT / '.work').mkdir(exist_ok=True)
    (ROOT / '.work/preview-port.txt').write_text(str(port))
    uvicorn.Server(uvicorn.Config(build_app(), log_level='warning')).run(sockets=[sock])
