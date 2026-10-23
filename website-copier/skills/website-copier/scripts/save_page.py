#!/usr/bin/env python3
"""Save one web page as a single offline HTML file (Website Copier skill for Claude).

Usage:  python3 save_page.py URL [OUTPUT.html]

Opens the page in headless Chromium (Playwright) when it is available, scrolls it so lazy parts load and takes
the page as the browser drew it; without a browser it downloads the HTML directly. Then it inlines stylesheets,
images and fonts as data: URIs, so the file opens without internet. Built to stay light and fast:
  - one image size per <img> (the best srcset candidate up to MAX_WIDTH px), the other sizes are dropped;
  - each URL is downloaded once; with Pillow, wide images are scaled down and photos re-compressed;
  - only woff2 (else woff) fonts; video, audio and iframes stay as links; scripts are removed (static snapshot);
  - files are fetched in parallel with a per-file timeout and an overall time budget.
No dependencies except the standard library; Playwright and Pillow are used when available. Prints a JSON
summary at the end; "reason": "network_blocked" means the sandbox proxy does not let us reach the site.
"""
import base64
import concurrent.futures as cf
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

MAX_WIDTH = 1600            # px: wider images are scaled down (Pillow) and srcset picks up to this width
JPEG_QUALITY = 80
MAX_IMAGE_BYTES = 2_500_000  # after compression; heavier images stay as links
MAX_ASSETS = 300
WORKERS = 8
FILE_TIMEOUT = 10           # s per file
RENDER_BUDGET = 45          # s for opening and scrolling the page in the browser
TIME_BUDGET = 75            # s for all assets after the page is taken; after it, the rest stay as links
MAX_HTML_BYTES = 15_000_000
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
BLOCKED_HOSTS = ('youtube.com', 'youtu.be', 'netflix.com', 'spotify.com', 'twitch.tv', 'disneyplus.com',
                 'hulu.com', 'primevideo.com', 'max.com', 'vimeo.com', 'tiktok.com')

try:
    from PIL import Image  # optional
except Exception:  # noqa: BLE001
    Image = None

START = time.time()
ASSETS_START = START  # reset when the page is taken, so a slow render does not eat the asset budget
stats = {'images': 0, 'stylesheets': 0, 'fonts': 0, 'skipped_heavy': 0, 'skipped_time': 0,
         'skipped_failed': 0, 'skipped_limit': 0, 'scripts_removed': 0, 'resized': 0, 'popups_removed': 0}
_cache = {}  # url -> data URI or None


def is_blocked(err):
    """The sandbox proxy refused the connection (claude.ai allows only package sites by default)."""
    t = str(err)
    return 'Tunnel connection failed: 403' in t or 'ERR_TUNNEL_CONNECTION_FAILED' in t or 'ProxyError' in t


def blocked_exit(host):
    print(json.dumps({'ok': False, 'reason': 'network_blocked', 'host': host}))
    sys.exit(1)


def proxy_settings():
    """The sandbox proxy from the environment, in Playwright's format (Chromium does not read these variables)."""
    p = (os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or os.environ.get('HTTP_PROXY')
         or os.environ.get('http_proxy'))
    if not p:
        return None
    u = urllib.parse.urlparse(p if '://' in p else 'http://' + p)
    s = {'server': '%s://%s:%s' % (u.scheme or 'http', u.hostname, u.port or 80)}
    if u.username:
        s['username'] = urllib.parse.unquote(u.username)
        s['password'] = urllib.parse.unquote(u.password or '')
    bypass = os.environ.get('NO_PROXY') or os.environ.get('no_proxy')
    if bypass:
        s['bypass'] = bypass
    return s


# runs in the page before it is taken: pop-up dialogs and full-screen overlays go, scrolling is allowed again
CLOSE_POPUPS_JS = r"""() => {
  const area = window.innerWidth * window.innerHeight;
  const total = (document.body && document.body.innerText || '').length;
  const kill = new Set(document.querySelectorAll(
    '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]'));
  for (const e of Array.from(document.body ? document.body.querySelectorAll('*') : [])) {
    const cs = getComputedStyle(e);
    if (cs.position !== 'fixed' || cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = e.getBoundingClientRect();
    if (r.width * r.height >= area * 0.5) kill.add(e);
  }
  let removed = 0;
  for (const e of kill) {
    if (e === document.body || e === document.documentElement || !e.isConnected) continue;
    if ((e.innerText || '').length > total * 0.5) continue;  // never the page itself
    e.remove();
    removed++;
  }
  for (const el of [document.documentElement, document.body]) {
    if (el && getComputedStyle(el).overflowY === 'hidden') el.style.setProperty('overflow', 'auto', 'important');
  }
  return removed;
}"""

# runs in the page before it is taken: CSSOM styles back into <style>, the image the browser shows, canvases as pictures
PREPARE_JS = r"""() => {
  for (const s of Array.from(document.styleSheets)) {
    try {
      const n = s.ownerNode;
      if (n && n.tagName === 'STYLE') {
        const t = Array.from(s.cssRules).map(r => r.cssText).join('\n');
        if (t.length > (n.textContent || '').length) n.textContent = t;
      }
    } catch (e) {}
  }
  for (const i of Array.from(document.images)) {
    if (i.currentSrc && !i.currentSrc.startsWith('blob:')) i.setAttribute('src', i.currentSrc);
    for (const a of ['srcset', 'data-src', 'data-srcset', 'data-lazy-src', 'loading']) i.removeAttribute(a);
  }
  for (const c of Array.from(document.querySelectorAll('canvas'))) {
    try {
      const url = c.toDataURL('image/png');
      if (url.length > 3000000 || c.clientWidth < 2) continue;
      const img = document.createElement('img');
      img.setAttribute('src', url);
      img.setAttribute('width', c.clientWidth);
      img.setAttribute('height', c.clientHeight);
      if (c.className) img.setAttribute('class', c.className);
      if (c.getAttribute('style')) img.setAttribute('style', c.getAttribute('style'));
      c.replaceWith(img);
    } catch (e) {}
  }
  return document.documentElement.outerHTML;
}"""


def render(url):
    """Open the page in headless Chromium, scroll it and return (html, final_url)."""
    from playwright.sync_api import sync_playwright
    deadline = time.time() + RENDER_BUDGET
    with sync_playwright() as pw:
        kw = {'headless': True, 'args': ['--disable-dev-shm-usage']}
        px = proxy_settings()
        if px:
            kw['proxy'] = px
        browser = pw.chromium.launch(**kw)
        try:
            ctx = browser.new_context(user_agent=UA, viewport={'width': 1440, 'height': 900}, locale='en-US',
                                      ignore_https_errors=True, service_workers='block')
            page = ctx.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            try:
                page.wait_for_load_state('load', timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            # scroll down screen by screen so lazy images and blocks load
            for _ in range(40):
                if time.time() > deadline:
                    break
                bottom = page.evaluate('() => { window.scrollBy(0, window.innerHeight); '
                                       'return window.scrollY + window.innerHeight >= '
                                       'document.documentElement.scrollHeight - 2; }')
                page.wait_for_timeout(250)
                if bottom:
                    break
            page.evaluate('() => window.scrollTo(0, 0)')
            left = int((deadline - time.time()) * 1000)
            try:
                page.wait_for_load_state('networkidle', timeout=max(1000, min(5000, left)))
            except Exception:  # noqa: BLE001
                pass
            try:
                page.keyboard.press('Escape')
                page.wait_for_timeout(300)
                stats['popups_removed'] = page.evaluate(CLOSE_POPUPS_JS)
            except Exception:  # noqa: BLE001
                pass
            return '<!DOCTYPE html>\n' + page.evaluate(PREPARE_JS), page.url
        finally:
            browser.close()


def fetch(url, accept='*/*', referer=None):
    headers = {'User-Agent': UA, 'Accept': accept, 'Accept-Language': 'en-US,en;q=0.9'}
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=FILE_TIMEOUT) as res:
        data = res.read(30_000_000)
        ctype = (res.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        charset = res.headers.get_content_charset()
        return data, ctype, charset, res.geturl()


def guess_type(url, ctype):
    if ctype and ctype not in ('application/octet-stream', 'binary/octet-stream', 'text/plain'):
        return ctype
    path = urllib.parse.urlparse(url).path.lower()
    for ext, t in (('.png', 'image/png'), ('.jpg', 'image/jpeg'), ('.jpeg', 'image/jpeg'), ('.gif', 'image/gif'),
                   ('.webp', 'image/webp'), ('.avif', 'image/avif'), ('.svg', 'image/svg+xml'),
                   ('.ico', 'image/x-icon'), ('.woff2', 'font/woff2'), ('.woff', 'font/woff'),
                   ('.ttf', 'font/ttf'), ('.otf', 'font/otf'), ('.css', 'text/css')):
        if path.endswith(ext):
            return t
    return ctype or 'application/octet-stream'


def shrink_image(data, ctype):
    """Scale down wide images and re-compress photos (only with Pillow)."""
    if Image is None or ctype in ('image/svg+xml', 'image/gif', 'image/x-icon', 'image/vnd.microsoft.icon'):
        return data, ctype
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001
        return data, ctype
    changed = False
    if im.width > MAX_WIDTH:
        im = im.resize((MAX_WIDTH, max(1, round(im.height * MAX_WIDTH / im.width))), Image.LANCZOS)
        changed = True
        stats['resized'] += 1
    has_alpha = im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info)
    if not changed and len(data) < 150_000:
        return data, ctype
    out = io.BytesIO()
    if has_alpha:
        im.save(out, 'WEBP', quality=JPEG_QUALITY)
        new_type = 'image/webp'
    else:
        im.convert('RGB').save(out, 'JPEG', quality=JPEG_QUALITY, optimize=True, progressive=True)
        new_type = 'image/jpeg'
    new = out.getvalue()
    return (new, new_type) if len(new) < len(data) else (data, ctype)


def to_data_uri(data, ctype):
    return 'data:%s;base64,%s' % (ctype, base64.b64encode(data).decode('ascii'))


def asset_uri(url, kind, referer):
    """Download an image or font once and return its data: URI (None = leave as a link)."""
    if url in _cache:
        return _cache[url]
    if len(_cache) >= MAX_ASSETS:
        stats['skipped_limit'] += 1
        return None
    if time.time() - ASSETS_START > TIME_BUDGET:
        stats['skipped_time'] += 1
        return None
    _cache[url] = None
    try:
        data, ctype, _, final = fetch(url, referer=referer)
    except Exception:  # noqa: BLE001
        stats['skipped_failed'] += 1
        return None
    ctype = guess_type(final, ctype)
    if kind == 'image':
        data, ctype = shrink_image(data, ctype)
        if len(data) > MAX_IMAGE_BYTES:
            stats['skipped_heavy'] += 1
            return None
        stats['images'] += 1
    else:
        stats['fonts'] += 1
    _cache[url] = to_data_uri(data, ctype)
    return _cache[url]


def prefetch(urls, kind, referer):
    """Download many assets in parallel (results land in the cache)."""
    todo = [u for u in dict.fromkeys(urls) if u and u not in _cache and u.startswith(('http://', 'https://'))]
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(lambda u: asset_uri(u, kind, referer), todo))


CSS_URL = re.compile(r'url\(\s*([\'"]?)([^\'")]+)\1\s*\)', re.I)
CSS_IMPORT = re.compile(r'@import\s+(?:url\(\s*)?[\'"]?([^\'")\s;]+)[\'"]?\s*\)?[^;]*;', re.I)
FONT_FACE = re.compile(r'@font-face\s*{[^}]*}', re.I)


def pick_font_src(block):
    """In an @font-face src list keep only one font file: woff2, else woff, else the first."""
    m = re.search(r'src\s*:\s*([^;}]+)', block, re.I)
    if not m:
        return block
    parts = [p.strip() for p in re.split(r',(?![^(]*\))', m.group(1))]
    urls = [p for p in parts if p.lower().startswith('url(')]
    if len(urls) < 2:
        return block
    best = next((p for p in urls if 'woff2' in p.lower()), None) or next(
        (p for p in urls if '.woff' in p.lower() or "'woff'" in p.lower() or '"woff"' in p.lower()), urls[0])
    keep = [p for p in parts if not p.lower().startswith('url(')] + [best]
    return block[:m.start(1)] + ', '.join(keep) + block[m.end(1):]


def process_css(css, base, depth=0):
    """Inline @import (up to 3 levels) and url() of a stylesheet; returns the CSS text."""
    css = FONT_FACE.sub(lambda m: pick_font_src(m.group(0)), css)

    def do_import(m):
        if depth >= 3:
            return ''
        url = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        try:
            data, _, charset, final = fetch(url, 'text/css,*/*;q=0.1', base)
            stats['stylesheets'] += 1
            return process_css(data.decode(charset or 'utf-8', 'replace'), final, depth + 1)
        except Exception:  # noqa: BLE001
            stats['skipped_failed'] += 1
            return ''
    css = CSS_IMPORT.sub(do_import, css)

    found = []
    for m in CSS_URL.finditer(css):
        u = m.group(2).strip()
        if not u.startswith(('data:', '#')):
            found.append(urllib.parse.urljoin(base, u))
    fonts = [u for u in found if re.search(r'\.(woff2?|ttf|otf|eot)(\?|#|$)', u, re.I)]
    prefetch(fonts, 'font', base)
    prefetch([u for u in found if u not in fonts], 'image', base)

    def repl(m):
        u = m.group(2).strip()
        if u.startswith(('data:', '#')):
            return m.group(0)
        uri = _cache.get(urllib.parse.urljoin(base, u))
        return 'url("%s")' % uri if uri else 'url("%s")' % urllib.parse.urljoin(base, u)
    return CSS_URL.sub(repl, css)


def attr(tag, name):
    m = re.search(r'\s%s\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))' % re.escape(name), tag, re.I)
    if not m:
        return None
    return html.unescape(m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4))


def drop_attr(tag, name):
    return re.sub(r'\s%s\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)' % re.escape(name), '', tag, flags=re.I)


def set_attr(tag, name, value):
    tag = drop_attr(tag, name)
    return re.sub(r'^(<[a-zA-Z0-9]+)', r'\1 %s="%s"' % (name, html.escape(value, quote=True)), tag)


def best_from_srcset(srcset, base):
    """The widest candidate up to MAX_WIDTH (w descriptors) or the 2x / largest density candidate."""
    cands = []
    for part in re.split(r',\s+(?=\S)', srcset.strip()):
        bits = part.strip().split()
        if not bits:
            continue
        url, desc = bits[0], (bits[1] if len(bits) > 1 else '1x')
        try:
            val = float(desc[:-1]) if desc[-1] in 'wx' else 1.0
        except ValueError:
            val = 1.0
        cands.append((desc[-1], val, urllib.parse.urljoin(base, url)))
    if not cands:
        return None
    w = [c for c in cands if c[0] == 'w']
    if w:
        fit = [c for c in w if c[1] <= MAX_WIDTH]
        return (max(fit, key=lambda c: c[1]) if fit else min(w, key=lambda c: c[1]))[2]
    x = [c for c in cands if c[1] <= 2] or cands
    return max(x, key=lambda c: c[1])[2]


def main():
    global ASSETS_START
    if len(sys.argv) < 2:
        print('usage: save_page.py URL [OUTPUT.html]')
        sys.exit(2)
    url = sys.argv[1].strip()
    if not re.match(r'https?://', url):
        url = 'https://' + url
    host = urllib.parse.urlparse(url).hostname or ''
    if any(host == h or host.endswith('.' + h) for h in BLOCKED_HOSTS):
        print(json.dumps({'ok': False, 'reason': 'video/streaming site is not supported', 'host': host}))
        sys.exit(1)

    mode, render_error, doc = 'browser', None, None
    try:
        doc, base = render(url)
    except Exception as e:  # noqa: BLE001  no Playwright / Chromium, or the browser failed: download directly
        if is_blocked(e):
            blocked_exit(host)
        mode, render_error = 'plain', str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
    if doc is None:
        try:
            data, ctype, charset, base = fetch(url, 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8')
        except Exception as e:  # noqa: BLE001
            if is_blocked(e):
                blocked_exit(host)
            print(json.dumps({'ok': False, 'reason': 'the site could not be reached: %s' % str(e)[:200]}))
            sys.exit(1)
        if 'html' not in ctype:
            print(json.dumps({'ok': False, 'reason': 'not an HTML page: %s' % ctype}))
            sys.exit(1)
        if not charset:
            m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', data[:4096], re.I)
            charset = m.group(1).decode() if m else 'utf-8'
        doc = data.decode(charset, 'replace')
    ASSETS_START = time.time()
    m = re.search(r'<base\s[^>]*href=["\']([^"\']+)', doc, re.I)
    if m:
        base = urllib.parse.urljoin(base, html.unescape(m.group(1)))

    # scripts, preload hints and existing CSP / base go: the copy is a static snapshot
    n = len(re.findall(r'<script\b', doc, re.I))
    doc = re.sub(r'<script\b[^>]*>.*?</script\s*>', '', doc, flags=re.I | re.S)
    doc = re.sub(r'<script\b[^>]*/>', '', doc, flags=re.I)
    stats['scripts_removed'] = n
    doc = re.sub(r'<link\b[^>]*rel=["\']?(?:modulepreload|preload|prefetch|preconnect|dns-prefetch|manifest)[^>]*>', '',
                 doc, flags=re.I)
    doc = re.sub(r'<meta\b[^>]*http-equiv=["\']?(?:content-security-policy|refresh)[^>]*>', '', doc, flags=re.I)
    doc = re.sub(r'<base\b[^>]*>', '', doc, flags=re.I)
    doc = re.sub(r'<source\b[^>]*>', '', doc, flags=re.I)  # <picture>/<video> variants: the <img> keeps one size

    # <img>: lazy attributes → real ones, one size from srcset
    img_urls = []

    def fix_img(m):
        tag = m.group(0)
        src = attr(tag, 'data-src') or attr(tag, 'data-lazy-src') or attr(tag, 'src')
        srcset = attr(tag, 'data-srcset') or attr(tag, 'srcset')
        chosen = best_from_srcset(srcset, base) if srcset else None
        if not chosen and src and not src.startswith('data:'):
            chosen = urllib.parse.urljoin(base, src)
        for a in ('srcset', 'data-srcset', 'sizes', 'data-src', 'data-lazy-src'):
            tag = drop_attr(tag, a)
        if chosen:
            tag = set_attr(tag, 'src', chosen)
            img_urls.append(chosen)
        return tag
    doc = re.sub(r'<img\b[^>]*>', fix_img, doc, flags=re.I)

    # icons
    icon_urls = []

    def fix_icon(m):
        href = attr(m.group(0), 'href')
        if href and not href.startswith('data:'):
            u = urllib.parse.urljoin(base, href)
            icon_urls.append(u)
            return set_attr(m.group(0), 'href', u)
        return m.group(0)
    doc = re.sub(r'<link\b[^>]*rel=["\']?(?:shortcut )?icon[^>]*>', fix_icon, doc, flags=re.I)

    prefetch(img_urls + icon_urls, 'image', base)

    def embed_img(m):
        tag = m.group(0)
        src = attr(tag, 'src')
        uri = _cache.get(src) if src else None
        return set_attr(tag, 'src', uri) if uri else tag
    doc = re.sub(r'<img\b[^>]*>', embed_img, doc, flags=re.I)
    doc = re.sub(r'<link\b[^>]*rel=["\']?(?:shortcut )?icon[^>]*>',
                 lambda m: set_attr(m.group(0), 'href', _cache[attr(m.group(0), 'href')])
                 if _cache.get(attr(m.group(0), 'href')) else m.group(0), doc, flags=re.I)

    # stylesheets → <style>
    def inline_link(m):
        tag = m.group(0)
        if not re.search(r'rel=["\']?stylesheet', tag, re.I):
            return tag
        href = attr(tag, 'href')
        if not href:
            return ''
        u = urllib.parse.urljoin(base, href)
        try:
            data, _, cs, final = fetch(u, 'text/css,*/*;q=0.1', base)
        except Exception:  # noqa: BLE001
            stats['skipped_failed'] += 1
            return tag
        stats['stylesheets'] += 1
        media = attr(tag, 'media')
        css = process_css(data.decode(cs or 'utf-8', 'replace'), final)
        return '<style%s>%s</style>' % (' media="%s"' % html.escape(media) if media else '', css)
    doc = re.sub(r'<link\b[^>]*>', inline_link, doc, flags=re.I)
    doc = re.sub(r'(<style\b[^>]*>)(.*?)(</style\s*>)',
                 lambda m: m.group(1) + process_css(m.group(2), base) + m.group(3), doc, flags=re.I | re.S)
    doc = re.sub(r'\sstyle\s*=\s*"([^"]*url\([^"]*)"',
                 lambda m: ' style="%s"' % html.escape(process_css(html.unescape(m.group(1)), base), quote=True),
                 doc, flags=re.I)

    # links keep working online: make them absolute
    doc = re.sub(r'(<a\b[^>]*\shref=)(["\'])(?!https?:|#|mailto:|tel:|javascript:|data:)([^"\']*)\2',
                 lambda m: '%s%s%s%s' % (m.group(1), m.group(2), urllib.parse.urljoin(base, html.unescape(m.group(3))),
                                         m.group(2)), doc, flags=re.I)

    # nothing in the file may reach the internet when opened; a note says where the copy came from
    csp = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; "
           "style-src 'unsafe-inline' data:; font-src data:; media-src data:\">")
    note = '<!-- Saved from %s on %s UTC by the Website Copier skill (websitecopier.app). Static snapshot. -->' % (
        html.escape(url), time.strftime('%Y-%m-%d %H:%M', time.gmtime()))
    if re.search(r'<head\b[^>]*>', doc, re.I):
        doc = re.sub(r'(<head\b[^>]*>)', lambda m: m.group(1) + csp, doc, count=1, flags=re.I)
    else:
        doc = csp + doc
    doc = note + '\n' + doc

    out = sys.argv[2] if len(sys.argv) > 2 else None
    if not out:
        t = re.search(r'<title[^>]*>(.*?)</title>', doc, re.I | re.S)
        name = re.sub(r'[^\w\- ]+', '', html.unescape(t.group(1)) if t else host).strip()[:60] or host
        out = re.sub(r'\s+', '-', name) + '.html'
    body = re.sub(r'<(style|noscript)\b.*?</\1\s*>', ' ', doc, flags=re.I | re.S)
    text_chars = len(re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', body))).strip())
    raw = doc.encode('utf-8')
    with open(out, 'wb') as f:
        f.write(raw)
    print(json.dumps({'ok': True, 'file': out, 'size_mb': round(len(raw) / 1e6, 1),
                      'too_big': len(raw) > MAX_HTML_BYTES, 'text_chars': text_chars,
                      'looks_empty': text_chars < 1500, 'seconds': round(time.time() - START, 1),
                      'mode': mode, 'render_error': render_error, 'pillow': Image is not None, **stats}))


if __name__ == '__main__':
    main()
