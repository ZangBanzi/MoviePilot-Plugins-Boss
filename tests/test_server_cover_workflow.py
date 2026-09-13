"""Real HTTP fixtures for server batches and the Emby 4.9 Users/Me failure."""
import asyncio
import io
import sys
import threading
import time
from concurrent.futures import CancelledError

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from test_native_covers import origin, setup, wait_job
from test_gateway_runtime import close_pool
from test_coverstudio import make_plugin, poster


def test_server_batch_uses_each_library_and_preserves_gateway(setup):
    p, view, source = setup
    s = p._cover_studio
    source.by_parent = {'libA': [i for i in source.items if i['Id'] == 'good'],
                        'libB': [i for i in source.items if i['Id'] == 'other']}
    before_ids = list(view['item_ids'])
    client = p._gateway_client_cache
    result = s.action({'action': 'generate', 'server': s.server_id(client), 'all_server': True, 'publish': True})
    assert result['total'] == 3
    assert wait_job(s)['failed'] == 0
    assert set(source.posted) == {'/Items/libA/Images/Primary', '/Items/libB/Images/Primary'}
    assert len(set(source.posted.values())) == 2
    results = s.job['results']
    assert [r['artwork_count'] for r in results if r['native']] == [1, 1]
    assert next(r for r in results if not r['native'])['artwork_count'] == 2
    assert all(r['mime'] == 'image/png' for r in results if r['native'])
    assert p._gateway_client_cache is client and view['item_ids'] == before_ids
    assert sum(r.get('purpose') == 'before_native_publish' for r in s.state()['history']) == 2
    assert all('/Images/Primary' in c[1] for c in source.calls if c[0] == 'POST')


def test_second_server_same_library_ids_never_switches_playback(setup):
    p, virtual, first = setup
    s = p._cover_studio
    second_origin = origin.__wrapped__()
    second, address = next(second_origin)
    try:
        gateway = p._gateway_client_cache
        alternate = type(gateway)(address, 'admin', 5)
        p._create_clients = lambda: [(gateway, '主服务器'), (alternate, '第二服务器')]
        owner = s.server_id(alternate)
        s.action({'action': 'load_native', 'server': owner})
        key = next(v['key'] for v in s.native_views if v['server'] == owner)
        assert key.startswith('native:'+owner+':')
        with pytest.raises(ValueError, match='不能跨服务器'):
            s.action({'action': 'preview', 'server': owner, 'key': virtual['key']})
        s.action({'action': 'generate', 'server': owner, 'all_server': True, 'publish': True})
        assert wait_job(s)['failed'] == 0 and s.job['total'] == 2
        assert len(second.posted) == 2 and not first.posted
        assert p._gateway_client_cache is gateway
        p._create_clients = lambda: [(gateway, '主服务器')]
        with pytest.raises(ValueError, match='服务器已移除'):
            s.action({'action': 'generate', 'key': key, 'publish': True})
    finally:
        second_origin.close()


@pytest.mark.parametrize('compressed', [False, True])
def test_emby49_images_without_headers_and_revocation(setup, compressed):
    p, view, source = setup
    source.me_unsupported = True
    source.compress_art = compressed
    s = p._cover_studio
    app = FastAPI(); sys.modules['app.factory'].app = app
    p._install_gateway_routes()

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as http:
            headers = {'X-Emby-Token': 'alice'}
            response = await http.get('/Users/alice/Views', headers=headers)
            item = next(i for i in response.json()['Items'] if i['Id'] == view['id'])
            tag = item['ImageTags']['Primary']
            assert len(tag) == 32 and 'alice' not in tag
            path = f"/Items/{view['id']}/Images/Primary"
            # Mirrors an Emby <img>: only ImageTag, no API token or auth header.
            picture = await http.get(path, params={'tag': tag})
            assert picture.status_code == 200 and picture.headers['x-mediaarchiver-cover'] == 'artwork'
            with Image.open(io.BytesIO(picture.content)) as image:
                assert image.n_frames == 10 and image.info['loop'] == 0
                image.load()
            assert not any(c[1] == '/Users/Me' for c in source.calls)
            assert not any(c[3] == 'admin' and ('/Images/' in c[1] or c[1].startswith('/Users/') or c[1] == '/Items') for c in source.calls)
            # Header and query forms of the same current token share only its identity.
            direct = await http.get(path, params={'api_key': 'alice'})
            assert direct.content == picture.content
            await http.get('/Users/bob/Views', headers={'X-Emby-Token': 'bob'})
            other = await http.get(path, params={'tag': tag, 'api_key': 'bob'})
            assert other.content != picture.content and other.headers['x-mediaarchiver-cover'] == 'artwork'
            source.revoked.add('alice')
            revoked = await http.get(path, params={'tag': tag}, headers={'If-None-Match': picture.headers['etag']})
            assert revoked.status_code == 200 and revoked.headers['cache-control'] == 'private, no-store'
            assert revoked.content != picture.content
            with Image.open(io.BytesIO(revoked.content)) as image: assert image.format == 'PNG'
            source.revoked.clear()
            recovered = await http.get(path, params={'tag': tag})
            assert recovered.content == picture.content
            s.plugin._cover_tickets[tag]['time'] -= 3601
            expired = await http.get(path, params={'tag': tag})
            assert expired.content != picture.content
            tampered = await http.get(path, params={'tag': 'a'*32})
            assert tampered.content == expired.content
        await close_pool(p)

    try:
        asyncio.run(check())
    finally:
        p._remove_gateway_routes()


@pytest.mark.parametrize('count', [0, 1])
def test_no_fake_motion_with_insufficient_artwork(tmp_path, count):
    p, _, view = make_plugin(tmp_path)
    mime, data = p._cover_studio.encode(view, p._cover_studio.options(view), [poster()]*count, 'gif')
    assert mime == 'image/png'
    with Image.open(io.BytesIO(data)) as image: assert not getattr(image, 'is_animated', False)


def test_old_sync_cannot_resume_or_replace_new_runtime(tmp_path):
    p, module, _ = make_plugin(tmp_path)
    started, release = threading.Event(), threading.Event()
    class Fetcher:
        def fetch(self, _):
            started.set(); assert release.wait(3)
            return {}
    p._ranking_enabled = True; p._selected_rankings = {'tencent_mixed'}
    p._create_clients = lambda: []
    p._make_fetcher = lambda: Fetcher()
    p._run_lock.acquire()
    old = p._sync_cancel
    worker = threading.Thread(target=p._sync_worker, args=('rebuild', old))
    worker.start(); assert started.wait(2)
    old.set(); p._sync_cancel = threading.Event(); p._stopping = False
    p._runtime = {'state': 'new-lifecycle'}
    release.set(); worker.join(3)
    assert not worker.is_alive() and not p._run_lock.locked()
    assert p._runtime == {'state': 'new-lifecycle'} and p._ranking_cache == {}
    f = module.RankingFetcher('key', 'api.themoviedb.org', 'zh-CN', ['US'], 20, 5)
    f.cancel_event = old
    with pytest.raises(CancelledError): f._get('tencent_mixed')


def test_cold_gallery_keeps_original_302_responsive(setup):
    p, original, source = setup
    index = p._proxy_item_index
    p._virtual_views = {}
    for i in range(8):
        view = p._make_virtual_view(f'fixture:{i}', f'影院 {i}', 'attribute', ['good', 'other'], index, 'now')
        p._virtual_views[view['id']] = view
    app = FastAPI(); sys.modules['app.factory'].app = app
    p._install_gateway_routes()
    original_encode = p._cover_studio.encode

    async def check():
        rendering = asyncio.Event()
        loop = asyncio.get_running_loop()
        def encode(*args):
            loop.call_soon_threadsafe(rendering.set)
            return original_encode(*args)
        p._cover_studio.encode = encode
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as http:
            tasks = [asyncio.create_task(http.get(f'/Items/{identifier}/Images/Primary',
                       headers={'X-Emby-Token': 'alice'})) for identifier in p._virtual_views]
            await asyncio.wait_for(rendering.wait(), 8)
            playback = await asyncio.wait_for(http.get('/Videos/good/stream'), 1.5)
            assert playback.status_code == 302 and playback.headers['location'] == 'https://original-cdn.example/video?sign=keep'
            health = await asyncio.wait_for(http.get('/__mediaarchiver__/health'), 1.5)
            assert health.status_code == 200
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 18)
            for response in responses:
                assert response.status_code == 200 and response.headers['x-mediaarchiver-cover'] == 'artwork'
                with Image.open(io.BytesIO(response.content)) as image:
                    assert image.n_frames > 1
                    image.load()
        await close_pool(p)
    asyncio.run(check())


def test_native_refresh_is_atomic_and_failure_invalidates_targets(setup):
    p, _, _ = setup
    s = p._cover_studio
    key = s.load_native()[0]['key']
    request = s._admin_request
    entered, release = threading.Event(), threading.Event()
    def delayed(method, path, *args, **kwargs):
        if path == '/Library/VirtualFolders/Query':
            entered.set(); assert release.wait(3)
        return request(method, path, *args, **kwargs)
    s._admin_request = delayed
    thread = threading.Thread(target=s.load_native)
    thread.start(); assert entered.wait(2)
    try:
        assert s.preview(key, {})['artwork_count'] == 2
    finally:
        release.set(); thread.join(3)
    assert s.view(key)['native']
    s._admin_request = lambda *args, **kwargs: (403, 'application/json', b'')
    with pytest.raises(ValueError, match='HTTP 403'): s.load_native()
    with pytest.raises(ValueError, match='有效目标'): s.view(key)
