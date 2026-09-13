"""日志回归：正常空结果、失效来源、混合榜保留/恢复清理。"""
from pathlib import Path
from test_virtual_library import _load_module, _FakeScanEmby

m = _load_module(Path(__file__).resolve().parents[1])
def fetcher():
    return m.RankingFetcher("key", "api.themoviedb.org", "zh-CN", ["US"], 20, 10)
f = fetcher()
f._provider_id = lambda *args: 1
f._tmdb = lambda *args: {"results": [], "total_results": 0}
assert f._get("tencent_kids").ok
assert f._get("tencent_mixed").ok and f._get("tencent_mixed").complete
assert not f._get("tencent_mixed").entries
for invalid in ({}, {"results": []}, {"results": [], "total_results": 5}, {"results": [{}]}):
    try: f._discover_items(invalid)
    except RuntimeError: pass
    else: raise AssertionError(invalid)

f = fetcher()
for key in f.DOUBAN_COLLECTIONS:
    f._results[key] = m.RankingResult(False, set(), error="HTTP 404")
f._results["douban_showing"] = m.RankingResult(True, {m.RankEntry(media_type="Movie", tmdb="2")}, "test")
partial = f._get("douban_mixed")
assert partial.ok and not partial.complete
p = m.MediaArchiver()
p._ranking_enabled = True
p._selected_rankings = {"douban_mixed"}
client = _FakeScanEmby([{"Id": "a", "Type": "Movie", "ProviderIds": {"Tmdb": "1"}},
                        {"Id": "b", "Type": "Movie", "ProviderIds": {"Tmdb": "2"}}])
identity = "Test|" + client.api_root
key = "ranking:douban_mixed"
old = p._make_virtual_view(key, "mixed", "ranking", ["a", "deleted"], client._items, "old")
p._state = {"servers": {identity: {"virtual_views": {key: old}}}}
p._sync_server(client, "Test", {"douban_mixed": partial})
assert p._state["servers"][identity]["virtual_views"][key]["item_ids"] == ["a", "b"]
partial.complete = True
p._sync_server(client, "Test", {"douban_mixed": partial})
assert p._state["servers"][identity]["virtual_views"][key]["item_ids"] == ["b"]
p._sync_server(client, "Test", {"douban_mixed": m.RankingResult(True, set(), empty_valid=True)})
assert p._state["servers"][identity]["virtual_views"][key]["item_ids"] == []

f = fetcher()
f._request = lambda *args, **kwargs: b'<h2>new</h2><a href="/subject/111/">no</a><h2>&#21271;&#32654;&#31080;&#25151;&#27036;</h2><ul><li><a href="https://movie.douban.com/subject/222/">yes</a></li></ul><h2>TOP250</h2><a href="/subject/333/">no</a>'
result = f._douban("douban_north_america")
assert {entry.douban for entry in result.entries} == {"222"}
f._request = lambda *args, **kwargs: b'<h2>Top250</h2><a href="/subject/333/">no</a>'
assert not f._get("douban_north_america").ok
f._request = lambda *args, **kwargs: b'{"movieName":"Title only"}'
assert not f._get("maoyan_movie").ok
f = fetcher()
f._request = lambda *args, **kwargs: b'<script id="__NEXT_DATA__">{"pageData":{"chartTitles":{"edges":[{"node":{"title":{"id":"tt1234567"}}}]}}}</script>'
assert {e.imdb for e in f._get("imdb_popular_movie").entries} == {"tt1234567"}
print("PASS: 空结果/畸形响应、混合榜新增/保留/恢复清理、豆瓣区块隔离、IMDb结构、猫眼证据不足")
