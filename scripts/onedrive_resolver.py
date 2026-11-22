"""Public OneDrive links, resolved in an isolated browser (no account/password).

Reads the JSON responses that the page itself receives, like the supplied
userscript. Does not turn an HTML sharing page into a pretend download URL.
Lists visible files even before a downloadUrl exists. Resolves a selected file
through its own Download menu when necessary. Diagnostics contain only counts,
HTTP statuses and error categories, never tokens, names, cookies or full URLs.
"""
from __future__ import annotations

import base64
from collections import Counter, deque
from dataclasses import dataclass, field
from html import unescape
import json
import os
import posixpath
import re
import shutil
import time
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit


class OneDriveError(RuntimeError):
    """A user-facing error that never contains session tokens or signed URLs."""


def clean_url(value: str) -> str:
    value = unescape(str(value or '').strip().strip('<>'))
    value = re.sub(r'\\([:/.?&=_%#!()\-])', r'\1', value).rstrip('\\')
    if not value.startswith(('https://', 'http://')):
        value = 'https://' + value
    return value


def _host(url: str) -> str:
    return (urlsplit(url).hostname or '').lower()


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith('.' + domain)


def is_onedrive_url(url: str) -> bool:
    h = _host(clean_url(url))
    return h in ('1drv.ms', 'onedrive.live.com') or any(
        _under(h, d) for d in ('sharepoint.com', 'microsoftpersonalcontent.com', '1drv.com', 'storage.live.com')
    )


def _microsoft_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        port = p.port
        h = (p.hostname or '').lower()
    except ValueError:
        return False
    return p.scheme == 'https' and not p.username and not p.password and port in (None, 443) and (
        h == 'graph.microsoft.com' or any(_under(h, d) for d in (
            '1drv.ms', '1drv.com', 'live.com', 'onedrive.com',
            'sharepoint.com', 'sharepointonline.com', 'microsoftpersonalcontent.com',
        ))
    )


def is_direct_url(url: str) -> bool:
    if not _microsoft_url(url):
        return False
    p = urlsplit(url)
    h, path = _host(url), p.path.lower()
    # CDN links and preauthenticated download.aspx links already are downloads.
    return (_under(h, '1drv.com') or _under(h, 'storage.live.com') or
            path.endswith('/download.aspx') or
            (h == 'onedrive.live.com' and path in ('/download', '/download/')))


def sharing_url(url: str) -> str:
    """Decode redeem, without losing the selected subfolder in the original URL."""
    url = clean_url(url)
    token = dict(parse_qsl(urlsplit(url).query)).get('redeem', '')
    if token:
        try:
            raw = token[2:] if token.startswith('u!') else token
            decoded = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8')
            if _microsoft_url(decoded) and is_onedrive_url(decoded):
                return decoded
        except (ValueError, UnicodeError):
            pass
    return url


def _uid(url: str, item_id: Any = None) -> str:
    q = {k.lower(): v for k, v in parse_qsl(urlsplit(url).query)}
    # UniqueId is stable while authkey/tempauth/downloadUrl may change.
    return (q.get('uniqueid') or str(item_id or '') or url).lower()


def _item_number(value: str) -> str | None:
    value = str(value).lower().strip('{}')
    m = re.search(r'!(\d+)$', value)
    if m:
        return str(int(m[1]))
    if value.isdigit():
        return str(int(value))
    # Compatibility heuristic from the user's script; only a name fallback.
    m = re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-([0-9a-f]{8})0000', value)
    if m:
        return str(int.from_bytes(bytes.fromhex(m[1]), 'little'))
    return None


def _size(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


MAX_CAPTURE_BYTES = 16 * 1024 * 1024


def json_values(text: str):
    """Decode bounded data; never evaluate scripts or arbitrary JavaScript."""
    if not isinstance(text, str) or len(text) > MAX_CAPTURE_BYTES:
        return
    decoder = json.JSONDecoder()
    value = text.lstrip('\ufeff \r\n\t')
    # Standard JSON first; multipart bodies begin after a blank line.
    candidates = [0] + [m.start(1) for m in re.finditer(r'(?m)^[ \t]*([\[{])', value)]
    end = -1
    for start in sorted(set(candidates))[:256]:
        if start < end:
            continue
        try:
            obj, used = decoder.raw_decode(value[start:])
        except ValueError:
            continue
        if isinstance(obj, (dict, list)):
            end = start + used
            yield obj


@dataclass
class Capture:
    """Collects only downloadUrl fields, never arbitrary URLs from a page."""
    files: dict[str, dict] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    folders: dict[str, dict] = field(default_factory=dict)
    file_names: set[str] = field(default_factory=set)
    next_links: set[str] = field(default_factory=set)
    metadata: dict[str, dict] = field(default_factory=dict)

    def name(self, item_id: Any, name: Any) -> None:
        if not isinstance(name, str) or not name.strip():
            return
        n = _item_number(str(item_id or ''))
        if n is not None:
            self.names[n] = name

    def add(self, name: Any, url: str, size: Any = 0, item_id: Any = None) -> None:
        if not isinstance(url, str) or not _microsoft_url(url):
            return
        uid = _uid(url, item_id)
        old = self.files.get(uid, {})
        self.files[uid] = {
            'url': url, 'name': name if isinstance(name, str) and name else old.get('name'),
            'size': _size(size) or old.get('size', 0), 'od_uid': uid,
        }

    def scan(self, obj: Any, depth: int = 0, listing: bool = False) -> None:
        if depth > 18:
            return
        if isinstance(obj, list):
            for item in obj:
                self.scan(item, depth + 1, listing)
            return
        if not isinstance(obj, dict):
            return
        name = obj.get('name') or obj.get('Name') or obj.get('FileLeafRef')
        iid = obj.get('id') or obj.get('Id') or obj.get('ID') or obj.get('resourceId') or obj.get('UniqueId')
        self.name(iid, name)
        folder = (isinstance(obj.get('folder'), dict) or obj.get('isFolder') is True or
                  str(obj.get('FSObjType', obj.get('FileSystemObjectType', ''))) == '1')
        if folder and listing and isinstance(name, str) and name:
            self.folders[name] = {**self.folders.get(name, {}), **obj, 'name': name}
        if not folder:
            dl = next((obj[k] for k in ('@content.downloadUrl', '@microsoft.graph.downloadUrl',
                                       'downloadUrl', 'DownloadUrl') if isinstance(obj.get(k), str)), None)
            if dl:
                self.add(name, dl, obj.get('size', obj.get('Size', obj.get('File_x0020_Size'))), iid)
            if isinstance(name, str) and ('file' in obj or dl or str(obj.get('FSObjType', '')) == '0'):
                self.file_names.add(name)
                self.metadata[name] = {'name': name, 'id': str(iid or ''),
                    'size': _size(obj.get('size', obj.get('Size', obj.get('File_x0020_Size'))))}
        for k, v in obj.items():
            if k.lower().endswith('odata.nextlink') and isinstance(v, str):
                self.next_links.add(v)
            elif k == 'body' and isinstance(v, str) and v.lstrip().startswith(('{', '[')):
                try:
                    self.scan(json.loads(v), depth + 1, listing)
                except ValueError:
                    pass
            elif isinstance(v, (dict, list)):
                self.scan(v, depth + 1, listing or k in ('children', 'value', 'Row', 'items'))

    def dom(self, rows: list[dict]) -> None:
        for r in rows:
            self.name(r.get('id'), r.get('name'))
            if r.get('folder') and r.get('name'):
                self.folders.setdefault(r['name'], {'name': r['name']})
            elif r.get('name'):
                self.file_names.add(r['name'])
                old = self.metadata.get(r['name'], {})
                self.metadata[r['name']] = {**old, 'name': r['name'],
                    'id': str(r.get('id') or old.get('id') or ''),
                    'size': old.get('size', 0)}

    def text(self, text: str) -> bool:
        """JSON, anti-XSSI JSON and JSON bodies in multipart/$batch responses."""
        parsed = False
        for value in json_values(text):
            self.scan(value)
            parsed = True
        return parsed

    def listing(self, source: str) -> list[dict]:
        """Unresolved entries are explicit descriptors, NEVER direct downloads."""
        result = self.entries()
        resolved = {e['name'] for e in result}
        for name in sorted(self.file_names - resolved):
            meta = self.metadata.get(name, {})
            iid = str(meta.get('id') or '')
            result.append({'url': source, 'name': name, 'size': meta.get('size', 0),
                'od_uid': 'pending:' + (iid or name), 'od_item_id': iid,
                'od_pending': True, 'od_page': source, 'od_resolved_at': 0,
                'presign': 'onedrive'})
        return result

    def entries(self) -> list[dict]:
        out = []
        for uid, data in self.files.items():
            name = data.get('name') or self.names.get(_item_number(uid) or '')
            if not name:
                name = 'arquivo_' + re.sub(r'[^a-zA-Z0-9_-]', '', uid)[-20:]
            out.append({**data, 'name': name})
        return out


def folder_url(current: str, folder: dict) -> str | None:
    """Navigate descendants with the original sharing context; no invented API."""
    p = urlsplit(current)
    pairs = parse_qsl(p.query, keep_blank_values=True)
    query = dict(pairs)
    parent = query.get('id', '')
    name = folder.get('name') or folder.get('Name') or folder.get('FileLeafRef') or ''
    if parent.startswith('/') and name and name not in ('.', '..') and not any(c in name for c in '/\\\x00'):
        target = folder.get('FileRef') or folder.get('ServerRelativeUrl') or parent.rstrip('/') + '/' + name
        if not isinstance(target, str) or not target.startswith(parent.rstrip('/') + '/'):
            return None
        if posixpath.normpath(target) != target:
            return None
        pairs = [(k, v) for k, v in pairs if k.lower() not in ('id', 'resid')]
        pairs.append(('id', target))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(pairs), ''))
    iid = str(folder.get('id') or folder.get('resourceId') or '')
    if '!' in iid:
        pairs = [(k, v) for k, v in pairs if k.lower() not in ('id', 'resid')]
        pairs.append(('id', iid))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(pairs), ''))
    web = folder.get('webUrl')
    if isinstance(web, str) and _microsoft_url(web) and _host(web) == _host(current) and web != current:
        return web
    return None


CAPTURE_JS = r'''(() => {
  if (window.__uploaderODCaptureV2) return;
  const s=window.__uploaderODCaptureV2={items:[],bytes:0,dropped:0};
  const LIMIT=16*1024*1024;
  function save(url,text) {
    if (typeof text!=='string' || !/[\[{]/.test(text.slice(0,8192))) return;
    if(text.length>LIMIT) {s.dropped++;return;}
    s.items.push({url,text,bytes:text.length});s.bytes+=text.length;
    while(s.items.length>64 || s.bytes>LIMIT) {s.bytes-=s.items.shift().bytes;s.dropped++;}
  }
  async function bounded(response) {
    const ct=(response.headers.get('content-type')||'').toLowerCase();
    if (/^(audio|video|image|font)\//.test(ct) || /zip|octet-stream|pdf/.test(ct)) return;
    if(Number(response.headers.get('content-length')||0)>LIMIT) return;
    const r=response.clone();
    if(!r.body) return;
    const reader=r.body.getReader();const decoder=new TextDecoder();let chunks=[],size=0;
    try {
      while(true) {
        const v=await reader.read();if(v.done) break;
        size+=v.value.byteLength;
        if(size>LIMIT) {s.dropped++;await reader.cancel();return;}
        chunks.push(decoder.decode(v.value,{stream:true}));
      }
      chunks.push(decoder.decode());save(response.url,chunks.join(''));
    } catch(_) {} finally {try{reader.releaseLock();}catch(_){}}
  }
  const fetch0=window.fetch;
  if(fetch0) window.fetch=function(...args) {
    const p=fetch0.apply(this,args);
    p.then(r=>{bounded(r).catch(()=>{});}).catch(()=>{});return p;
  };
  const send0=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send=function(...args) {
    this.addEventListener('load',function() {
      try {
        if(this.responseType==='json') save(this.responseURL,JSON.stringify(this.response));
        else if(this.responseType==='' || this.responseType==='text') save(this.responseURL,this.responseText);
      }catch(_){}
    },{once:true});return send0.apply(this,args);
  };
})()'''

DRAIN_JS = r'''() => {
  const s=window.__uploaderODCaptureV2;
  if(!s) return {items:[],dropped:0};
  const result={items:s.items.splice(0),dropped:s.dropped};s.bytes=0;s.dropped=0;return result;
}'''


DOM_ROWS_JS = r'''() => Array.from(document.querySelectorAll('[role="row"][data-selection-index]'))
  .filter(r => r.getAttribute('data-selection-index') !== '-1').map(r => {
    const f = r.querySelector('[data-automationid="field-LinkFilename"]');
    const titled = f && (f.querySelector('[data-id="heroField"][title]') || f.querySelector('[title]'));
    const cb = r.querySelector('[data-automationid="selection-checkbox"]');
    const name = (titled && titled.getAttribute('title')) || (cb && cb.getAttribute('aria-label')) || (f && f.textContent.trim());
    const icons = Array.from(r.querySelectorAll('[data-automationid="field-DocIcon"] [aria-label]')).map(x => x.getAttribute('aria-label')).join(' ');
    return {id:r.getAttribute('data-selection-index'), name, folder: /folder|pasta/i.test(icons)};
  }).filter(x => x.name)'''

SCROLL_JS = r'''() => {
  const all = new Set();
  for (const grid of document.querySelectorAll('[role="grid"], [data-is-scrollable="true"]')) {
    let el = grid;
    for (let i=0; el && i<8; i++, el=el.parentElement) all.add(el);
  }
  const candidates = [...all].filter(e => e.scrollHeight > e.clientHeight + 8 &&
    /auto|scroll/.test(getComputedStyle(e).overflowY));
  let moved = false;
  for (const e of candidates) { const before=e.scrollTop; e.scrollTop += Math.max(350,e.clientHeight*0.85); moved ||= e.scrollTop > before; }
  if (!candidates.length) { const before=window.scrollY; window.scrollBy(0,Math.max(350,window.innerHeight*0.85)); moved=window.scrollY>before; }
  return moved;
}'''


@dataclass
class Limits:
    max_files: int = 2000
    max_folders: int = 100
    max_depth: int = 6
    max_pages: int = 100
    max_scrolls: int = 150
    total_seconds: float = 600
    page_seconds: float = 90
    settle_ms: int = 1000
    download_seconds: float = 12


def _selected_path(url: str) -> str:
    query = dict(parse_qsl(urlsplit(url).query))
    value = query.get('id') or query.get('RootFolder') or ''
    return value.rstrip('/') if value.startswith('/') else ''


BREADCRUMB_PATH_JS = r'''() => {
  const crumbs=[...document.querySelectorAll('[data-automationid="breadcrumb-listitem"][data-item-key]')];
  for(const el of crumbs.reverse()) {
    try {const a=JSON.parse(el.getAttribute('data-item-key'));
      const p=Array.isArray(a) && a.filter(v=>typeof v==='string' && v.startsWith('/')).pop();
      if(p) return p;
    }catch(_){}
  }
  return '';
}'''


def _network_code(error: Any) -> str:
    """No arbitrary exception message reaches diagnostics."""
    match = re.search(r'net::[A-Z0-9_]+', str(error or ''))
    return match.group(0) if match else type(error).__name__


def _page_state(page, cap: Capture) -> str:
    if cap.file_names or cap.folders or cap.files:
        return 'listing'
    host = _host(page.url)
    if host in ('login.live.com', 'login.microsoftonline.com'):
        return 'login'
    try:
        # Text is classified in memory; it is never printed or saved.
        text = page.locator('body').inner_text(timeout=1000).lower()[:12000]
    except Exception:
        return 'unavailable'
    for key, patterns in (
        ('access-denied', ('access denied', 'you need permission', 'request access', 'acesso negado', 'solicitar acesso')),
        ('not-found', ("this item might not exist", 'this link has expired', 'este link expirou', 'item nao existe')),
        ('empty', ('this folder is empty', 'esta pasta est\u00e1 vazia', 'this folder has no files')),
    ):
        if any(p in text for p in patterns):
            return key
    return 'no-listing'


def _match_file(entries: list[dict], entry: dict) -> dict | None:
    uid = entry.get('od_uid')
    if uid:
        hit = next((e for e in entries if e.get('od_uid') == uid), None)
        if hit and not hit.get('od_pending'):
            return hit
    name = entry.get('name')
    matches = [e for e in entries if e.get('name') == name and not e.get('od_pending')]
    return matches[0] if len(matches) == 1 else None


class BrowserResolver:
    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits()
        self.warnings: set[str] = set()
        self.deadline = 0.0
        self.diagnostics: list[dict] = []

    def _report(self, phase: str, page, cap: Capture, stats: dict, started: float) -> dict:
        report = {
            'version': 2, 'phase': phase,
            'seconds': round(time.monotonic() - started, 1),
            'state': _page_state(page, cap),
            'files': len(cap.file_names), 'folders': len(cap.folders),
            'download_urls': len(cap.files),
            **stats,
        }
        # Only predefined counters, status codes and error categories are emitted.
        self.diagnostics.append(report)
        print('[onedrive:v2] ' + json.dumps(report, ensure_ascii=True), flush=True)
        return report

    @staticmethod
    def _target_row(page, row: dict):
        iid = str(row.get('id') or '')
        if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', iid):
            return page.locator(f'[role="row"][data-selection-index="{iid}"]').first
        field = page.locator('[data-automationid="field-LinkFilename"]').filter(
            has_text=re.compile('^' + re.escape(str(row['name'])) + '$'))
        return page.locator('[role="row"][data-selection-index]').filter(has=field).first

    def _click_download(self, page, row: dict) -> str:
        """Use ONLY the selected row's context menu, never folder-wide Download."""
        try:
            page.keyboard.press('Escape')
            node = self._target_row(page, row)
            node.scroll_into_view_if_needed(timeout=3000)
            node.hover(timeout=3000)
            more = node.locator('[data-automationid="moreActionsHeroField"]')
            if more.count():
                more.first.click(timeout=3000)
            else:
                node.click(button='right', timeout=3000)
            # Fluent/Spartan context menu; languages seen in the supplied page.
            menu = page.get_by_role('menu')
            action = menu.get_by_role('menuitem', name=re.compile(
                r'^\s*(Download|Baixar|Transferir|Descargar)(?:\s*\([^)]*\))?\s*$', re.I)).first
            action.wait_for(state='visible', timeout=3000)
            if not action.is_enabled() or action.get_attribute('aria-disabled') == 'true':
                return 'disabled'
            action.click(timeout=3000)
            return 'clicked'
        except Exception as exc:
            return 'ui-' + type(exc).__name__
        finally:
            try:
                page.keyboard.press('Escape')
            except Exception:
                pass

    def read_page(self, context, page, url: str, *, target: dict | None = None,
                  phase: str = 'list') -> tuple[str, str, Capture]:
        cap = Capture()
        pending: deque = deque()
        visited: set[str] = set()
        inflight: set = set()
        started = time.monotonic()
        deadline = min(self.deadline, started + self.limits.page_seconds)
        stats = {'http': Counter(), 'network_failures': Counter(), 'json_objects': 0,
                 'js_errors': 0, 'body_read_errors': 0, 'navigation': 'ok',
                 'ui_download': 'not-needed', 'download_events': 0,
                 'non_http_downloads': 0, 'scope_confirmed': False}
        active: dict = {}
        download_pages = []

        def consume(data, response_url, headers):
            before = set(cap.next_links)
            cap.scan(data)
            stats['json_objects'] += 1
            for raw in cap.next_links - before:
                nxt = urljoin(response_url, raw)
                if _microsoft_url(nxt) and urlsplit(nxt).netloc == urlsplit(response_url).netloc:
                    pending.append((nxt, headers))
                else:
                    self.warnings.add('Paginacao com mudanca de origem nao foi seguida.')

        def consume_text(text, response_url, headers=None):
            for value in json_values(text):
                consume(value, response_url, headers or {})

        def on_request(request):
            if request.resource_type in ('document', 'xhr', 'fetch', 'script'):
                inflight.add(request)

        def on_finished(request):
            inflight.discard(request)

        def on_failed(request):
            inflight.discard(request)
            stats['network_failures'][_network_code(request.failure)] += 1

        def on_response(response):
            stats['http'][str(response.status)] += 1
            try:
                if response.status not in (200, 206) or not _microsoft_url(response.url):
                    return
                hdr = response.headers
                ct = hdr.get('content-type', '').lower()
                # A selected file may expose the signed URL only when Download is clicked.
                if (active and is_direct_url(response.url) and
                        ('attachment' in hdr.get('content-disposition', '').lower()) and
                        not any(t in ct for t in ('html', 'json', 'multipart/'))):
                    cap.add(active['name'], response.url, hdr.get('content-length'), active.get('id'))
                    return
                if re.match(r'^(audio|video|image|font)/', ct) or any(t in ct for t in ('zip', 'octet-stream', 'pdf')):
                    return
                if response.request.resource_type not in ('xhr', 'fetch') and 'json' not in ct:
                    return
                if _size(hdr.get('content-length')) > MAX_CAPTURE_BYTES:
                    self.warnings.add('Uma resposta excedeu o limite de captura; listagem pode ser parcial.')
                    return
                # No requirement that request and response be seen in the same page/tab.
                text = response.text()
                headers = {}
                try:
                    headers = {k: v for k, v in response.request.all_headers().items()
                               if k.lower() in ('authorization', 'accept', 'x-sharetoken', 'prefer')}
                except Exception:
                    pass
                visited.add(response.url)
                consume_text(text, response.url, headers)
            except Exception:
                stats['body_read_errors'] += 1

        def on_download(download):
            stats['download_events'] += 1
            try:
                if active and _microsoft_url(download.url):
                    suggested = download.suggested_filename or ''
                    if not suggested or suggested.casefold() == active['name'].casefold():
                        cap.add(active['name'], download.url, 0, active.get('id'))
                    else:
                        self.warnings.add('Download com nome diferente do selecionado foi ignorado.')
                elif active:
                    stats['non_http_downloads'] += 1
            finally:
                # Discovery must not download an entire multi-GB archive.
                try:
                    download.cancel()
                except Exception:
                    pass

        def attach_download(new_page):
            new_page.on('download', on_download)
            download_pages.append(new_page)

        def on_page_error(_):
            stats['js_errors'] += 1

        def drain():
            try:
                packet = page.evaluate(DRAIN_JS)
                if packet.get('dropped'):
                    self.warnings.add('Fila de captura excedida; resultado pode ser parcial.')
                for item in packet.get('items', []):
                    if _microsoft_url(item.get('url', '')):
                        consume_text(item.get('text', ''), item['url'])
            except Exception:
                pass

        context.on('request', on_request)
        context.on('requestfinished', on_finished)
        context.on('requestfailed', on_failed)
        context.on('response', on_response)
        context.on('page', attach_download)
        page.on('pageerror', on_page_error)
        attach_download(page)
        requested_path = _selected_path(url)
        print('[onedrive:v2] ' + json.dumps({'phase': phase, 'event': 'start'}), flush=True)
        try:
            try:
                remaining = max(100, min(45000, int((deadline - time.monotonic()) * 1000)))
                page.goto(url, wait_until='domcontentloaded', timeout=remaining)
            except Exception as exc:
                stats['navigation'] = _network_code(exc)
                if page.url == 'about:blank':
                    raise OneDriveError('OneDrive: navegacao falhou antes de abrir a pagina. Veja [onedrive:v2] no log.') from None
            idle, pages = 0, 0
            previous = None
            ui_attempted = False
            script_scanned = False
            for _ in range(self.limits.max_scrolls):
                if time.monotonic() >= deadline:
                    self.warnings.add('Tempo limite da pagina atingido; listagem pode ser parcial.')
                    break
                page.wait_for_timeout(self.limits.settle_ms)
                if _host(page.url) in ('login.live.com', 'login.microsoftonline.com'):
                    raise OneDriveError('OneDrive: o navegador foi redirecionado para login. Este runner nao possui uma sessao autenticada.')
                rows = page.evaluate(DOM_ROWS_JS)
                cap.dom(rows)
                drain()
                actual_path = page.evaluate(BREADCRUMB_PATH_JS) or _selected_path(page.url)
                stats['scope_confirmed'] = not requested_path or actual_path.rstrip('/') == requested_path
                if not script_scanned:
                    inline_json = page.locator('script[type="application/json"]').all_text_contents()
                    script_scanned = bool(inline_json)
                    for text in inline_json:
                        consume_text(text, page.url)
                while pending and pages < self.limits.max_pages and time.monotonic() < deadline:
                    nxt, headers = pending.popleft()
                    if nxt in visited:
                        continue
                    visited.add(nxt)
                    pages += 1
                    resp = None
                    try:
                        resp = context.request.get(nxt, headers=headers,
                            timeout=max(100, min(15000, int((deadline-time.monotonic())*1000))), max_redirects=0)
                        if resp.status != 200:
                            self.warnings.add(f'Paginacao recusada (HTTP {resp.status}); listagem parcial.')
                            continue
                        if _size(resp.headers.get('content-length')) <= MAX_CAPTURE_BYTES:
                            consume_text(resp.text(), nxt, headers)
                    except Exception:
                        self.warnings.add('Falha em uma pagina adicional; listagem parcial.')
                    finally:
                        if resp is not None:
                            resp.dispose()
                if target and stats['scope_confirmed']:
                    if _match_file(cap.entries(), target):
                        break
                    row = next((r for r in rows if not r.get('folder') and r.get('name') == target.get('name')), None)
                    if row and not ui_attempted:
                        ui_attempted = True
                        active.update(row)
                        stats['ui_download'] = self._click_download(page, row)
                        until = min(deadline, time.monotonic() + self.limits.download_seconds)
                        while (stats['ui_download'] == 'clicked' and time.monotonic() < until
                               and not _match_file(cap.entries(), target)):
                            page.wait_for_timeout(250)
                            drain()
                        active.clear()
                        if _match_file(cap.entries(), target):
                            break
                        if stats['ui_download'] == 'clicked':
                            stats['ui_download'] = 'no-http-link'
                        break
                if len(cap.file_names) >= self.limits.max_files or len(cap.files) >= self.limits.max_files:
                    self.warnings.add('Limite de arquivos atingido; listagem parcial.')
                    break
                snapshot = (len(cap.files), len(cap.folders), len(cap.file_names), len(visited))
                moved = page.evaluate(SCROLL_JS)
                idle = idle + 1 if snapshot == previous and not moved and not pending else 0
                previous = snapshot
                has_listing = bool(cap.file_names or cap.folders or cap.files)
                # No 15-second "empty" shortcut. Without listing, wait page_seconds.
                # Background telemetry must not keep a completed list alive forever.
                if idle >= 3 and has_listing and stats['scope_confirmed']:
                    break
                state = _page_state(page, cap) if not has_listing and idle >= 3 else 'loading'
                if state in ('access-denied', 'not-found', 'empty'):
                    break
            else:
                self.warnings.add('Limite de rolagem atingido; listagem parcial.')
            if pending:
                self.warnings.add('Limite de paginacao atingido; listagem parcial.')
            if not stats['scope_confirmed'] and (cap.file_names or cap.folders or cap.files):
                raise OneDriveError('OneDrive: a pagina aberta nao confirmou a subpasta solicitada. Nao vou enviar arquivos da pasta errada.')
            title = re.sub(r'\s*[-\u2013]\s*OneDrive\s*$', '', page.title()).strip()
            return page.url, title or 'OneDrive', cap
        finally:
            context.remove_listener('request', on_request)
            context.remove_listener('requestfinished', on_finished)
            context.remove_listener('requestfailed', on_failed)
            context.remove_listener('response', on_response)
            context.remove_listener('page', attach_download)
            page.remove_listener('pageerror', on_page_error)
            for dp in download_pages:
                try:
                    dp.remove_listener('download', on_download)
                except Exception:
                    pass
            self._report(phase, page, cap, stats, started)

    def collect(self, context, page, url: str, recursive: bool = True,
                *, target: dict | None = None) -> dict:
        self.deadline = time.monotonic() + self.limits.total_seconds
        context.add_init_script(script=CAPTURE_JS)
        root = clean_url(url)
        seed = sharing_url(root)
        if seed != root:
            # Same tab and context. Let the shared page actually load before
            # moving to /Sessions; do not close a still-authenticating seed tab.
            self.read_page(context, page, seed, phase='bootstrap')
        tasks = deque([(root, '', 0)])
        seen: set[str] = set()
        entries, title = [], 'OneDrive'
        visited_folders = 0
        last_cap = Capture()
        while tasks and visited_folders < self.limits.max_folders and len(entries) < self.limits.max_files:
            if time.monotonic() >= self.deadline:
                self.warnings.add('Tempo total de listagem atingido; resultado parcial.')
                break
            dest, rel, depth = tasks.popleft()
            key = _selected_path(dest) or dest
            if key in seen:
                continue
            seen.add(key)
            visited_folders += 1
            try:
                final, page_title, cap = self.read_page(context, page, dest,
                    target=target, phase='resolve-file' if target else 'list')
            except OneDriveError:
                if not entries and not rel:
                    raise
                self.warnings.add('Uma subpasta nao pode ser aberta; listagem parcial.')
                continue
            last_cap = cap
            if not rel:
                title = page_title
            if target:
                match = _match_file(cap.entries(), target)
                if not match:
                    last = self.diagnostics[-1] if self.diagnostics else {}
                    state = last.get('state', 'unknown')
                    ui = last.get('ui_download', 'not-attempted')
                    raise OneDriveError(
                        f'OneDrive: arquivo selecionado sem link de download. estado={state}; '
                        f'arquivos={len(cap.file_names)}; acao={ui}. Veja [onedrive:v2] no log. '
                        'Nenhuma pagina HTML foi enviada como arquivo.')
            now = time.time()
            for entry in cap.listing(final):
                entries.append({**entry, 'path': rel, 'presign': 'onedrive', 'od_page': final,
                                'od_resolved_at': 0 if entry.get('od_pending') else now})
            if target or not recursive:
                break
            for folder in sorted(cap.folders.values(), key=lambda f: f['name'].casefold()):
                if depth >= self.limits.max_depth:
                    self.warnings.add('Limite de profundidade atingido; subpastas omitidas.')
                    continue
                child = folder_url(final, folder)
                if not child:
                    self.warnings.add('Subpasta sem URL de navegacao; resultado parcial.')
                    continue
                child_key = _selected_path(child) or child
                if child_key not in seen:
                    tasks.append((child, (rel + '/' if rel else '') + folder['name'], depth + 1))
        if tasks or len(entries) > self.limits.max_files:
            self.warnings.add('Limite de arquivos/pastas atingido; resultado parcial.')
        if not entries:
            last = self.diagnostics[-1] if self.diagnostics else {}
            state = last.get('state', 'unknown')
            raise OneDriveError(
                f'OneDrive: nenhuma listagem de arquivos foi obtida. estado={state}; '
                f'pastas={len(last_cap.folders)}; respostas_JSON={last.get("json_objects", 0)}; '
                f'erros_JS={last.get("js_errors", 0)}. Veja [onedrive:v2] no log. '
                'Isso, sozinho, nao confirma pasta vazia ou privada.')
        dedup = {(e['path'], e['name']): e for e in entries}
        result = sorted(dedup.values(), key=lambda e: (e['path'].casefold(), e['name'].casefold()))
        pending_count = sum(bool(e.get('od_pending')) for e in result)
        note = ('OneDrive (listagem; download sob demanda)' if pending_count
                else 'OneDrive (downloadUrl via navegador)')
        return {'title': title, 'entries': result[:self.limits.max_files],
                'warn': '; '.join(sorted(self.warnings)), 'note': note}


def expand_public(url: str, *, recursive: bool = True, limits: Limits | None = None,
                  target: dict | None = None) -> dict:
    url = clean_url(url)
    if not is_onedrive_url(url) or not _microsoft_url(url):
        raise OneDriveError('OneDrive: URL de compartilhamento invalida.')
    if is_direct_url(url):
        return {'title': 'OneDrive', 'entries': [{'url': url, 'name': None, 'server_name': True}],
                'warn': '', 'note': 'OneDrive (link direto)'}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise OneDriveError('OneDrive: falta Playwright. Atualize o workflow uploader.yml.') from None
    with sync_playwright() as pw:
        # Real Chromium in its new headless mode, not chromium-headless-shell.
        executable = os.getenv('ONEDRIVE_CHROMIUM_EXECUTABLE')
        launch = {'headless': True}
        if executable:
            launch['executable_path'] = executable
        else:
            launch['channel'] = 'chromium'
        try:
            browser = pw.chromium.launch(**launch)
        except Exception:
            executable = executable or shutil.which('chromium') or shutil.which('google-chrome')
            if not executable:
                raise OneDriveError('OneDrive: instale o Chromium completo: python -m playwright install --with-deps chromium (sem --only-shell).') from None
            try:
                browser = pw.chromium.launch(headless=True, executable_path=executable)
            except Exception:
                raise OneDriveError('OneDrive: Chromium nao iniciou. Confira a instalacao no Actions.') from None
        context = None
        try:
            context = browser.new_context(locale='en-US', accept_downloads=True,
                service_workers='allow', viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            return BrowserResolver(limits).collect(context, page, url, recursive, target=target)
        except OneDriveError:
            raise
        except Exception as exc:
            raise OneDriveError('OneDrive: falha no navegador (' + type(exc).__name__ + '). Veja [onedrive:v2] no log.') from None
        finally:
            if context is not None:
                context.close()
            browser.close()


_REFRESH: dict[str, tuple[float, list[dict]]] = {}


def fresh_url(entry: dict, *, force: bool = False) -> str:
    """A pending descriptor must NEVER return its HTML page to download_to()."""
    now = time.time()
    if not entry.get('od_pending') and not force and now - float(entry.get('od_resolved_at') or 0) < 120:
        return entry['url']
    source = entry.get('od_page')
    if not source:
        if entry.get('od_pending'):
            raise OneDriveError('OneDrive: descritor pendente sem pagina de origem.')
        return entry['url']
    stamp, files = _REFRESH.get(source, (0, []))
    match = _match_file(files, entry) if not force and now - stamp < 90 else None
    if not match:
        files = expand_public(source, recursive=False, target={
            'name': entry.get('name'), 'od_uid': entry.get('od_uid'),
            'od_item_id': entry.get('od_item_id')})['entries']
        stamp = time.time()
        _REFRESH[source] = (stamp, files)
        match = _match_file(files, entry)
    if not match or match.get('od_pending') or not _microsoft_url(match.get('url', '')):
        raise OneDriveError('OneDrive: o arquivo escolhido nao produziu um link de download HTTP autorizado.')
    entry.update({'url': match['url'], 'od_resolved_at': stamp, 'od_pending': False,
                  'od_uid': match.get('od_uid', entry.get('od_uid'))})
    return entry['url']
