"""Resource bounds and interrupted operations use isolated local fixtures."""
import io
import threading
import urllib.error
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest

from test_coverstudio import make_plugin, poster
from test_gateway_runtime import load_plugin
from test_virtual_library import _FakeRequest


@pytest.fixture
def module():
    return load_plugin()


def test_cache_evicts_least_recent_by_image_bytes(module):
    cache = module.ImageCache(max_entries=3, max_bytes=8)
    cache['a'] = ('image/png', b'aaa')
    cache['b'] = ('image/png', b'bbb')
    assert cache['a'][1] == b'aaa'
    cache['c'] = ('image/gif', b'cccc')
    assert list(cache) == ['a', 'c'] and cache.byte_size == 7
    cache['a'] = ('image/png', b'a')
    assert cache.byte_size == 5
    cache['oversized'] = ('image/png', b'x' * 9)
    assert 'oversized' not in cache and cache.byte_size == 5
    cache.clear()
    assert not cache and cache.byte_size == 0


def test_artwork_cache_counts_nested_images_and_expires_on_insert(module, monkeypatch):
    clock = [0]
    cache_module = __import__(module.ImageCache.__module__, fromlist=['time'])
    monkeypatch.setattr(cache_module.time, 'monotonic', lambda: clock[0])
    cache = module.ImageCache(max_bytes=6, ttl=60)
    cache['a'] = (0, [b'aa', b'bb'], ['notice'])
    clock[0] = 59
    assert cache['a'][1] == [b'aa', b'bb']
    clock[0] = 60
    assert cache.get('a') is None and cache.byte_size == 0
    cache['b'] = (60, [b'aaa', b'bbb'])
    cache['c'] = (60, [b'c'])
    assert list(cache) == ['c'] and cache.byte_size == 1


def test_cache_concurrent_reads_writes_and_clear_respect_budget(module):
    cache = module.ImageCache(max_entries=7, max_bytes=64)
    def worker(index):
        for turn in range(150):
            cache[(index, turn % 9)] = ('image/png', b'x' * 11)
            cache.get((index, (turn - 1) % 9))
            if turn % 23 == 0:
                cache.clear()
            assert cache.byte_size <= 64
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))
    assert len(cache) <= 5 and cache.byte_size == len(cache) * 11


def test_warm_cover_does_not_wait_for_cold_cover(module):
    plugin = module.MediaArchiver()
    warm = {'key': 'warm', 'name': 'Warm', 'item_ids': ['1']}
    cold = {'key': 'cold', 'name': 'Cold', 'item_ids': ['2']}
    plugin._render_cover_animated = lambda _: ('image/png', b'warm')
    request = _FakeRequest('/cover')
    assert plugin._virtual_cover_response(request, warm).body == b'warm'
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        assert release.wait(3)
        return 'image/png', b'cold'
    plugin._render_cover_animated = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(plugin._virtual_cover_response, request, cold)
        try:
            assert entered.wait(2)
            response = pool.submit(plugin._virtual_cover_response, request, warm).result(timeout=1)
            assert response.body == b'warm' and not task.done()
        finally:
            release.set()
        assert task.result(timeout=2).body == b'cold'


def test_reset_during_render_does_not_repopulate_cache(module):
    plugin = module.MediaArchiver()
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        assert release.wait(3)
        return 'image/png', b'old'
    plugin._render_cover_animated = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(plugin._virtual_cover_response, _FakeRequest('/cover'),
                           {'key': 'cold', 'name': 'Cold', 'item_ids': []})
        try:
            assert entered.wait(2)
            plugin._refresh_cover_tags()
        finally:
            release.set()
        assert task.result(timeout=2).body == b'old'
    assert not plugin._cover_cache


@pytest.mark.parametrize('target', ['emby', 'ranking'])
def test_metadata_response_is_bounded_before_parsing(module, monkeypatch, target):
    opened = []
    class Response(io.BytesIO):
        status = 200
        def read(self, size=-1):
            opened.append(size)
            assert size == 9
            return super().read(size)
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **k: Response(b'x' * 20))
    if target == 'emby':
        monkeypatch.setattr(module.EmbyClient, '_discover_api_root', lambda self: self.base_url)
        client = module.EmbyClient('http://local.invalid', 'fixture')
        client.MAX_RESPONSE_BYTES = 8
        call = lambda: client.request_json('GET', '/Items')
    else:
        client = module.RankingFetcher('key', 'api.themoviedb.org', 'zh-CN', ['US'], 20, 5)
        client.MAX_RESPONSE_BYTES = 8
        call = lambda: client._request(client.tmdb_base)
    with pytest.raises(ValueError, match='超过'):
        call()
    assert opened == [9]


def test_broken_pagination_stops_instead_of_growing_forever(module, monkeypatch):
    monkeypatch.setattr(module.EmbyClient, '_discover_api_root', lambda self: self.base_url)
    client = module.EmbyClient('http://local.invalid', 'fixture')
    calls = []
    def page(method, path, params):
        calls.append(params['StartIndex'])
        return {'Items': [{'Id': 'a'}, {'Id': 'b'}], 'TotalRecordCount': 999999}
    client.request_json = page
    with pytest.raises(ValueError, match='分页未前进'):
        client.library_items()
    assert calls == [0, 2]


def test_pagination_preserves_progressing_pages(module, monkeypatch):
    monkeypatch.setattr(module.EmbyClient, '_discover_api_root', lambda self: self.base_url)
    client = module.EmbyClient('http://local.invalid', 'fixture')
    client.request_json = lambda method, path, params: {
        'Items': [{'Id': str(params['StartIndex'])}], 'TotalRecordCount': 3}
    assert [item['Id'] for item in client.library_items()] == ['0', '1', '2']


def test_pagination_without_total_continues_full_pages(module, monkeypatch):
    monkeypatch.setattr(module.EmbyClient, '_discover_api_root', lambda self: self.base_url)
    client = module.EmbyClient('http://local.invalid', 'fixture')
    calls = []
    def page(method, path, params):
        start = params['StartIndex']
        calls.append(start)
        return {'Items': [{'Id': str(i)} for i in range(start, min(start + params['Limit'], 301))]}
    client.request_json = page
    assert len(client.library_items()) == 301 and calls == [0, 300]


def test_cancel_interrupts_ranking_retry_wait(module, monkeypatch):
    client = module.RankingFetcher('key', 'api.themoviedb.org', 'zh-CN', ['US'], 20, 5)
    entered = threading.Event()
    class Cancellation(threading.Event):
        def wait(self, timeout=None):
            entered.set()
            return super().wait(timeout)
    client.cancel_event = Cancellation()
    attempts = []
    def fail(*a, **k):
        attempts.append(1)
        raise urllib.error.HTTPError('https://fixture.invalid', 429, 'retry', {'Retry-After': '5'}, None)
    monkeypatch.setattr(module.urllib.request, 'urlopen', fail)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(client._request, client.tmdb_base)
        assert entered.wait(2)
        client.cancel_event.set()
        with pytest.raises(CancelledError):
            task.result(timeout=1)
    assert attempts == [1]


def test_history_invalid_image_leaves_no_original(tmp_path):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    with pytest.raises(Exception):
        studio._history_entry(view, studio.options(view), 'image/png', b'invalid', 'batch')
    assert not list(tmp_path.rglob('*.image')) and not list(tmp_path.rglob('*.jpg'))


def test_history_thumbnail_write_failure_cleans_original(tmp_path, monkeypatch):
    plugin, _, view = make_plugin(tmp_path)
    studio = plugin._cover_studio
    write = studio._atomic
    def fail(path, payload):
        if path.suffix == '.jpg':
            raise OSError('fixture disk full')
        write(path, payload)
    monkeypatch.setattr(studio, '_atomic', fail)
    with pytest.raises(OSError, match='disk full'):
        studio._history_entry(view, studio.options(view), 'image/png', poster(), 'batch')
    assert not list(tmp_path.rglob('*.image')) and not list(tmp_path.rglob('*.tmp'))


def test_history_budget_counts_only_retained_entries(tmp_path):
    plugin, _, _ = make_plugin(tmp_path)
    studio = plugin._cover_studio
    studio.plugin._saved_config['cover_studio'] = {'history_limit': 1}
    mib = 1024 * 1024
    rows = [{'id': 'a', 'batch': 'current', 'size': 1 * mib},
            {'id': 'b', 'batch': 'old', 'size': 255 * mib},
            {'id': 'c', 'batch': 'current', 'size': 1 * mib}]
    assert [row['id'] for row in studio._trim_history(rows)] == ['a', 'c']


def test_history_budget_skips_oversized_entry_without_starving_small_rows(tmp_path):
    plugin, _, _ = make_plugin(tmp_path)
    rows = [{'id': 'a', 'batch': 'current', 'size': 257 * 1024 * 1024},
            {'id': 'b', 'batch': 'current', 'size': 1}]
    assert plugin._cover_studio._trim_history(rows) == [rows[1]]


@pytest.mark.parametrize('folder,suffix', [('history', '.image'), ('fonts', '.ttf'), ('backups', '.json')])
def test_symlinked_data_folder_cannot_escape_plugin_root(tmp_path, folder, suffix):
    plugin_root = tmp_path / 'plugin'
    outside = tmp_path / 'outside'
    plugin_root.mkdir()
    outside.mkdir()
    identifier = 'a' * 32
    protected = outside / (identifier + suffix)
    protected.write_bytes(b'outside')
    (plugin_root / folder).symlink_to(outside, target_is_directory=True)
    plugin, _, _ = make_plugin(plugin_root)
    with pytest.raises(ValueError, match='文件目录超出'):
        plugin._cover_studio._file(folder, identifier, suffix)
    assert protected.read_bytes() == b'outside'
