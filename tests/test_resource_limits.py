"""Metadata and ranking resource bounds retained after cover removal."""
import io
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
import pytest
from test_gateway_runtime import load_plugin

@pytest.fixture
def module(): return load_plugin()

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
