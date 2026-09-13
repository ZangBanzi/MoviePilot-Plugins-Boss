"""Local QA host. Uses constructed metadata/posters and only binds loopback.

Not installed as a plugin component. Run after `npm run build:preview`.
"""
import argparse
import json
import sys
import types
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from test_coverstudio import make_plugin, poster
from test_native_covers import origin


def build_app(work: Path):
    root = Path(__file__).resolve().parents[1]
    p, module, _ = make_plugin(work / 'preview-data' / uuid.uuid4().hex)
    p._saved_config['tmdb_api_key'] = ''
    p._virtual_views = {}
    for key, name in [('4k', '4K 极清影院'), ('remux', 'Remux 原盘'), ('tvb', 'TVB 港剧'), ('hdr', 'HDR 视界')]:
        view = p._make_virtual_view('attribute:'+key, name, 'attribute', ['m1', 'm2'], p._proxy_item_index, '2026-09-13')
        p._virtual_views[view['id']] = view
    p._proxy_status.update({'running': True, 'message': '本地构造样本 · 未连接 NAS', 'api_port': 3334, 'public_port': 8098})
    p._runtime.update({'state': 'completed', 'message': '构造元数据用于页面验证'})
    qa_origin = origin.__wrapped__()
    _, emby_address = next(qa_origin)
    p._gateway_client_cache = module.EmbyClient(emby_address, 'admin', timeout=5)
    native_artwork = p._cover_studio.admin_artwork
    p._cover_studio.admin_artwork = lambda view, opts: native_artwork(view, opts) if view.get('native') else ([poster('#143c57'), poster('#653f36'), poster('#27463f')], [])
    p._cover_studio._save_studio_original = p._cover_studio._save_studio
    app = FastAPI()
    app.add_event_handler('shutdown', qa_origin.close)
    user_module = types.ModuleType('app.db.user_oper')
    def admin(request: Request):
        if request.headers.get('Authorization') != 'Bearer local-test':
            raise HTTPException(status_code=403)
    user_module.get_current_active_superuser = admin
    sys.modules['app.db.user_oper'] = user_module
    for api in p.get_api():
        route = dict(api); route.pop('auth')
        route['path'] = '/api/v1/plugin/MediaArchiver'+route['path']
        app.add_api_route(**route)

    @app.post('/api/v1/plugin/MediaArchiver')
    async def save_config(request: Request):
        admin(request)
        cfg = p._validate_studio_config(await request.json())
        p._saved_config = cfg
        p.update_config(cfg)
        p._refresh_cover_tags()
        return {'success': True}

    @app.get('/host-vue.js')
    def host_vue():
        return FileResponse(root / 'frontend/node_modules/vue/dist/vue.esm-browser.prod.js', media_type='application/javascript')

    @app.get('/federation')
    def federation():
        return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="margin:0"><div id="fed"></div><script type="module">
import * as Vue from '/host-vue.js';
import {init,get} from '/api/v1/plugin/file/mediaarchiver/dist/assets/remoteEntry.js';
init({vue:{[Vue.version]:{get:()=>()=>Vue,loaded:true}}});
const api={get:async p=>(await fetch('/api/v1/'+p,{headers:{Authorization:'Bearer local-test'}})).json(),post:async(p,d)=>(await fetch('/api/v1/'+p,{method:'POST',headers:{Authorization:'Bearer local-test','Content-Type':'application/json'},body:JSON.stringify(d)})).json()};
const factory=await get('./Page');const Page=factory();
Vue.createApp({render:()=>Vue.h(Page,{api})}).mount('#fed');
window.__federationReady=true;
</script></body></html>''')

    app.mount('/api/v1/plugin/file/mediaarchiver/dist/assets', StaticFiles(directory=root / 'plugins.v2/mediaarchiver/dist/assets'))
    app.mount('/', StaticFiles(directory=work / 'preview', html=True))
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--work', type=Path, default=Path(__file__).resolve().parents[2] / '.work')
    args = parser.parse_args()
    import socket
    sock = socket.socket()
    sock.bind(('127.0.0.1', args.port))
    port = sock.getsockname()[1]
    args.work.mkdir(parents=True, exist_ok=True)
    (args.work / 'preview-port.txt').write_text(str(port), encoding='utf-8')
    print(f'QA preview: http://127.0.0.1:{port}', flush=True)
    server = uvicorn.Server(uvicorn.Config(build_app(args.work), log_level='warning'))
    server.run(sockets=[sock])
