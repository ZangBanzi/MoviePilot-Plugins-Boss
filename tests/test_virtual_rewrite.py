"""Virtual-only contract regressions, no MoviePilot/NAS credentials."""
import copy
import json
import threading
from concurrent.futures import CancelledError
import pytest
from test_gateway_runtime import load_plugin

@pytest.fixture
def plugin():
    return load_plugin().MediaArchiver()

def test_native_pages_have_no_cover_dependency(plugin):
    assert plugin.get_render_mode()[0] == 'vuetify'
    form, defaults = plugin.get_form()
    assert form and plugin.get_page()
    assert 'cover_studio' not in defaults
    assert not hasattr(plugin, '_cover_studio')
    assert all('studio' not in api['path'] for api in plugin.get_api())

def test_corrupt_persisted_state_does_not_break_page(plugin):
    plugin._test_data['virtual_state'] = {'servers': {'bad': None, 'old': {'attribute_counts': {'remux': 'broken'}, 'ranking_counts': []}}, 'source_status': [], 'connection_test': ['bad']}
    plugin._test_data['virtual_logs'] = [None, 'bad', {'message': 'ok'}]
    plugin._load_state()
    assert plugin.get_form()[0] and plugin.get_page()
    assert plugin.status().success

def test_failed_sync_still_has_readable_status(plugin):
    plugin._runtime = {'state': 'failed', 'message': 'fixture failure', 'stats': {}}
    response = plugin.status()
    assert response.success and response.data['state'] == 'failed'
    response.data['stats']['scanned'] = 99
    assert plugin._runtime['stats'] == {}

def test_thread_start_failure_releases_sync_lock(plugin, monkeypatch):
    module = __import__(type(plugin).__module__, fromlist=['threading'])
    plugin._enabled = plugin._attribute_enabled = True
    monkeypatch.setattr(module.threading.Thread, 'start', lambda self: (_ for _ in ()).throw(RuntimeError('fixture start failure')))
    result = plugin._start_sync()
    assert not result.success and plugin._runtime['state'] == 'failed'
    assert plugin._run_lock.acquire(False)
    plugin._run_lock.release()

def test_restore_snapshot_preserves_search_metadata(plugin):
    identifier = plugin._view_id('attribute:remux')
    plugin._state = {'servers': {'fixture': {'name': 'fixture', 'virtual_views': {'attribute:remux': {'id': identifier, 'key': 'attribute:remux', 'item_ids': ['a']}}, 'item_index': {'a': {'Id': 'a', 'Name': 'Alpha', 'Type': 'Movie'}}}}}
    plugin._restore_proxy_cache()
    assert plugin._select_view_item_ids(plugin._virtual_views[identifier], {'SearchTerm': ['Alpha']}, False) == (['a'], 1)

def test_explicit_server_does_not_restore_other_server(plugin):
    plugin._emby_servers = ['selected']
    plugin._state = {'servers': {'other': {'name': 'other', 'virtual_views': {'x': {'id': 'x', 'item_ids': ['a']}}}}}
    plugin._restore_proxy_cache()
    assert not plugin._virtual_views

def test_failed_snapshot_build_preserves_state(plugin):
    plugin._attribute_enabled = True
    plugin._enabled_rules = {'remux'}
    class Client:
        api_root = 'http://fixture.invalid'
        def library_items(self): return [{'Id': 'a', 'Name': 'Alpha.Remux', 'Path': '/Alpha.Remux.mkv', 'Type': 'Movie'}]
    before = copy.deepcopy(plugin._state)
    plugin._compact_proxy_item = lambda _: (_ for _ in ()).throw(ValueError('fixture bad metadata'))
    with pytest.raises(ValueError): plugin._sync_server(Client(), 'fixture', {})
    assert plugin._state == before and not plugin._virtual_views

def test_lifecycle_invalidated_during_build_cannot_publish(plugin):
    plugin._attribute_enabled = True
    plugin._enabled_rules = {'remux'}
    class Client:
        api_root = 'http://fixture.invalid'
        def library_items(self): return [{'Id': 'a', 'Type': 'Movie'}]
    event = plugin._sync_cancel
    original = plugin._compact_proxy_item
    def invalidate(item):
        event.set()
        plugin._sync_cancel = threading.Event()
        return original(item)
    plugin._compact_proxy_item = invalidate
    with pytest.raises(CancelledError): plugin._sync_server(Client(), 'fixture', {}, event)
    assert not plugin._virtual_views and plugin._state['servers'] == {}

def test_generated_image_route_is_gone(plugin):
    view = plugin._make_virtual_view('attribute:remux', 'Remux', 'attribute', ['a'], {'a': {'Type': 'Movie'}}, 'now')
    assert plugin._synthetic_view(view)['ImageTags'] == {}
    assert not hasattr(plugin, '_render_cover_png')

@pytest.mark.parametrize('route', ['/Users/u/Items', '/Items'])
def test_authorized_members_are_paged_after_permission_filter(plugin, route):
    import asyncio
    from test_virtual_library import _FakeRequest
    view = {'id': 'view', 'item_ids': ['a', 'b', 'c']}
    plugin._proxy_item_index = {key: {'Name': key} for key in view['item_ids']}
    catalog_module = __import__(type(plugin).__module__ + '.catalog', fromlist=['ViewerCatalog'])
    async def read(self, path, pairs=()):
        if path == '/Users/u': return {'Id': 'u'}
        ids = dict(pairs)['Ids'].split(',')
        return {'Items': [{'Id': key, 'MediaSources': [{'Id': 'source', 'Path': '/original/' + key}]} for key in ids if key in {'b', 'c'}]}
    original = catalog_module.ViewerCatalog._read
    catalog_module.ViewerCatalog._read = read
    try:
        request = _FakeRequest(route, 'UserId=u&StartIndex=1&Limit=1')
        query = {'UserId': ['u'], 'StartIndex': ['1'], 'Limit': ['1']}
        response = asyncio.run(plugin._virtual_items_response(request, route, view, query, False))
        result = json.loads(response.body)
        assert result['TotalRecordCount'] == 2 and result['StartIndex'] == 1
        assert result['Items'] == [{'Id': 'c', 'MediaSources': [{'Id': 'source', 'Path': '/original/c'}]}]
    finally: catalog_module.ViewerCatalog._read = original


def test_old_worker_failure_after_reload_does_not_change_new_status(plugin):
    module = __import__(type(plugin).__module__, fromlist=['threading'])
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    event = plugin._sync_cancel
    def fail():
        entered.set()
        assert release.wait(3)
        raise RuntimeError('old job failure')
    plugin._create_clients = fail
    plugin._run_lock.acquire()
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(plugin._sync_worker, 'rebuild', event)
        try:
            assert entered.wait(2)
            with plugin._proxy_lock:
                event.set()
                plugin._sync_cancel = threading.Event()
                plugin._runtime = {'state': 'idle', 'message': 'new configuration', 'stats': {}}
        finally: release.set()
        task.result(2)
    assert plugin._runtime['message'] == 'new configuration' and not plugin._run_lock.locked()


def test_page_and_form_never_embed_host_api_token(plugin):
    assert 'test-token' not in json.dumps(plugin.get_page()) + json.dumps(plugin.get_form())
    assert plugin.get_api() and all(api.get('dependencies') for api in plugin.get_api())
