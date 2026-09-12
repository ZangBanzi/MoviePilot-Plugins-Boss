"""Real HTTPX + FastAPI + local HTTP origin regression; no NAS/network credentials."""
import asyncio
import gzip
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brotli
import httpx
import pytest
import zstandard
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import test_virtual_library as legacy


def load_plugin():
    saved = {key: value for key, value in sys.modules.items()
             if key == 'fastapi' or key.startswith('fastapi.') or key == 'starlette' or key.startswith('starlette.')}
    legacy._install_moviepilot_stubs()
    for name in list(sys.modules):
        if name == 'fastapi' or name.startswith('fastapi.') or name == 'starlette' or name.startswith('starlette.'):
            sys.modules.pop(name)
    sys.modules.update(saved)
    default_source = ROOT / 'plugins.v2' / 'mediaarchiver' / '__init__.py'
    if not default_source.exists():
        default_source = ROOT / 'deliverables' / '__init__.py'
    path = Path(os.environ.get('MEDIAARCHIVER_SOURCE', default_source))
    spec = importlib.util.spec_from_file_location('gateway_runtime_plugin', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def plugin_module():
    return load_plugin()


class Origin(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    observed = []
    peers = set()
    slow_started = threading.Event()
    slow_release = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.do_GET()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        cls = type(self)
        cls.peers.add(self.client_address)
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length) if length else b''
        cls.observed.append((self.path, dict(self.headers), body))
        headers = [('Content-Type', 'application/json')]
        status = 200
        if url.path.endswith('/Views'):
            payload = {'Items': [{'Id': 'native', 'Name': '原始媒体库'}], 'TotalRecordCount': 1}
            data = json.dumps(payload, ensure_ascii=False).encode()
            mode = query.get('encoding', [''])[0]
            if mode == 'retry' and self.headers.get('cache-control') != 'no-cache':
                data = b'broken response'
            elif mode == 'broken':
                data = b'not json or any compression'
            elif mode == 'utf16':
                data = json.dumps(payload).encode('utf-16')
            elif mode in ('gzip', 'gzip_noheader', 'gzip_stale', 'stacked'):
                if mode != 'gzip_stale':
                    data = gzip.compress(data)
                if mode != 'gzip_noheader':
                    headers.append(('Content-Encoding', 'gzip'))
                if mode == 'stacked':
                    data = brotli.compress(data)
                    headers[-1] = ('Content-Encoding', 'gzip, br')
            elif mode in ('br', 'br_noheader', 'br_wrongheader'):
                data = brotli.compress(data)
                if mode != 'br_noheader':
                    headers.append(('Content-Encoding', 'gzip' if mode == 'br_wrongheader' else 'br'))
            elif mode.startswith('deflate'):
                import zlib
                data = zlib.compress(data)
                if mode == 'deflate_raw':
                    data = data[2:-4]
                if mode != 'deflate_noheader':
                    headers.append(('Content-Encoding', 'deflate'))
            elif mode == 'zstd_unknownsize':
                data = zstandard.ZstdCompressor(write_content_size=False).compress(data)
                headers.append(('Content-Encoding', 'zstd'))
            headers += [('ETag', 'old-origin'), ('Cache-Control', 'public,max-age=600')]
        elif url.path in ('/Items', '/Users/u/Items'):
            ids = query.get('Ids', ['m1,m2'])[0].split(',')
            allowed = {'m2'} if self.headers.get('X-Emby-Token') == 'limited' else {'m1', 'm2'}
            data = json.dumps({'Items': [{'Id': i, 'Type': 'Movie', 'MediaSources': [{'Id': 'src-' + i, 'Path': '/original/' + i}]} for i in ids if i in allowed]}).encode()
        elif url.path == '/cookie':
            headers += [('Set-Cookie', 'sid=user-a; Path=/'), ('Set-Cookie', 'other=user-a; Path=/')]
            data = b'{}'
        elif url.path == '/slow':
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'A'); self.wfile.flush()
            cls.slow_started.set()
            cls.slow_release.wait(3)
            try:
                self.wfile.write(b'B'); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif url.path == '/range':
            status = 206
            data = b'4567'
            headers = [('Content-Type', 'application/octet-stream'), ('Content-Range', 'bytes 4-7/10')]
        elif url.path == '/redirect':
            status = 302; data = b''
            headers.append(('Location', 'https://cdn.invalid/original?sign=untouched'))
        elif url.path == '/unauthorized':
            status = 401; data = gzip.compress(b'not-json-login-error')
            headers.append(('Content-Encoding', 'gzip'))
        elif url.path in ('/status/204', '/status/304'):
            status = int(url.path.rsplit('/', 1)[1]); data = b''
            headers.append(('ETag', 'valid-tag'))
        else:
            data = json.dumps({'path': self.path, 'cookie': self.headers.get('Cookie', ''),
                'encoding': self.headers.get('Accept-Encoding', ''), 'body': body.decode(),
                'token': self.headers.get('X-Emby-Token', '')}).encode()
            if url.path == '/QuickConnect/Enabled':
                time.sleep(0.04)
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)
            self.wfile.flush()


@pytest.fixture(scope='module')
def origin():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    server.daemon_threads = True
    server.request_queue_size = 128
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    Origin.slow_release.set()
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def setup_gateway(module, origin):
    app = FastAPI()
    @app.get('/api/probe')
    def probe():
        return {'moviepilot': True}
    sys.modules['app.factory'].app = app
    p = module.MediaArchiver()
    p._enabled = p._attribute_enabled = True
    p._gateway_client_cache = types.SimpleNamespace(api_root=origin)
    view_id = p._view_id('remux')
    p._virtual_views = {view_id: {'id': view_id, 'name': 'Remux专区', 'key': 'remux',
        'collection_type': 'movies', 'item_ids': ['m1', 'm2'], 'cover_tag': 'abcd'}}
    p._proxy_item_index = {'m1': {'Type': 'Movie', 'Name': 'First'}, 'm2': {'Type': 'Movie', 'Name': 'Second'}}
    p._install_gateway_routes()
    return p, app, view_id


async def close_pool(p):
    if p._httpx_client is not None:
        await p._httpx_client.aclose()
    p._remove_gateway_routes()


def test_ponytail_sync_and_cover_do_not_block_browsing(plugin_module, origin):
    # 排名匹配仍兼容别名、同名电影的年份和重复条目。
    module = plugin_module
    items = [
        {'Id': 'a', 'Name': '同名', 'OriginalTitle': '同名', 'Type': 'Movie',
         'ProductionYear': 2000, 'ProviderIds': {'TheMovieDB': '1', 'B.G.M': '2'}},
        {'Id': 'b', 'Name': '同名', 'Type': 'Movie', 'ProductionYear': 2020},
    ]
    index = module.LibraryIndex(iter(items + [items[0], {'Name': '无Id'}]))
    assert index.match([module.RankEntry(media_type='Movie', tmdb='1')]) == {'a'}
    assert index.match([module.RankEntry(media_type='Movie', bangumi='2')]) == {'a'}
    assert index.match([module.RankEntry(media_type='Movie', title='同名', year=2020)]) == {'b'}
    assert index.match([module.RankEntry(title='同名')]) == set()

    # 同步生成新快照时，网关仍能取得旧快照；发布后才整体切换。
    from concurrent.futures import ThreadPoolExecutor
    p = module.MediaArchiver()
    old_index = {'old': {'Name': '旧索引'}}
    p._proxy_item_index = old_index
    started, release = threading.Event(), threading.Event()
    compact = p._compact_proxy_item

    def slow_compact(item):
        started.set()
        assert release.wait(3), '测试未及时释放同步任务'
        return compact(item)

    p._compact_proxy_item = slow_compact
    scan = types.SimpleNamespace(api_root=origin, library_items=lambda: items + [items[0]])
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(p._sync_server, scan, 'test', {})
        try:
            assert started.wait(2)
            available = p._proxy_lock.acquire(blocking=False)
            if available:
                p._proxy_lock.release()
            assert available, '整理快照不应长时间占用浏览锁'
            assert p._proxy_item_index is old_index
        finally:
            release.set()
        assert task.result(timeout=2)['scanned'] == 2
    assert set(p._proxy_item_index) == {'a', 'b'}

    async def check_cover():
        p, app, view_id = setup_gateway(module, origin)
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def slow_render(view):
            calls.append(view['id'])
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3), '封面绘图阻塞了请求事件循环'
            return b'\x89PNG\r\n\x1a\n'

        p._render_cover_png = slow_render
        tasks = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            try:
                tasks = [asyncio.create_task(client.get(f'/Items/{view_id}/Images/Primary')) for _ in range(2)]
                await asyncio.wait_for(started.wait(), timeout=2)
                assert all(not task.done() for task in tasks)
                health = await asyncio.wait_for(client.get('/__mediaarchiver__/health'), timeout=1)
                assert health.status_code == 200 and not release.is_set()
            finally:
                release.set()
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                await close_pool(p)
        assert all(isinstance(response, httpx.Response) and response.status_code == 200 for response in responses)
        assert calls == [view_id], '同一封面并发请求只应绘制一次'

    asyncio.run(check_cover())


@pytest.mark.parametrize('encoding', ['', 'gzip', 'gzip_noheader', 'gzip_stale', 'stacked',
    'br', 'br_noheader', 'br_wrongheader', 'deflate', 'deflate_noheader', 'deflate_raw', 'utf16', 'zstd_unknownsize', 'retry'])
def test_json_views(plugin_module, origin, encoding):
    async def check():
        p, app, view_id = setup_gateway(plugin_module, origin)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            response = await client.get('/Users/u/Views', params={'encoding': encoding}, headers={'X-Emby-Token': 'u', 'Accept-Encoding': 'gzip, br', 'If-None-Match': 'old-origin'})
            assert response.status_code == 200, response.text
            assert view_id in {i['Id'] for i in response.json()['Items']}
            assert 'etag' not in response.headers
            assert 'content-encoding' not in response.headers
            assert response.headers['cache-control'] == 'private, no-store'
            assert int(response.headers['content-length']) == len(response.content)
            sent = Origin.observed[-1][1]
            assert sum(k.casefold() == 'accept-encoding' for k in sent) == 1
            assert not any(k.casefold() == 'if-none-match' for k in sent)
        await close_pool(p)
    asyncio.run(check())


def test_invalid_json_diagnostics_and_retry(plugin_module, origin):
    async def check():
        p, app, _ = setup_gateway(plugin_module, origin)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            for _ in range(2):
                response = await client.get('/Users/u/Views?encoding=broken&api_key=never-log-this')
                assert response.status_code == 502
            health = (await client.get('/__mediaarchiver__/health')).json()
            assert health['version'] == '4.3.10'
            assert len(health['code_sha256']) == 64
            assert health['performance']['failures'] == 2
            assert health['performance']['suppressed_errors'] == 1
            assert health['performance']['decode_retries'] == 2
            assert health['last_error']['stage'] == 'decode_json'
            assert 'never-log-this' not in json.dumps(health) + json.dumps(list(p._logs))
        await close_pool(p)
    asyncio.run(check())


def test_cookies_headers_and_proxy_transparency(plugin_module, origin):
    async def check():
        p, app, _ = setup_gateway(plugin_module, origin)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            r = await client.get('/cookie')
            assert len(r.headers.get_list('set-cookie')) == 2
            client.cookies.clear()  # simulate a second user at the same shared gateway
            r = await client.get('/echo', headers={'X-Emby-Token': 'user-b', 'Accept-Encoding': ''})
            assert r.json()['cookie'] == ''
            assert r.json()['token'] == 'user-b'
            r = await client.get('/range', headers={'Range': 'bytes=4-7'})
            assert r.status_code == 206 and r.content == b'4567'
            assert r.headers['content-range'] == 'bytes 4-7/10'
            r = await client.get('/redirect')
            assert r.status_code == 302 and r.headers['location'].endswith('sign=untouched')
            r = await client.get('/unauthorized')
            assert r.status_code == 401 and r.content == b'not-json-login-error'
            r = await client.post('/echo', content=b'hello', headers={'Content-Type': 'text/plain'})
            assert r.json()['body'] == 'hello'
            r = await client.get('/emby/Subtitles/%E4%B8%AD%E6%96%87%2Ftest?x=%2F%2B')
            assert r.json()['path'] == '/Subtitles/%E4%B8%AD%E6%96%87%2Ftest?x=%2F%2B'
            for status in (204, 304):
                r = await client.get('/status/' + str(status))
                assert r.status_code == status and r.content == b''
            r = await client.head('/echo')
            assert r.status_code == 200 and r.content == b''
            r = await client.head('/Users/u/Views')
            assert r.status_code == 200 and r.content == b'' and int(r.headers['content-length']) > 0
        await close_pool(p)
    asyncio.run(check())


def test_routes_paging_and_user_isolation(plugin_module, origin):
    async def check():
        p, app, view_id = setup_gateway(plugin_module, origin)
        route_count = len(app.routes)
        for _ in range(4):
            p._remove_gateway_routes(); p._install_gateway_routes()
        assert len(app.routes) == route_count
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            assert (await client.get('/api/probe')).json() == {'moviepilot': True}
            assert (await client.get('/api/missing')).status_code == 404
            for route in ('/QuickConnect/Enabled', '/Trailers', '/MediaInfo'):
                assert (await client.get(route)).status_code == 200
            for route in ('/Users/u/Items', '/Items'):
                response = await client.get(route, params={'UserId': 'u', 'ParentId': view_id, 'StartIndex': 1, 'Limit': 1}, headers={'X-Emby-Token': 'limited'})
                payload = response.json()
                assert [x['Id'] for x in payload['Items']] == ['m2']
                assert payload['Items'][0]['MediaSources'][0]['Path'] == '/original/m2'
            response = await client.get('/Items/Latest', params={'UserId': 'u', 'ParentId': view_id})
            assert isinstance(response.json(), list)
        await close_pool(p)
    asyncio.run(check())


def test_first_chunk_arrives_and_disconnect_releases(plugin_module, origin):
    async def check():
        from starlette.requests import Request
        p, _, _ = setup_gateway(plugin_module, origin)
        Origin.slow_release.clear(); Origin.slow_started.clear()
        request = Request({'type': 'http', 'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
            'path': '/slow', 'raw_path': b'/slow', 'query_string': b'', 'headers': [],
            'server': ('gateway', 80), 'client': ('127.0.0.1', 1)}, receive=lambda: None)
        request._body = b''
        r = await p._gateway_stream_response(request, '/slow')
        try:
            first = await asyncio.wait_for(anext(r.body_iterator), timeout=0.8)
            assert first == b'A', 'proxy must not wait for EOF or 1MiB'
            await r.body_iterator.aclose()
            assert p._gateway_metrics['active_streams'] == 0
        finally:
            Origin.slow_release.set()
            await close_pool(p)
    asyncio.run(check())


def test_disconnect_before_body_iteration(plugin_module, origin):
    async def check():
        from starlette.requests import Request
        p, _, _ = setup_gateway(plugin_module, origin)
        request = Request({'type': 'http', 'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
            'path': '/echo', 'query_string': b'', 'headers': [], 'server': ('gateway', 80)}, receive=lambda: None)
        request._body = b''
        r = await p._gateway_stream_response(request, '/echo')
        async def send(_):
            raise OSError('client disconnected before response start')
        async def receive():
            return {'type': 'http.disconnect'}
        try:
            await r({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        except Exception:
            pass
        assert p._gateway_metrics['active_streams'] == 0
        await close_pool(p)
    asyncio.run(check())


def test_concurrency_and_keepalive(plugin_module, origin):
    async def check():
        p, app, _ = setup_gateway(plugin_module, origin)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            await client.get('/echo')
            before = len(Origin.peers)
            for _ in range(5):
                assert (await client.get('/echo')).status_code == 200
            assert len(Origin.peers) == before, 'sequential requests should reuse TCP connection'
            started = time.monotonic()
            results = await asyncio.gather(*(client.get('/QuickConnect/Enabled') for _ in range(32)))
            elapsed = time.monotonic() - started
            assert all(r.status_code == 200 for r in results)
            assert p._gateway_metrics['peak_inflight'] > 1
            assert p._gateway_metrics['active_streams'] == 0
            assert p._gateway_metrics['failures'] == 0
            print(f'32 concurrent local HTTP requests: {elapsed:.3f}s; peak={p._gateway_metrics["peak_inflight"]}')
        await close_pool(p)
    asyncio.run(check())


def test_raw_header_bytes_survive(plugin_module, origin):
    async def check():
        p, app, _ = setup_gateway(plugin_module, origin)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            value = '播放器'.encode('utf-8')
            r = await client.get('/echo', headers=[(b'X-Emby-Device-Name', value)])
            assert r.status_code == 200
            assert Origin.observed[-1][1]['x-emby-device-name'].encode('latin1') == value
            from starlette.responses import Response
            result = p._apply_response_headers(Response(b'hello'), [('Content-Disposition', value.decode('latin1'))])
            assert dict(result.raw_headers)[b'content-disposition'] == value
        await close_pool(p)
    asyncio.run(check())


def test_json_size_limit_and_selection_cache(plugin_module, origin):
    p, _, view_id = setup_gateway(plugin_module, origin)
    old_limit = plugin_module.MediaArchiver.MAX_JSON_BYTES
    plugin_module.MediaArchiver.MAX_JSON_BYTES = 4096
    try:
        with pytest.raises(ValueError):
            p._decode_buffered_body([('Content-Encoding', 'gzip')], gzip.compress(b'[' + b' ' * 10000 + b']'))
    finally:
        plugin_module.MediaArchiver.MAX_JSON_BYTES = old_limit
    view = p._virtual_views[view_id]
    before = p._select_view_item_ids(view, {'Limit': ['1']}, False)
    assert before == (['m1'], 2)
    p._proxy_item_index = {'m1': {'Type': 'Movie', 'Name': 'Z'}, 'm2': {'Type': 'Movie', 'Name': 'A'}}
    assert p._select_view_item_ids(view, {'Limit': ['1']}, False) == (['m2'], 2)
    for n in range(100):
        p._select_view_item_ids(view, {'SearchTerm': [str(n)]}, False)
    assert len(p._selection_cache) <= p.SELECTION_CACHE_LIMIT


def test_websocket_real_upstream(plugin_module):
    import websockets
    async def check():
        async def echo(connection):
            async for message in connection:
                await connection.send(message)
        async with websockets.serve(echo, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            p, _, _ = setup_gateway(plugin_module, f'http://127.0.0.1:{port}')
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            class Socket:
                url = types.SimpleNamespace(path='/socket', query='api_key=fake')
                scope = {'subprotocols': []}
                headers = {}
                async def accept(self, **_): pass
                async def close(self, **_): pass
                async def receive(self): return await incoming.get()
                async def send_text(self, message): await outgoing.put(message)
                async def send_bytes(self, message): await outgoing.put(message)
            task = asyncio.create_task(p._emby_gateway_websocket(Socket()))
            try:
                for message in ('hello', b'world'):
                    await incoming.put({'type': 'websocket.receive', 'text' if isinstance(message, str) else 'bytes': message})
                    assert await asyncio.wait_for(outgoing.get(), 2) == message
                await incoming.put({'type': 'websocket.disconnect'})
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


def test_real_animated_cover_and_static_client_formats(plugin_module, origin):
    """用实际 Pillow 编码经过 FastAPI 路由，验证封面不会污染播放或缓存格式。"""
    import io
    from PIL import Image

    async def check():
        p, app, view_id = setup_gateway(plugin_module, origin)
        p._virtual_views[view_id]['key'] = 'attribute:remux'
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
                url = f'/Items/{view_id}/Images/Primary'
                gif = await client.get(url)
                assert gif.status_code == 200 and gif.headers['content-type'] == 'image/gif'
                with Image.open(io.BytesIO(gif.content)) as image:
                    assert image.n_frames == 12 and image.info['loop'] == 0
                tags = {gif.headers['etag']}
                for requested, expected in [('png', 'PNG'), ('jpg', 'JPEG'), ('webp', 'WEBP')]:
                    response = await client.get(url, params={'Format': requested})
                    assert response.status_code == 200
                    with Image.open(io.BytesIO(response.content)) as image:
                        assert image.format == expected and not getattr(image, 'is_animated', False)
                    assert response.headers['etag'] not in tags
                    tags.add(response.headers['etag'])
                    cached = await client.get(url, params={'Format': requested},
                                              headers={'If-None-Match': response.headers['etag']})
                    assert cached.status_code == 304
                head = await client.head(url)
                assert head.status_code == 200 and not head.content
                assert int(head.headers['content-length']) == len(gif.content)
                assert (await client.get('/__mediaarchiver__/health')).status_code == 200
                assert (await client.get(url)).content == gif.content
        finally:
            await close_pool(p)
    asyncio.run(check())


def test_gif_encoding_failure_preserves_valid_png(plugin_module, monkeypatch):
    import io
    from PIL import Image
    original = Image.Image.save

    def fail_gif(self, fp, format=None, **kwargs):
        if format == 'GIF':
            raise OSError('simulated GIF encoder failure')
        return original(self, fp, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, 'save', fail_gif)
    p = plugin_module.MediaArchiver()
    mime, data = p._render_cover_animated({'key': 'attribute:remux', 'item_ids': []})
    assert mime == 'image/png'
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        assert image.format == 'PNG'
