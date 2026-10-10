"""Read a virtual catalog through the incoming Emby user's permissions.

No permission cache or administrator credentials: authorization is checked on every
read, before totals and pagination. Only original Emby item objects are returned.
"""
import json
import re
import urllib.parse
from typing import Mapping


class CatalogDenied(Exception):
    def __init__(self, response):
        self.response = response


class ViewerCatalog:
    BATCH_SIZE = 200

    def __init__(self, gateway, request, route):
        self.gateway = gateway
        self.request = request
        self.route = route
        self.client = None
        self.user_id = ''

    async def _read(self, path, pairs=()):
        import asyncio
        gateway = self.gateway
        if self.client is None:
            self.client = gateway._gateway_client_cache or await asyncio.to_thread(gateway._gateway_client)
        auth = [(k, v) for k, v in urllib.parse.parse_qsl(str(self.request.url.query), keep_blank_values=True)
                if k.casefold() in {'api_key', 'apikey', 'x-emby-token', 'x-mediabrowser-token'}]
        incoming = path + '?' + urllib.parse.urlencode(auth + list(pairs))
        target, _ = gateway._upstream_target(self.client, incoming)
        headers = gateway._forward_request_headers(self.request, target, b'', True)
        status, upstream_headers, raw = await gateway._fetch_gateway_bytes(self.client, 'GET', incoming, headers, b'')
        if status != 200:
            # Preserve real authentication and upstream error status; never return an empty success.
            from fastapi import Response
            response = Response(content=raw, status_code=status)
            raise CatalogDenied(gateway._apply_response_headers(response, upstream_headers, False))
        decoded = await asyncio.to_thread(gateway._decode_buffered_body, upstream_headers, raw)
        return json.loads(decoded)

    async def authorize(self):
        match = re.match(r'/Users/([^/]+)/', self.route, re.I)
        query = urllib.parse.parse_qs(str(self.request.url.query))
        requested = match.group(1) if match else self.gateway._query_value(query, 'UserId')
        has_credential = any(value for key, values in query.items()
                             if key.casefold() in {'api_key', 'apikey', 'x-emby-token', 'x-mediabrowser-token'}
                             for value in values)
        has_credential |= any(value and (key.casefold() in {'x-emby-token', 'x-mediabrowser-token', 'cookie'}
                             or key.casefold() in {'authorization', 'x-emby-authorization'}
                             and (re.search(r'\bToken\s*=', value, re.I) or value.lower().startswith(('bearer ', 'basic '))))
                             for key, value in self.request.headers.items())
        if not has_credential:
            raise CatalogDenied(self.gateway._json_response(401, {'error': 'Emby authentication required'}))
        header_user = ''
        for key, value in self.request.headers.items():
            if key.casefold() in {'authorization', 'x-emby-authorization'}:
                found = re.search(r'\bUserId\s*=\s*"?([A-Za-z0-9_-]+)', value, re.I)
                if found: header_user = found.group(1)
        if requested and header_user and requested.casefold() != header_user.casefold():
            raise CatalogDenied(self.gateway._json_response(403, {'error': 'Emby user scope mismatch'}))
        self.user_id = requested or header_user
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.user_id):
            raise CatalogDenied(self.gateway._json_response(400, {'error': 'Emby UserId required'}))
        # Emby supports /Users/{Id}; /Users/Me is unavailable on existing installations.
        user = await self._read('/Users/' + self.user_id)
        if not isinstance(user, Mapping) or str(user.get('Id', '')).casefold() != self.user_id.casefold():
            raise CatalogDenied(self.gateway._json_response(403, {'error': 'Emby user scope mismatch'}))

    async def items(self, identifiers, query=None, details=False):
        identifiers = list(dict.fromkeys(str(value) for value in identifiers if value))
        accepted = {}
        # User-dependent filters (played/favorite/resume) belong to Emby, not the admin scan.
        safe_query = {'filters', 'isplayed', 'isfavorite', 'isresumable', 'minofficialrating',
                      'maxofficialrating', 'genres', 'genreids', 'tags', 'fields', 'enableuserdata',
                      'enableimages', 'imagetypelimit', 'enableimagetypes'}
        pairs = [(key, value) for key, values in (query or {}).items() if key.casefold() in safe_query
                 for value in values]
        if not details:
            pairs = [(key, value) for key, value in pairs if key.casefold() not in {'fields', 'enableuserdata', 'enableimages', 'imagetypelimit', 'enableimagetypes'}]
            pairs += [('Fields', ''), ('EnableUserData', 'false'), ('EnableImages', 'false')]
        for offset in range(0, len(identifiers), self.BATCH_SIZE):
            batch = identifiers[offset:offset + self.BATCH_SIZE]
            payload = await self._read('/Users/' + self.user_id + '/Items', pairs + [
                ('Ids', ','.join(batch)), ('Recursive', 'true'), ('StartIndex', '0'), ('Limit', str(len(batch)))])
            rows = payload.get('Items') if isinstance(payload, Mapping) else payload
            if not isinstance(rows, list):
                raise ValueError('Emby Items response is invalid')
            allowed_batch = set(batch)
            accepted.update({str(row['Id']): row if details else {'Id': str(row['Id'])} for row in rows
                             if isinstance(row, Mapping) and str(row.get('Id')) in allowed_batch})
        return accepted

    async def page(self, view, query, latest):
        await self.authorize()
        identifiers, _ = self.gateway._select_view_item_ids(view, query, latest, paginate=False)
        allowed = await self.items(identifiers, query)
        ordered = [key for key in identifiers if key in allowed]
        start = 0 if latest else self.gateway._int_query(query, 'StartIndex', 0, 0, 1_000_000)
        limit = self.gateway._int_query(query, 'Limit', 16 if latest else 50, 1, 200)
        selected = ordered[start:start + limit]
        fetched = await self.items(selected, query, details=True)
        page = [fetched[key] for key in selected if key in fetched]
        return page if latest else {'Items': page, 'TotalRecordCount': len(ordered), 'StartIndex': start}

    async def views(self, views):
        await self.authorize()
        allowed = await self.items(key for view in views for key in view.get('item_ids', []))
        return [dict(view, item_ids=[key for key in view.get('item_ids', []) if key in allowed])
                for view in views if any(key in allowed for key in view.get('item_ids', []))]
