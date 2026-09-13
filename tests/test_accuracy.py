"""严格识别回归。全部使用构造元数据，不访问外部站点或 NAS。"""
import copy
import json
from pathlib import Path
from test_virtual_library import _load_module, _FakeScanEmby


def main():
    m = _load_module(Path(__file__).resolve().parents[1])
    p = m.MediaArchiver()
    cases = [
        ({"Path": "/Remux/4K/HDR/普通电影.mkv", "Overview": "Dolby Vision Atmos Remux"}, set()),
        ({"Path": "/电影/Film_REMUX_2160p_DV_Atmos.mkv"}, {"remux", "4k", "dolby_vision", "hdr", "atmos"}),
        ({"Path": "/电影/notremux.mkv"}, set()),
        ({"Path": "https://example.invalid/film.mkv?quality=Remux.4K.HDR.Atmos"}, set()),
        ({"Path": "/Film.4K.mkv", "MediaStreams": [{"Type": "Video", "Width": 1920, "Height": 1080}]}, set()),
        ({"MediaStreams": [{"Type": "Video", "Width": 3840, "Height": 1600}]}, {"4k"}),
        ({"MediaStreams": [{"Type": "Subtitle", "Width": 3840, "Height": 2160}]}, set()),
        ({"MediaStreams": [{"Type": "Video", "DvProfile": 8}]}, {"dolby_vision", "hdr"}),
        ({"MediaStreams": [{"Type": "Video", "DvProfile": None, "VideoRange": "SDR"}], "Path": "/Film.DV.HDR.mkv"}, set()),
        ({"MediaStreams": [{"Type": "Video", "VideoRangeType": "DOVIWithHDR10"}]}, {"dolby_vision", "hdr"}),
        ({"MediaStreams": [{"Type": "Video", "ColorTransfer": "smpte2084"}]}, {"hdr"}),
        ({"MediaStreams": [{"Type": "Video", "Profile": "Main 10", "BitDepth": 10}]}, set()),
        ({"MediaStreams": [{"Type": "Audio", "Codec": "truehd", "Channels": 8}]}, set()),
        ({"MediaStreams": [{"Type": "Audio", "CodecTag": "EAC3-JOC"}]}, {"atmos"}),
        ({"MediaStreams": [{"Type": "Audio", "Codec": "aac"}], "Path": "/Film.Atmos.mkv"}, set()),
        ({"Studios": [{"Name": "ViuTV"}], "Tags": ["港剧"], "Overview": "TVB演员参演", "Path": "/TVB/Film.mkv"}, set()),
        ({"Studios": [{"Name": "Television Broadcasts Limited"}]}, {"tvb"}),
        ({"Tags": ["myTV SUPER", "埋堆堆"]}, set()),
        ({"Tags": ["虚拟库：Remux"]}, {"remux"}),
        ({"Path": "/Film.Remux.mkv", "Tags": ["排除Remux专区"]}, set()),
        ({"MediaSources": [
            {"Path": "/Film.4K.DV.mkv", "MediaStreams": [{"Type": "Video", "Width": 1920, "Height": 1080, "VideoRange": "SDR"}]},
            {"Path": "/Film.mkv", "MediaStreams": [{"Type": "Video", "Width": 3840, "Height": 1600}]},
        ]}, {"4k"}),
    ]
    for item, expected in cases:
        snapshot = copy.deepcopy(item)
        assert p._classify(item) == expected, (item, p._classify(item), expected)
        assert item == snapshot

    items = [
        {"Id": "a", "Type": "Movie", "Name": "同名电影", "ProductionYear": 2020, "ProviderIds": {"Tmdb": "111", "Imdb": "tt0000001"}},
        {"Id": "b", "Type": "Movie", "Name": "另一电影", "ProductionYear": 2020, "ProviderIds": {"Tmdb": "222", "Imdb": "tt0000002"}},
        {"Id": "s", "Type": "Series", "Name": "同名电影", "ProductionYear": 2020, "ProviderIds": {"Tmdb": "111"}},
    ]
    index = m.LibraryIndex(items)
    for entry, expected in [
        (m.RankEntry(media_type="Movie", tmdb="111"), {"a"}),
        (m.RankEntry(media_type="Series", tmdb="111"), {"s"}),
        (m.RankEntry(tmdb="111"), set()),
        (m.RankEntry(media_type="Movie", tmdb="333", title="同名电影", year=2020), set()),
        (m.RankEntry(media_type="Movie", tmdb="111", imdb="tt0000002"), set()),
        (m.RankEntry(media_type="Movie", title="同名电影"), set()),
        (m.RankEntry(media_type="Movie", title="同名电影", year=2020), {"a"}),
    ]:
        assert index.match([entry]) == expected, entry
    ambiguous = m.LibraryIndex(items + [{"Id": "other", "Type": "Movie", "Name": "同名电影", "ProductionYear": 2020}])
    assert ambiguous.match([m.RankEntry(media_type="Movie", title="同名电影", year=2020)]) == set()

    fetcher = m.RankingFetcher("key", "api.themoviedb.org", "zh-CN", ["US", "HK"], 20, 10)
    chart = {"@type": "ItemList", "itemListElement": [{"item": {"url": "https://www.imdb.com/title/tt0000001/"}}]}
    fetcher._request = lambda *args, **kwargs: ('related tt0000002<script type="application/ld+json">' + json.dumps(chart) + '</script>').encode()
    assert {x.imdb for x in fetcher._imdb(True).entries} == {"tt0000001"}
    fetcher._request = lambda *args, **kwargs: b'<html>related tt0000002</html>'
    assert not fetcher._get("imdb_popular_movie").ok
    fetcher._json = lambda *args, **kwargs: {"errors": [{"message": "disabled"}]}
    assert not fetcher._get("anilist_popular").ok
    fetcher._provider_id = lambda *args: 8
    def discover(path, query):
        start = 1 if query["watch_region"] == "US" else 21
        return {"results": [{"id": n, "title": str(n), "popularity": n} for n in range(start, start + 20)]}
    fetcher._tmdb = discover
    assert {int(x.tmdb) for x in fetcher._platform("netflix_movie").entries} == set(range(21, 41))

    p._ranking_enabled = True
    p._selected_rankings = {"imdb_popular_movie"}
    client = _FakeScanEmby(items)
    identity = "Test|" + client.api_root
    key = "ranking:imdb_popular_movie"
    old = p._make_virtual_view(key, "IMDb", "ranking", {"a", "deleted"}, client._items, "yesterday")
    p._state = {"servers": {identity: {"virtual_views": {key: old}}}}
    p._sync_server(client, "Test", {})
    assert p._state["servers"][identity]["virtual_views"][key]["item_ids"] == ["a"]
    p._state["servers"][identity]["virtual_views"][key].pop("recognition_policy")
    p._sync_server(client, "Test", {})
    assert p._state["servers"][identity]["virtual_views"][key]["item_ids"] == []
    print("PASS: 属性冲突、多版本、TVB、身份消歧、榜单主体、来源失败与缓存迁移")


if __name__ == "__main__":
    main()
