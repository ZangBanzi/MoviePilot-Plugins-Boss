"""Cover studio integration: real Pillow/FastAPI, no NAS or external source access."""
import asyncio
import base64
import copy
import io
import json
import threading
import time
import types
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Any

from test_gateway_runtime import load_plugin
from test_virtual_library import _FakeRequest


def poster(color='navy'):
    image = Image.new('RGB', (360, 540), color)
    draw = ImageDraw.Draw(image)
    draw.ellipse((45, 65, 315, 335), fill='#6688aa')
    draw.polygon([(0, 540), (140, 200), (360, 450), (360, 540)], fill='#192639')
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def make_plugin(tmp_path):
    module = load_plugin()
    class HostResponse(BaseModel):
        success: bool = True
        message: str = ''
        data: Any = None
    module.schemas.Response = HostResponse
    p = module.MediaArchiver()
    p._studio_data_dir = tmp_path
    p._saved_config = p.get_form()[1]
    p._saved_config.update({'auto_sync': False, 'tmdb_api_key': 'local-fixture-secret'})
    p._enabled = p._attribute_enabled = True
    p._proxy_item_index = {'m1': {'Name': '公开影片', 'Type': 'Movie', 'DateCreated': '2026-01-01'},
                           'm2': {'Name': '受限影片', 'Type': 'Movie', 'DateCreated': '2026-02-01'}}
    view = p._make_virtual_view('attribute:remux', 'Remux 专区', 'attribute', ['m1', 'm2'], p._proxy_item_index, '2026-09-13')
    p._virtual_views = {view['id']: view}
    return p, module, view


@pytest.fixture
def plugin(tmp_path):
    p, module, view = make_plugin(tmp_path)
    yield p, module, view
    p.stop_service()


@pytest.mark.parametrize('style', ['stack', 'diagonal', 'wall', 'minimal'])
def test_real_layouts_and_animation(plugin, style):
    p, _, view = plugin
    studio = p._cover_studio
    options = studio.options(view, {'style': style, 'title': '我的私人影院', 'source': 'brand'})
    mime, payload = studio.encode(view, options, [poster()], 'gif')
    assert mime == 'image/gif'
    with Image.open(io.BytesIO(payload)) as image:
        assert image.n_frames == 12 and image.size == (640, 360) and image.info['loop'] == 0
        image.seek(0)
        first = image.convert('RGB').tobytes()
        image.seek(6)
        assert first != image.convert('RGB').tobytes()


def test_layouts_differ_and_exports_honor_resolution(plugin):
    p, _, view = plugin
    payloads = []
    for style in ['stack', 'diagonal', 'wall', 'minimal']:
        options = p._cover_studio.options(view, {'style': style, 'resolution': 1280})
        mime, payload = p._cover_studio.encode(view, options, [poster()], 'webp')
        with Image.open(io.BytesIO(payload)) as image:
            assert image.size == (1280, 720) and image.format == 'WEBP'
        payloads.append(payload)
    assert len(set(payloads)) == 4


def test_option_save_preserves_config_changes_tags_and_static_mode(plugin):
    p, _, view = plugin
    before = p._virtual_cover_response(_FakeRequest('/cover'), view)
    oldtag = view['cover_tag']
    options = p._cover_studio.options(view, {'animated': False, 'title': '新标题'})
    p._cover_studio.save_options(view['key'], options)
    newview = p._virtual_views[view['id']]
    assert p._saved_config['tmdb_api_key'] == 'local-fixture-secret'
    assert newview['cover_tag'] != oldtag
    assert newview['item_ids'] == view['item_ids']
    assert p._synthetic_view(newview)['ImageTags']['Primary'] == newview['cover_tag']
    after = p._virtual_cover_response(_FakeRequest('/cover'), newview)
    assert after.body.startswith(b'\x89PNG') and before.body != after.body
    assert p._virtual_cover_response(_FakeRequest('/cover', headers={'If-None-Match': after.headers['ETag']}), newview).status_code == 304


def test_global_default_does_not_overwrite_explicit_library(plugin):
    p, _, view = plugin
    s = p._cover_studio
    s.save_options(view['key'], s.options(view, {'title': '独立标题'}))
    s.save_options('', s.options({}, {'title': '全局标题'}), global_scope=True)
    assert s.options(view)['title'] == '独立标题'
    s.action({'action': 'reset_override', 'key': view['key']})
    assert s.options(view)['title'] == '全局标题'


def test_inflight_cover_keeps_its_own_configuration_tag(plugin):
    p, _, view = plugin
    s = p._cover_studio
    snapshot = s.options(view, {'animated': False})
    old_view = dict(view, _cover_options=snapshot)
    s.save_options(view['key'], s.options(view, {'title': '刚保存的新标题', 'animated': False}))
    old = p._virtual_cover_response(_FakeRequest('/cover'), old_view)
    new = p._virtual_cover_response(_FakeRequest('/cover'), view)
    assert old.headers['etag'] != new.headers['etag'] and old.body != new.body


def test_static_setting_respects_unknown_and_gif_format_requests(plugin):
    p, _, view = plugin
    p._cover_studio.save_options(view['key'], p._cover_studio.options(view, {'animated': False}))
    for fmt in ['original', 'gif', 'png']:
        request = _FakeRequest('/cover')
        request.query_params = {'Format': fmt}
        response = p._virtual_cover_response(request, view)
        assert response.body.startswith(b'\x89PNG')


@pytest.mark.parametrize('patch', [{'title_font': '../../secret'}, {'accent': 'red'}, {'title_size': float('nan')}, {'animated': 'false'}, {'resolution': 999}, {'style': 'unknown'}])
def test_invalid_options_rejected(plugin, patch):
    p, _, view = plugin
    with pytest.raises(ValueError):
        p._cover_studio.options(view, patch)


def test_history_generate_restore_download_and_cache_clean(plugin):
    p, _, view = plugin
    s = p._cover_studio
    s.admin_artwork = lambda *_: ([poster()], [])
    options = s.options(view, {'title': '历史标题', 'animated': False})
    s.save_options(view['key'], options)
    s.start_generate()
    assert s.job_lock.acquire(timeout=10)
    s.job_lock.release()
    assert s.job['failed'] == 0 and s.job['done'] == 1
    data = s.state()
    assert len(data['history']) == 1 and all(p['thumbnail'].startswith('data:image/') for p in data['presets'])
    row = data['history'][0]
    image = s.history_image(row['id'])
    assert base64.b64decode(image['image'].split(',')[1]).startswith(b'\x89PNG')
    s.save_options(view['key'], s.options(view, {'title': '后来标题'}))
    s.action({'action': 'restore_history', 'id': row['id']})
    assert s.options(view)['title'] == '历史标题'
    s.action({'action': 'clean_images'})
    assert s.history_image(row['id']) == image
    s.action({'action': 'delete_history', 'id': row['id']})
    assert not s.state()['history']


def test_background_job_is_single_and_cancelable(plugin):
    p, _, view = plugin
    s = p._cover_studio
    entered, release = threading.Event(), threading.Event()
    def art(*_):
        entered.set(); release.wait(4)
        return [], []
    s.admin_artwork = art
    s.start_generate()
    assert entered.wait(2)
    with pytest.raises(ValueError):
        s.start_generate()
    with pytest.raises(ValueError):
        s.save_options(view['key'], s.options(view))
    s.action({'action': 'cancel'})
    release.set()
    assert s.job_lock.acquire(timeout=10)
    s.job_lock.release()
    assert s.job['message'] == '已停止'


def test_presets_backup_restore_validation_and_paths(plugin):
    p, _, view = plugin
    s = p._cover_studio
    preset = s.action({'action': 'save_preset', 'name': '<script>test</script>', 'options': s.options(view)})
    assert len(s.config['presets']) == 1
    s.action({'action': 'delete_preset', 'id': preset['id']})
    assert s.config['presets'] == []
    saved = s.action({'action': 'backup'})
    assert s.action({'action': 'read_backup', 'id': saved['id']})['backup'] == saved['backup']
    clean = p._validate_studio_config(saved['backup']['config'])
    assert clean['tmdb_api_key'] == 'local-fixture-secret'
    with pytest.raises(ValueError):
        p._validate_studio_config({'sync_cron': 'broken'})
    with pytest.raises(ValueError):
        s.action({'action': 'read_backup', 'id': '../../secret'})


def test_font_upload_decode_and_cache_preserves_files(plugin):
    from fontTools import subset
    p, _, view = plugin
    s = p._cover_studio
    font = subset.load_font(str(s.font_path('default')), subset.Options())
    subsetter = subset.Subsetter()
    subsetter.populate(text='我的私人影院ABC ')
    subsetter.subset(font)
    output = io.BytesIO(); font.save(output); font.close()
    result = s.upload_font({'name': '../../my-font.ttf', 'data': base64.b64encode(output.getvalue()).decode()})
    assert result['name'] == 'my-font.ttf'
    assert s.font_path(result['id']).is_file()
    mime, data = s.encode(view, s.options(view, {'title_font': result['id'], 'title': '我的私人影院'}))
    assert mime == 'image/png' and data.startswith(b'\x89PNG')
    s.action({'action': 'clean_fonts'})
    assert s.font_path(result['id']).is_file()
    with pytest.raises(ValueError):
        s.upload_font({'name': 'bad.ttf', 'data': base64.b64encode(b'not a font').decode()})
    with pytest.raises(ValueError):
        s.import_font_url({'url': 'https://127.0.0.1/font.ttf'})


def test_history_pruning_only_selects_entries_before_index_commit(plugin):
    p, _, view = plugin
    s = p._cover_studio
    p._saved_config['cover_studio']['history_limit'] = 1
    payload = poster()
    old = s._history_entry(view, s.options(view), 'image/png', payload, 'old-batch')
    new = s._history_entry(view, s.options(view), 'image/png', payload, 'new-batch')
    assert s._trim_history([new, old]) == [new]
    # A failed index write must not already have deleted the previous history image.
    assert s._file('history', old['id'], '.image').exists()


def test_woff2_upload_and_missing_glyph_fallback(plugin):
    from fontTools import subset
    p, _, view = plugin
    s = p._cover_studio
    font = subset.load_font(str(s.font_path('default')), subset.Options())
    sub = subset.Subsetter(); sub.populate(text='ABC'); sub.subset(font)
    font.flavor = 'woff2'
    output = io.BytesIO(); font.save(output); font.close()
    row = s.upload_font({'name': 'web-font.woff2', 'data': base64.b64encode(output.getvalue()).decode()})
    assert s.text_font_id(row['id'], 'ABC') == row['id']
    assert s.text_font_id(row['id'], '中文') == 'default'


def make_admin_app(p, monkeypatch):
    user_module = types.ModuleType('app.db.user_oper')
    def admin(request: Request):
        if request.headers.get('Authorization') != 'Bearer admin-fixture':
            raise HTTPException(status_code=403)
    user_module.get_current_active_superuser = admin
    monkeypatch.setitem(sys.modules, 'app.db.user_oper', user_module)
    app = FastAPI()
    for item in p.get_api():
        if item['path'].startswith('/studio'):
            route = dict(item)
            route.pop('auth')
            route['path'] = '/api/v1/plugin/MediaArchiver' + route['path']
            app.add_api_route(**route)
    return app


def test_studio_api_admin_auth_and_real_preview(plugin, monkeypatch):
    p, _, view = plugin
    p._cover_studio.admin_artwork = lambda *_: ([], [])
    app = make_admin_app(p, monkeypatch)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
            url = '/api/v1/plugin/MediaArchiver/studio'
            assert (await http.get(url)).status_code == 403
            http.headers['Authorization'] = 'Bearer admin-fixture'
            data = (await http.get(url)).json()
            assert data['success'] and data['data']['version'] == '4.4.1'
            response = await http.post(url+'/action', json={'action': 'preview', 'key': view['key'], 'options': {'source': 'brand'}})
            assert response.status_code == 200 and response.json()['success']
            assert response.json()['data']['image'].startswith('data:image/png;')
            bad = await http.post(url+'/action', json={'action': 'preview', 'options': {'title_font': '../x'}})
            assert bad.status_code == 400
            http.headers['Authorization'] = 'Bearer ordinary-user'
            assert (await http.post(url+'/action', json={'action': 'backup'})).status_code == 403
    asyncio.run(check())


def test_gateway_artwork_permissions_revocation_and_cache(plugin):
    p, module, view = plugin
    p._gateway_client_cache = types.SimpleNamespace(api_root='http://configured-emby:8096', api_key='admin-key-must-not-be-used')
    calls, revoked = [], set()
    red, blue = poster('red'), poster('blue')
    async def fetch(client, method, incoming, headers, body):
        token = headers.get('x-emby-token')
        calls.append((incoming, token))
        assert token in {'alice', 'bob'}
        if token in revoked:
            return 401, [], b''
        if incoming.startswith('/Users/Me'):
            payload = {'Id': token}
        elif incoming.startswith('/Users/'+token+'/Items'):
            ids = ['m1', 'm2'] if token == 'alice' else ['m2']
            payload = {'Items': [{'Id': i, 'ImageTags': {'Primary': i+'tag'}} for i in ids]}
        elif incoming.startswith('/Items/m1/Images/'):
            assert token == 'alice'
            return 200, [], red
        elif incoming.startswith('/Items/m2/Images/'):
            return 200, [], blue
        else:
            raise AssertionError(incoming)
        return 200, [], json.dumps(payload).encode()
    p._fetch_gateway_bytes = fetch
    p._cover_studio.save_options(view['key'], p._cover_studio.options(view, {'sort': 'name', 'animated': False}))
    async def check():
        a = _FakeRequest('/cover', headers={'X-Emby-Token': 'alice'})
        b = _FakeRequest('/cover?UserId=alice', headers={'X-Emby-Token': 'bob'})
        alice = await p._studio_gateway_cover(a, view)
        bob = await p._studio_gateway_cover(b, view)
        assert alice.body != bob.body and alice.headers['etag'] != bob.headers['etag']
        assert alice.headers['cache-control'] == 'private, no-cache'
        before_images = sum('/Images/' in path for path, _ in calls)
        assert (await p._studio_gateway_cover(a, view)).body == alice.body
        assert sum('/Images/' in path for path, _ in calls) == before_images
        revoked.add('alice')
        a.headers['If-None-Match'] = alice.headers['etag']
        assert (await p._studio_gateway_cover(a, view)).status_code == 401
        assert not any('/Users/alice/Items' in path and token == 'bob' for path, token in calls)
    asyncio.run(check())
