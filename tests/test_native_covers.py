"""Real local Emby-shaped HTTP origin: native/virtual covers and playback together."""
import asyncio
import base64
import io
import gzip
import json
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import httpx
from fastapi import FastAPI
from PIL import Image
from test_coverstudio import make_plugin, poster
from test_virtual_library import _FakeRequest


@pytest.fixture
def origin():
    class Origin(BaseHTTPRequestHandler):
        calls = []
        libraries = [{'Id': 'libA', 'Name': '同名影院'}, {'ItemId': 'libB', 'Name': '同名影院'}]
        items = [{'Id': f'm{i:03}', 'Name': f'{i:03}', 'Type': 'Movie', 'ImageTags': {}} for i in range(70)]
        items += [{'Id': 'broken', 'Name': 'A坏图', 'Type': 'Movie', 'ImageTags': {'Primary': 'broken'}},
                  {'Id': 'good', 'Name': 'B好图', 'Type': 'Movie', 'ImageTags': {'Primary': 'good'}},
                  {'Id': 'other', 'Name': 'C另一张', 'Type': 'Movie', 'ImageTags': {'Primary': 'other'}}]
        art = {'good': poster('red'), 'other': poster('green')}
        current = poster('blue')
        current_mime = 'image/png'
        current_b = poster('purple')
        current_b_mime = 'image/png'
        fail_post = 0
        legacy = False
        revoked = set()
        me_unsupported = False
        compress_art = False
        by_parent = {}
        posted = {}

        def log_message(self, *_): pass

        def reply(self, status, payload=b'', mime='application/json', headers=None):
            if not isinstance(payload, bytes): payload = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            for key, value in (headers or {}).items(): self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD': self.wfile.write(payload)

        def do_GET(self):
            url = urlsplit(self.path); path = url.path; query = parse_qs(url.query)
            token = self.headers.get('X-Emby-Token') or query.get('api_key', [''])[0]
            self.calls.append(('GET', path, query, token))
            if path == '/System/Info': return self.reply(200, {'ServerName': 'fixture'})
            if token in self.revoked: return self.reply(401)
            if path in {'/Library/VirtualFolders/Query', '/Library/VirtualFolders'}:
                if token != 'admin': return self.reply(403)
                if path.endswith('/Query') and self.legacy: return self.reply(404)
                start = int(query.get('StartIndex', ['0'])[0])
                return self.reply(200, self.libraries if self.legacy else
                    {'Items': self.libraries[start:start+1], 'TotalRecordCount': len(self.libraries)})
            if path == '/Users/Me':
                return self.reply(500) if self.me_unsupported else self.reply(200, {'Id': token})
            if path in {'/Users/alice', '/Users/bob'}:
                return self.reply(200, {'Id': token}) if path.rsplit('/', 1)[1] == token else self.reply(403)
            if path in {'/Users/alice/Views', '/Users/bob/Views'}:
                return self.reply(200, {'Items': [], 'TotalRecordCount': 0}) if path.split('/')[2] == token else self.reply(403)
            if path == '/Items' or path.startswith('/Users/') and path.endswith('/Items'):
                # Emby Fields is an enum: ImageTags / BackdropImageTags are not valid fields.
                if 'ImageTags' in query.get('Fields', [''])[0]: return self.reply(400)
                items = self.by_parent.get(query.get("ParentId", [""])[0], self.items)
                if token == 'bob': items = [i for i in items if i['Id'] == 'other']
                if 'Ids' in query:
                    ids = query['Ids'][0].split(','); items = [i for i in items if i['Id'] in ids]
                if 'ImageTypes' in query:
                    kind = query['ImageTypes'][0]
                    items = [i for i in items if (i.get('ImageTags') or {}).get(kind)]
                total = len(items); limit = int(query.get('Limit', ['48'])[0])
                return self.reply(200, {'Items': items[:limit], 'TotalRecordCount': total})
            if path.startswith('/Items/') and '/Images/' in path:
                identifier, kind = path.split('/')[2], path.split('/')[4]
                if identifier == 'libA': return self.reply(200, self.current, self.current_mime)
                if identifier == 'libB': return self.reply(200, self.current_b, self.current_b_mime)
                if token == 'bob' and identifier != 'other': return self.reply(403)
                if kind == 'Backdrop': return self.reply(404)
                if identifier == 'broken': return self.reply(200, b'<html>bad image</html>', 'text/html')
                if identifier in self.art:
                    return self.reply(200, gzip.compress(self.art[identifier]), 'image/png', {'Content-Encoding':'gzip'}) if self.compress_art else self.reply(200, self.art[identifier], 'image/png')
                return self.reply(404)
            if path == '/Videos/good/stream':
                return self.reply(302, headers={'Location': 'https://original-cdn.example/video?sign=keep', 'Accept-Ranges': 'bytes'})
            if path == '/Items/good/PlaybackInfo':
                return self.reply(200, {'MediaSources': [{'Id': 'original-source', 'Path': 'https://original-cdn.example/video'}]})
            return self.reply(404)

        def do_POST(self):
            path = urlsplit(self.path).path
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            token = self.headers.get('X-Emby-Token')
            self.calls.append(('POST', path, body, token))
            if path not in {'/Items/libA/Images/Primary', '/Items/libB/Images/Primary'} or token != 'admin': return self.reply(403)
            if self.fail_post: return self.reply(self.fail_post)
            value = base64.b64decode(body, validate=True)
            with Image.open(io.BytesIO(value)) as image: image.load()
            if path == '/Items/libA/Images/Primary': Origin.current, Origin.current_mime = value, self.headers['Content-Type']
            else: Origin.current_b, Origin.current_b_mime = value, self.headers['Content-Type']
            self.posted[path] = value
            return self.reply(204)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield Origin, f'http://127.0.0.1:{server.server_port}'
    server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.fixture
def setup(tmp_path, origin):
    p, module, _ = make_plugin(tmp_path)
    source, address = origin
    p._gateway_client_cache = module.EmbyClient(address, 'admin', timeout=5)
    index = {i['Id']: p._compact_proxy_item(i) for i in source.items}
    p._proxy_item_index = index
    view = p._make_virtual_view('attribute:remux', '同名影院', 'attribute', list(index), index, '2026-09-13')
    p._virtual_views = {view['id']: view}
    yield p, view, source
    p.stop_service()


def wait_job(studio):
    deadline = time.monotonic()+15
    while studio.job['running'] and time.monotonic() < deadline: time.sleep(.01)
    assert not studio.job['running'], studio.job
    return studio.job


def test_native_and_virtual_match_same_artwork_beyond_missing_prefix(setup):
    p, view, source = setup; s = p._cover_studio
    membership = list(view['item_ids'])
    native = s.load_native()
    assert len(native) == 2 and native[0]['name'] == native[1]['name']
    assert native[0]['key'] != native[1]['key']
    opts = s.options(view, {'sort': 'name', 'style': 'wall', 'animated': False})
    virtual_art, notices = s.admin_artwork(view, opts)
    native_view = s.view(native[0]['key'])
    native_art, native_notices = s.admin_artwork(native_view, opts)
    assert virtual_art == native_art and len(native_art) == 2
    # Large upstream originals are normalized before caching; source identity remains distinct.
    decoded = [Image.open(io.BytesIO(data)).convert('RGB') for data in native_art]
    assert decoded[0].getpixel((0, 0))[0] > decoded[0].getpixel((0, 0))[1]
    assert decoded[1].getpixel((0, 0))[1] > decoded[1].getpixel((0, 0))[0]
    assert not notices and not native_notices and native_view['total_count'] == 73
    assert 'good' in s.select_ids(view, opts)  # after seventy empty entries
    assert native_view['id'] == 'libA' and p._virtual_views[view['id']]['item_ids'] == membership
    for target in [view, native_view]:
        mime, image = s.encode(target, opts, virtual_art)
        assert mime == 'image/png'
        with Image.open(io.BytesIO(image)) as png: assert png.size == (640, 360)
    assert all(call[0] == 'GET' for call in source.calls)


@pytest.mark.parametrize('animated', [False, True])
def test_native_preview_generate_publish_backup_restore(setup, animated):
    p, _, source = setup; s = p._cover_studio
    p._virtual_views = {}  # Native generation also works before any virtual libraries exist.
    key = s.load_native()[0]['key']; original = source.current
    p._saved_config['cover_studio']['history_limit'] = 1
    opts = s.options({}, {'animated': animated, 'sort': 'name'})
    s.save_options(key, opts)
    preview = s.preview(key, opts)
    assert preview['artwork_count'] == 2 and preview['total_count'] == 73
    s.start_generate(key)
    assert wait_job(s)['failed'] == 0 and source.current == original
    assert not [c for c in source.calls if c[0] == 'POST']
    s.start_generate(key, publish=True)
    assert wait_job(s)['failed'] == 0 and source.current != original
    assert source.current_mime == ('image/gif' if animated else 'image/png')
    with Image.open(io.BytesIO(source.current)) as generated:
        assert getattr(generated, 'n_frames', 1) == (10 if animated else 1)
    backup = next(row for row in s.state()['history'] if row.get('purpose') == 'before_native_publish')
    assert s._file('history', backup['id'], '.image').read_bytes() == original
    s.action({'action': 'restore_native_image', 'id': backup['id']})
    assert source.current == original
    posts = [c for c in source.calls if c[0] == 'POST']
    assert len(posts) == 2 and all(c[1] == '/Items/libA/Images/Primary' for c in posts)


@pytest.mark.parametrize('failure', ['removed', 'denied', 'wrong_target', 'backup_failure'])
def test_native_publish_refuses_invalid_targets_and_preserves_original(setup, failure, monkeypatch):
    p, virtual, source = setup; s = p._cover_studio
    key = s.load_native()[0]['key']; original = source.current
    if failure == 'wrong_target':
        with pytest.raises(ValueError): s.start_generate(virtual['key'], publish=True)
        assert not [c for c in source.calls if c[0] == 'POST']
        return
    if failure == 'removed': source.libraries = []
    if failure == 'denied': source.fail_post = 403
    if failure == 'backup_failure':
        monkeypatch.setattr(s, '_write_index', lambda *_: (_ for _ in ()).throw(OSError('fixture disk full')))
    s.start_generate(key, publish=True)
    assert wait_job(s)['failed'] == 1 and source.current == original
    assert s.job['errors']
    if failure != 'denied': assert not [c for c in source.calls if c[0] == 'POST']


def test_native_legacy_list_and_no_images_no_overwrite(setup):
    p, _, source = setup; s = p._cover_studio
    source.legacy = True
    key = s.load_native()[0]['key']; original = source.current
    source.art = {}
    s.start_generate(key, publish=True)
    assert wait_job(s)['failed'] == 1 and source.current == original
    assert not [c for c in source.calls if c[0] == 'POST']


def test_native_generation_and_real_302_playback_permissions_together(setup):
    p, view, source = setup; s = p._cover_studio
    s.save_options(view['key'], s.options(view, {'sort': 'name', 'animated': False}))
    key = s.load_native()[0]['key']
    s.start_generate(key)
    async def check():
        request = _FakeRequest('/cover', headers={'X-Emby-Token': 'alice'})
        response = await p._studio_gateway_cover(request, view)
        assert response.status_code == 200 and response.headers['cache-control'] == 'private, no-cache'
        scoped = [v for k, v in s.art_cache.items() if k[0] == 'user']
        # The permitted red source survives normalization in this user's cache.
        colors = [Image.open(io.BytesIO(data)).convert('RGB').getpixel((0, 0))
                  for value in scoped for data in value[1]]
        assert any(red > green for red, green, _ in colors)
        bob = await p._studio_gateway_cover(_FakeRequest('/cover', headers={'X-Emby-Token': 'bob'}), view)
        assert bob.body != response.body and bob.headers['etag'] != response.headers['etag']
        source.revoked.add('alice'); request.headers['If-None-Match'] = response.headers['etag']
        denied = await p._studio_gateway_cover(request, view)
        assert denied.status_code == 200 and denied.body.startswith(b'\x89PNG')
        assert denied.body != response.body and denied.headers['cache-control'] == 'private, no-store'
        source.revoked.clear()
        app = FastAPI(); sys.modules['app.factory'].app = app
        p._install_gateway_routes()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as http:
            headers = {'X-Emby-Token': 'alice', 'Range': 'bytes=10-20'}
            response = await http.get('/Videos/good/stream', headers=headers)
            assert response.status_code == 302
            assert response.headers['location'] == 'https://original-cdn.example/video?sign=keep'
            response = await http.get('/Items/good/PlaybackInfo', headers=headers)
            assert response.status_code == 200 and response.json()['MediaSources'][0]['Id'] == 'original-source'
            response = await http.get(f"/Items/{view['id']}/Images/Primary", headers={'X-Emby-Token': 'bob'})
            assert response.status_code == 200 and response.content == bob.body
        p._remove_gateway_routes()
    asyncio.run(check())
    assert wait_job(s)['failed'] == 0
    assert not [c for c in source.calls if c[0] == 'POST']


def test_gateway_retries_after_corrupt_image_and_empty_art_cache(setup):
    p, view, source = setup; s = p._cover_studio
    s.save_options(view['key'], s.options(view, {'animated': False}))
    async def check():
        request = _FakeRequest('/cover', headers={'X-Emby-Token': 'alice'})
        saved = source.art; source.art = {}
        empty = await p._studio_gateway_cover(request, view)
        source.art = saved
        # Simulate expiry without changing membership, metadata or scheme.
        with s.lock:
            s.art_cache = {key: (0, *value[1:]) for key, value in s.art_cache.items()}
        request.headers['If-None-Match'] = empty.headers['etag']
        restored = await p._studio_gateway_cover(request, view)
        assert restored.status_code == 200 and restored.body != empty.body
        assert restored.headers['etag'] != empty.headers['etag']
    asyncio.run(check())
