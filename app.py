from flask import Flask, render_template, jsonify, request
from bs4 import BeautifulSoup
import requests
import urllib.parse
import concurrent.futures
import threading
import time
import re
import math
import random

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/html, */*',
}

TRACKERS = [
    'udp://open.demonii.com:1337/announce',
    'udp://tracker.openbittorrent.com:80',
    'udp://tracker.coppersurfer.tk:6969',
    'udp://glotorrents.pw:6969/announce',
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://torrent.gresille.org:80/announce',
    'udp://p4p.arenabg.com:1337',
    'udp://tracker.leechers-paradise.org:6969',
]

TV_CATS    = {'205', '208', '212'}
MOVIE_CATS = {'201', '202', '207', '210', '211'}
OMDB_KEY   = 'trilogy'

SAFE_GROUPS = frozenset([
    'yify', 'yts', 'rarbg', 'tigole', 'galadriel', 'sparks',
    'mkvcage', 'bludv', 'bluebird', 'framestor', 'cinephiles',
])

PROXY_SOURCES = [
    'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
    'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt',
]

# Shared HTTP session for connection pooling
_session = requests.Session()
_session.headers.update(HEADERS)

# Proxy state
_proxy_lock = threading.Lock()
_proxy = {'value': None, 'found_at': 0.0, 'searching': False}
PROXY_TTL = 600

# Result cache: {query -> {'results': [...], 'ts': float}}
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 300   # 5 minutes
CACHE_MAX   = 50


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_magnet(info_hash, title):
    tr = '&tr='.join(urllib.parse.quote(t, safe='') for t in TRACKERS)
    return f'magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title)}&tr={tr}'


def get_quality(text):
    t = text.lower()
    if any(x in t for x in ['4k', '2160p', 'uhd']):  return '4K'
    if any(x in t for x in ['1080p', '1080i']):       return '1080p'
    if any(x in t for x in ['720p', '720i']):         return '720p'
    if any(x in t for x in ['480p', '480i']):         return '480p'
    return 'HD'


def format_size(size_bytes):
    try:
        n = int(size_bytes)
    except Exception:
        return ''
    if n >= 1 << 30: return f'{n / (1 << 30):.2f} GB'
    if n >= 1 << 20: return f'{n / (1 << 20):.1f} MB'
    if n >= 1 << 10: return f'{n >> 10} KB'
    return f'{n} B'


def clean_title(name):
    name = re.sub(r'\(\d{4}\)', '', name)
    name = re.sub(r'\b(19|20)\d{2}\b', '', name)
    name = re.sub(
        r'\b(4k|2160p|1080p|720p|480p|bluray|blu-ray|bdrip|webrip|web-dl'
        r'|web|hdtv|dvdrip|x264|x265|hevc|h264|h265|aac|dd5|ac3'
        r'|complete|extended|remastered|proper|repack)\b.*',
        '', name, flags=re.I,
    )
    name = re.sub(r'\bS\d{2}.*$', '', name, flags=re.I)
    name = re.sub(r'\bSeason\s+\d+.*$', '', name, flags=re.I)
    return name.strip(' .-_[]()').strip()


# ── Scoring ("Best Pick") ─────────────────────────────────────────────────────

def score_result(r):
    t       = r['torrents'][0]
    trust_m = {'vip': 1.4, 'trusted': 1.2, 'member': 1.0}.get(
                  r.get('uploader_status', 'member'), 1.0)
    group   = 20 if any(g in r['title'].lower() for g in SAFE_GROUPS) else 0
    return round(math.log1p(t['seeds']) * 20 * trust_m + group, 2)


def best_reason(r):
    t      = r['torrents'][0]
    status = r.get('uploader_status', 'member')
    parts  = [f"{t['seeds']} seeds", t['quality']]
    if status in ('vip', 'trusted'):
        parts.append('Trusted uploader')
    if any(g in r['title'].lower() for g in SAFE_GROUPS):
        parts.append('Trusted release group')
    return ' · '.join(parts)


def mark_best_pick(results):
    if not results:
        return results
    for r in results:
        r['best_pick']   = False
        r['score']       = score_result(r)
        r['best_reason'] = ''
    best = max(results, key=lambda x: x['score'])
    best['best_pick']   = True
    best['best_reason'] = best_reason(best)
    return results


# ── Proxy management ──────────────────────────────────────────────────────────

def _fetch_proxy_list():
    found = set()
    for url in PROXY_SOURCES:
        try:
            r = requests.get(url, timeout=8)
            for line in r.text.splitlines():
                line = line.strip()
                if re.match(r'\d+\.\d+\.\d+\.\d+:\d+$', line):
                    found.add(line)
            if len(found) > 300:
                break
        except Exception:
            pass
    items = list(found)
    random.shuffle(items)
    return items


def _test_proxy(proxy, test_url='http://httpbin.org/ip', timeout=4):
    p = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
    try:
        r = requests.get(test_url, proxies=p, timeout=timeout, allow_redirects=True)
        return r.status_code < 500
    except Exception:
        return False


def _find_and_cache_proxy():
    with _proxy_lock:
        if _proxy['searching']:
            return
        _proxy['searching'] = True

    app.logger.info('Proxy: fetching proxy list…')
    try:
        proxy_list = _fetch_proxy_list()
        app.logger.info(f'Proxy: testing {min(len(proxy_list), 240)} candidates…')
        for i in range(0, min(len(proxy_list), 300), 20):
            batch = proxy_list[i:i + 20]
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
                fmap = {ex.submit(_test_proxy, p): p for p in batch}
                try:
                    for f in concurrent.futures.as_completed(fmap, timeout=10):
                        try:
                            if f.result():
                                found = fmap[f]
                                with _proxy_lock:
                                    _proxy['value']    = found
                                    _proxy['found_at'] = time.time()
                                app.logger.info('Proxy: working proxy cached')
                                return
                        except Exception:
                            pass
                except (concurrent.futures.TimeoutError, TimeoutError):
                    pass  # some stragglers — continue to next batch
        app.logger.warning('Proxy: no working proxy found in this pass.')
        with _proxy_lock:
            _proxy['value'] = None
    finally:
        with _proxy_lock:
            _proxy['searching'] = False


def get_proxy():
    with _proxy_lock:
        p        = _proxy['value']
        found_at = _proxy['found_at']
        searching = _proxy['searching']

    now = time.time()
    if p and (now - found_at) < PROXY_TTL:
        return p
    if not searching:
        threading.Thread(target=_find_and_cache_proxy, daemon=True).start()
    return None


def proxied(url, params=None, timeout=12):
    """Try direct first (works on US/EU servers); fall back to proxy for India ISP blocks."""
    kwargs = {'params': params, 'headers': HEADERS}
    try:
        return _session.get(url, timeout=3, **kwargs)
    except Exception:
        pass
    p = get_proxy()
    if p:
        kwargs['proxies'] = {'http': f'http://{p}', 'https': f'http://{p}'}
        return requests.get(url, timeout=timeout, **kwargs)
    raise Exception(f'Unreachable: {url}')


# ── Sources ───────────────────────────────────────────────────────────────────

def _tpb_fetch(query):
    """Fetch from apibay.org; fall back to proxy when data-center IP is blocked."""
    url    = 'https://apibay.org/q.php'
    params = {'q': query, 'cat': '0'}
    try:
        r = _session.get(url, params=params, timeout=5)
        if r.text.strip():          # non-empty → data center not blocked
            return r.json()
    except Exception:
        pass
    # Empty body = data-center IP blocked by apibay — try via proxy
    p = get_proxy()
    if not p:
        raise Exception('apibay blocked and no proxy available yet')
    r = requests.get(url, params=params, headers=HEADERS,
                     proxies={'http': f'http://{p}', 'https': f'http://{p}'},
                     timeout=12)
    return r.json()


def search_tpb(query):
    try:
        data = _tpb_fetch(query)
        if not data or data[0].get('name') == 'No results returned':
            return []

        results = []
        for item in data[:25]:
            name = item.get('name', '')
            ih   = item.get('info_hash', '')
            if not name or not ih:
                continue

            cat_id = str(item.get('category', '0'))
            if cat_id in TV_CATS:      ctype = 'tv'
            elif cat_id in MOVIE_CATS: ctype = 'movie'
            else:
                ctype = 'tv' if re.search(
                    r's\d{2}e\d{2}|season\s*\d|episode\s*\d', name.lower()) else 'movie'

            results.append({
                'title':           name,
                'year':            '',
                'rating':          0,
                'genres':          [],
                'poster':          '',
                'type':            ctype,
                'source':          'TPB',
                'uploader_status': item.get('status', 'member'),
                'uploader':        item.get('username', ''),
                '_base':           clean_title(name),
                '_ctype':          ctype,
                'torrents': [{
                    'quality': get_quality(name),
                    'type':    '',
                    'size':    format_size(item.get('size', 0)),
                    'seeds':   int(item.get('seeders', 0)),
                    'peers':   int(item.get('leechers', 0)),
                    'magnet':  build_magnet(ih, name),
                }],
            })

        results.sort(key=lambda x: x['torrents'][0]['seeds'], reverse=True)
        return results
    except Exception as e:
        app.logger.warning(f'TPB: {e}')
        return []


def search_yts(query):
    try:
        r = proxied(
            'https://yts.mx/api/v2/list_movies.json',
            params={'query_term': query, 'limit': 10, 'sort_by': 'seeds'},
        )
        data = r.json()
        results = []
        if data.get('status') == 'ok' and data['data'].get('movies'):
            for movie in data['data']['movies']:
                torrents = [{
                    'quality': t['quality'],
                    'type':    t.get('type', ''),
                    'size':    t.get('size', ''),
                    'seeds':   t.get('seeds', 0),
                    'peers':   t.get('peers', 0),
                    'magnet':  build_magnet(t['hash'], movie.get('title_long', movie['title'])),
                } for t in movie.get('torrents', [])]
                if torrents:
                    title = movie.get('title_long', movie['title'])
                    results.append({
                        'title':           title,
                        'year':            str(movie.get('year', '')),
                        'rating':          movie.get('rating', 0),
                        'genres':          (movie.get('genres') or [])[:3],
                        'poster':          movie.get('medium_cover_image', ''),
                        'type':            'movie',
                        'source':          'YTS',
                        'uploader_status': 'trusted',
                        'uploader':        'YTS',
                        '_base':           movie.get('title', title),
                        '_ctype':          'movie',
                        'torrents':        sorted(torrents, key=lambda x: x['seeds'], reverse=True),
                    })
        return results
    except Exception as e:
        app.logger.warning(f'YTS: {e}')
        return []


def _fetch_1337x_magnet(url):
    try:
        r    = proxied(url, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        link = soup.find('a', href=re.compile(r'^magnet:'))
        return link['href'] if link else None
    except Exception:
        return None


def search_1337x(query):
    try:
        encoded = urllib.parse.quote(query)
        r    = proxied(f'https://1337x.to/search/{encoded}/1/')
        soup = BeautifulSoup(r.text, 'html.parser')
        rows = soup.select('table.table-list tbody tr')

        items = []
        for row in rows[:4]:
            try:
                links = row.select('td.name a')
                if len(links) < 2:
                    continue
                name       = links[1].text.strip()
                durl       = 'https://1337x.to' + links[1]['href']
                size_el    = row.select_one('td.size')
                size       = ' '.join((size_el.get_text(strip=True) if size_el else '').split())
                seeds_el   = row.select_one('td.seeds')
                seeds      = int(re.sub(r'\D', '', seeds_el.text) or '0') if seeds_el else 0
                leeches_el = row.select_one('td.leeches')
                leeches    = int(re.sub(r'\D', '', leeches_el.text) or '0') if leeches_el else 0
                items.append({'name': name, 'url': durl, 'size': size,
                              'seeds': seeds, 'leeches': leeches})
            except Exception:
                continue

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            fmap = {ex.submit(_fetch_1337x_magnet, it['url']): it for it in items}
            done, _ = concurrent.futures.wait(fmap, timeout=15)
            for f in done:
                it = fmap[f]
                try:
                    magnet = f.result()
                except Exception:
                    magnet = None
                if magnet:
                    is_tv = bool(re.search(r's\d{2}e\d{2}|season|episode', it['name'].lower()))
                    results.append({
                        'title':           it['name'],
                        'year':            '',
                        'rating':          0,
                        'genres':          [],
                        'poster':          '',
                        'type':            'tv' if is_tv else 'movie',
                        'source':          '1337x',
                        'uploader_status': 'member',
                        'uploader':        '',
                        '_base':           clean_title(it['name']),
                        '_ctype':          'tv' if is_tv else 'movie',
                        'torrents': [{
                            'quality': get_quality(it['name']),
                            'type':    '',
                            'size':    it['size'],
                            'seeds':   it['seeds'],
                            'peers':   it['leeches'],
                            'magnet':  magnet,
                        }],
                    })
        return results
    except Exception as e:
        app.logger.warning(f'1337x: {e}')
        return []


def search_knaben(query):
    """Knaben meta-search — public API, works from data center IPs."""
    try:
        r = _session.get(
            'https://knaben.eu/api/v1/',
            params={
                'search': query,
                'order_by': 'seeders',
                'order_direction': 'desc',
                'rows': 20,
            },
            timeout=10,
        )
        data  = r.json()
        hits  = data.get('hits', [])
        results = []
        for item in hits:
            ih     = item.get('infohash') or item.get('info_hash', '')
            name   = item.get('title') or item.get('name', '')
            seeds  = int(item.get('seeders') or 0)
            leeches = int(item.get('leechers') or 0)
            size   = item.get('size') or 0
            if not ih or not name:
                continue
            is_tv = bool(re.search(r's\d{2}e\d{2}|season|episode', name.lower()))
            results.append({
                'title':           name,
                'year':            '',
                'rating':          0,
                'genres':          [],
                'poster':          '',
                'type':            'tv' if is_tv else 'movie',
                'source':          'Knaben',
                'uploader_status': 'member',
                'uploader':        '',
                '_base':           clean_title(name),
                '_ctype':          'tv' if is_tv else 'movie',
                'torrents': [{
                    'quality': get_quality(name),
                    'type':    '',
                    'size':    format_size(size),
                    'seeds':   seeds,
                    'peers':   leeches,
                    'magnet':  build_magnet(ih, name),
                }],
            })
        results.sort(key=lambda x: x['torrents'][0]['seeds'], reverse=True)
        return results
    except Exception as e:
        app.logger.warning(f'Knaben: {e}')
        return []


# ── Poster enrichment ─────────────────────────────────────────────────────────

def _fetch_poster(title, ctype):
    try:
        media = 'series' if ctype == 'tv' else 'movie'
        r     = _session.get(
            'https://www.omdbapi.com/',
            params={'t': title, 'type': media, 'apikey': OMDB_KEY},
            timeout=4,
        )
        poster = r.json().get('Poster', '')
        return poster if poster and poster != 'N/A' else ''
    except Exception:
        return ''


def enrich_posters(results):
    seen: dict = {}
    for r in results:
        if r.get('poster'):
            continue
        key = r['_base'].lower()
        if key not in seen:
            seen[key] = {'title': r['_base'], 'ctype': r['_ctype'], 'items': []}
        seen[key]['items'].append(r)

    def _f(key, info):
        return key, _fetch_poster(info['title'], info['ctype'])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        fmap = {ex.submit(_f, k, v): k for k, v in seen.items()}
        for f in concurrent.futures.as_completed(fmap, timeout=8):
            try:
                key, poster = f.result()
                if poster:
                    for item in seen[key]['items']:
                        item['poster'] = poster
            except Exception:
                pass

    for r in results:
        r.pop('_base', None)
        r.pop('_ctype', None)
    return results


# ── Result cache ──────────────────────────────────────────────────────────────

def _cache_get(query):
    with _cache_lock:
        entry = _cache.get(query)
    if entry and (time.time() - entry['ts']) < CACHE_TTL:
        return entry['results']
    return None


def _cache_set(query, results):
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k]['ts'])
            del _cache[oldest]
        _cache[query] = {'results': results, 'ts': time.time()}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/api/debug')
def debug():
    """Connectivity probe — visit /api/debug to see which sources are reachable."""
    probes = {
        'apibay':        ('https://apibay.org/q.php',                       {'q': 'avatar', 'cat': '0'}),
        'yts':           ('https://yts.mx/api/v2/list_movies.json',         {'query_term': 'avatar', 'limit': 1}),
        'knaben':        ('https://knaben.eu/api/v1/',                       {'search': 'avatar', 'rows': 1}),
        'solidtorrents': ('https://solidtorrents.to/api/v1/search',         {'q': 'avatar'}),
        'btdig':         ('https://btdig.com/search',                        {'q': 'avatar', 'order': 1}),
        'torrentgalaxy': ('https://torrentgalaxy.to/torrents.php',          {'search': 'avatar'}),
    }
    out = {}
    for name, (url, params) in probes.items():
        try:
            r = _session.get(url, params=params, timeout=6)
            out[name] = {'status': r.status_code, 'bytes': len(r.content),
                         'preview': r.text[:300]}
        except Exception as e:
            out[name] = {'error': str(e)}
    return jsonify(out)

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/search')
def search():
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify({'error': 'Query too short'}), 400

    cached = _cache_get(query)
    if cached is not None:
        return jsonify({'results': cached, 'count': len(cached),
                        'proxy_active': bool(get_proxy()), 'cached': True})

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        tpb_f = ex.submit(search_tpb,    query)
        yts_f = ex.submit(search_yts,    query)
        x13_f = ex.submit(search_1337x,  query)
        knb_f = ex.submit(search_knaben, query)
        results = [*tpb_f.result(), *yts_f.result(), *x13_f.result(), *knb_f.result()]

    results = enrich_posters(results)
    results = mark_best_pick(results)
    results.sort(key=lambda x: (x.get('best_pick', False), x.get('score', 0)), reverse=True)

    _cache_set(query, results)

    return jsonify({
        'results':      results,
        'count':        len(results),
        'proxy_active': bool(get_proxy()),
        'cached':       False,
    })


@app.route('/api/proxy-status')
def proxy_status():
    with _proxy_lock:
        p         = _proxy['value']
        found_at  = _proxy['found_at']
        searching = _proxy['searching']
    return jsonify({
        'active':    bool(p and (time.time() - found_at) < PROXY_TTL),
        'searching': searching,
        'proxy':     p or '',
    })


# Start proxy search at module load so it's ready under Passenger/Gunicorn
threading.Thread(target=_find_and_cache_proxy, daemon=True).start()


if __name__ == '__main__':
    import webbrowser

    def _open():
        time.sleep(1.2)
        webbrowser.open('http://127.0.0.1:5000')
    threading.Thread(target=_open, daemon=True).start()
    app.run(host='127.0.0.1', port=5000, debug=False)
