"""Public OneDrive links, resolved in an isolated browser (no account/password).

Reads the JSON responses that the page itself receives, like the supplied
userscript. Does not turn an HTML sharing page into a pretend download URL.
No cookies, authorization headers, or signed links are written to logs/disk.
"""
from __future__ import annotations

import base64
from collections import deque
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
    p = urlsplit(url)
    h = (p.hostname or '').lower()
    return p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443) and (
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


@dataclass
class Capture:
    """Collects only downloadUrl fields, never arbitrary URLs from a page."""
    files: dict[str, dict] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    folders: dict[str, dict] = field(default_factory=dict)
    file_names: set[str] = field(default_factory=set)
    next_links: set[str] = field(default_factory=set)

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
  if (window.__uploaderODCaptureV1) return;
  const state = window.__uploaderODCaptureV1 = {items: [], bytes: 0, dropped: 0};
  const LIMIT = 16 * 1024 * 1024;
  function save(url, data, bytes) {
    if (!data || typeof data !== 'object') return;
    if (bytes > LIMIT) { state.dropped++; return; }
    state.items.push({url, data, bytes}); state.bytes += bytes;
    while (state.items.length > 32 || state.bytes > LIMIT) {
      state.bytes -= state.items.shift().bytes; state.dropped++;
    }
  }
  function text(url, value) {
    if (typeof value !== 'string' || value.length > LIMIT) return;
    if (!/^[\s]*[\[{]/.test(value)) return;
    try { save(url, JSON.parse(value), value.length); } catch (_) {}
  }
  const originalFetch = window.fetch;
  if (originalFetch) window.fetch = function(...args) {
    const promise = originalFetch.apply(this, args);
    promise.then(r => {
      const ct = (r.headers.get('content-type') || '').toLowerCase();
      if (!ct.includes('json') && !ct.includes('text/plain')) return;
      if (Number(r.headers.get('content-length') || 0) > LIMIT) { state.dropped++; return; }
      try { r.clone().text().then(t => text(r.url, t)).catch(() => {}); } catch (_) {}
    }).catch(() => {});
    return promise;
  };
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(...args) {
    this.addEventListener('load', function() {
      try {
        if (this.responseType === 'json') {
          const data = this.response;
          save(this.responseURL, data, JSON.stringify(data).length);
        } else if (this.responseType === '' || this.responseType === 'text') {
          const ct = (this.getResponseHeader('content-type') || '').toLowerCase();
          if (ct.includes('json') || ct.includes('text/plain')) text(this.responseURL, this.responseText);
        }
      } catch (_) {}
    }, {once: true});
    return originalSend.apply(this, args);
  };
})()'''

DRAIN_JS = r'''() => {
  const s=window.__uploaderODCaptureV1;
  if (!s) return {items:[], dropped:0};
  const result={items:s.items.splice(0),dropped:s.dropped};s.bytes=0;s.dropped=0;return result;
}'''


DOM_ROWS_JS = r'''() => Array.from(document.querySelectorAll('[role="row"][data-selection-index]'))
  .filter(r => r.getAttribute('data-selection-index') !== '-1').map(r => {
    const f = r.querySelector('[data-automationid="field-LinkFilename"]');
    const titled = f && f.querySelector('[title]');
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
    settle_ms: int = 1200


class BrowserResolver:
    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits()
        self.warnings: set[str] = set()
        self.deadline = 0.0

    def read_page(self, context, page, url: str) -> tuple[str, str, Capture]:
        cap = Capture()
        pending: deque = deque()
        visited: set[str] = set()
        started_requests: set = set()
        started = time.monotonic()
        deadline = min(self.deadline, time.monotonic() + self.limits.page_seconds)

        def consume(data, response_url, headers):
            before = set(cap.next_links)
            cap.scan(data)
            for raw in cap.next_links - before:
                nxt = urljoin(response_url, raw)
                # Never send session authorization to a different origin.
                if _microsoft_url(nxt) and urlsplit(nxt).netloc == urlsplit(response_url).netloc:
                    pending.append((nxt, headers))
                else:
                    self.warnings.add('Uma pagina de resultados nao foi seguida por mudar de origem.')

        def on_request(request):
            started_requests.add(request)

        def on_response(response):
            try:
                if response.request not in started_requests:
                    return
                if response.status != 200 or not _microsoft_url(response.url):
                    return
                hdr = response.headers
                ct = hdr.get('content-type', '').lower()
                if 'json' not in ct and not (response.request.resource_type in ('xhr', 'fetch') and 'text/plain' in ct):
                    return
                if _size(hdr.get('content-length')) > 16 * 1024 * 1024:
                    self.warnings.add('Resposta JSON muito grande; listagem pode estar incompleta.')
                    return
                data = response.json()
                visited.add(response.url)
                try:
                    headers = {k: v for k, v in response.request.all_headers().items()
                               if k.lower() in ('authorization', 'accept', 'x-sharetoken', 'prefer')}
                except Exception:
                    headers = {}
                consume(data, response.url, headers)
            except Exception:
                # Telemetry and unrelated responses need not be JSON.
                return

        context.on('request', on_request)
        context.on('response', on_response)
        try:
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
            except Exception:
                # A timed-out navigation can still have loaded the folder.
                if page.url == 'about:blank':
                    raise OneDriveError('OneDrive: nao foi possivel abrir o compartilhamento no navegador.') from None
            if _host(page.url) in ('login.live.com', 'login.microsoftonline.com'):
                raise OneDriveError('OneDrive: este link exige login. Use um compartilhamento publico autorizado ou um downloadUrl valido.')
            idle, pages = 0, 0
            previous = None
            for _ in range(self.limits.max_scrolls):
                if time.monotonic() >= deadline:
                    self.warnings.add('Tempo limite da listagem atingido; resultado parcial.')
                    break
                page.wait_for_timeout(self.limits.settle_ms)
                cap.dom(page.evaluate(DOM_ROWS_JS))
                # Browser-side fetch/XHR capture also sees cached/service-worker responses.
                captured = page.evaluate(DRAIN_JS)
                if captured.get('dropped'):
                    self.warnings.add('Fila de respostas do navegador excedida; resultado parcial.')
                for item in captured.get('items', []):
                    if _microsoft_url(item.get('url', '')):
                        consume(item.get('data'), item['url'], {})
                for text in page.locator('script[type="application/json"]').all_text_contents():
                    if len(text) <= 16 * 1024 * 1024:
                        try:
                            consume(json.loads(text), page.url, {})
                        except ValueError:
                            pass
                while pending and pages < self.limits.max_pages and time.monotonic() < deadline:
                    nxt, headers = pending.popleft()
                    if nxt in visited:
                        continue
                    visited.add(nxt)
                    pages += 1
                    resp = None
                    try:
                        resp = context.request.get(nxt, headers=headers, timeout=20000, max_redirects=0)
                        if resp.status != 200:
                            self.warnings.add(f'Paginacao OneDrive recusada (HTTP {resp.status}); resultado parcial.')
                            continue
                        if _size(resp.headers.get('content-length')) > 16 * 1024 * 1024:
                            self.warnings.add('Pagina JSON muito grande; resultado parcial.')
                            continue
                        consume(resp.json(), nxt, headers)
                    except Exception:
                        self.warnings.add('Falha ao carregar uma pagina adicional; resultado parcial.')
                    finally:
                        if resp is not None:
                            resp.dispose()
                if pending:
                    self.warnings.add('Limite de paginas atingido; resultado parcial.')
                if len(cap.files) >= self.limits.max_files:
                    self.warnings.add('Limite de arquivos atingido; resultado parcial.')
                    break
                snapshot = (len(cap.files), len(cap.folders), len(cap.names), len(visited))
                moved = page.evaluate(SCROLL_JS)
                idle = idle + 1 if snapshot == previous and not moved and not pending else 0
                previous = snapshot
                has_listing = bool(cap.files or cap.folders or cap.names)
                waited_for_empty = time.monotonic() - started >= min(15, self.limits.page_seconds / 2)
                if idle >= 3 and (has_listing or waited_for_empty):
                    break
            else:
                self.warnings.add('Limite de rolagem atingido; resultado parcial.')
            entries = cap.entries()
            found = {e['name'] for e in entries}
            missing = len(cap.file_names - found)
            if missing:
                self.warnings.add(f'{missing} arquivo(s) visiveis sem downloadUrl capturado.')
            title = re.sub(r'\s*[-\u2013]\s*OneDrive\s*$', '', page.title()).strip()
            return page.url, title or 'OneDrive', cap
        finally:
            context.remove_listener('request', on_request)
            context.remove_listener('response', on_response)

    def collect(self, context, page, url: str, recursive: bool = True) -> dict:
        self.deadline = time.monotonic() + self.limits.total_seconds
        context.add_init_script(script=CAPTURE_JS)
        root = clean_url(url)
        # The redeem link grants the public browser context access, but is NOT
        # substituted for the requested deep URL: /Sessions must stay /Sessions.
        seed = sharing_url(root)
        if seed != root:
            seed_page = context.new_page()
            try:
                seed_page.goto(seed, wait_until='domcontentloaded', timeout=45000)
                seed_page.wait_for_timeout(self.limits.settle_ms)
            except Exception:
                pass
            finally:
                seed_page.close()
        tasks = deque([(root, '', 0)])
        seen: set[str] = set()
        entries, title = [], 'OneDrive'
        visited_folders = 0
        while tasks and visited_folders < self.limits.max_folders and len(entries) < self.limits.max_files:
            if time.monotonic() >= self.deadline:
                self.warnings.add('Tempo total de listagem atingido; resultado parcial.')
                break
            target, rel, depth = tasks.popleft()
            if target in seen:
                continue
            seen.add(target)
            visited_folders += 1
            try:
                final_url, page_title, cap = self.read_page(context, page, target)
            except OneDriveError:
                if not entries and not rel:
                    raise
                self.warnings.add('Uma subpasta nao pode ser aberta; resultado parcial.')
                continue
            seen.add(final_url)
            if not rel:
                title = page_title
            now = time.time()
            for e in cap.entries():
                entries.append({**e, 'path': rel, 'presign': 'onedrive', 'od_page': final_url,
                                'od_resolved_at': now})
            if not recursive:
                break
            for folder in sorted(cap.folders.values(), key=lambda f: f['name'].casefold()):
                if depth >= self.limits.max_depth:
                    self.warnings.add('Limite de profundidade atingido; subpastas omitidas.')
                    continue
                child = folder_url(final_url, folder)
                if not child:
                    self.warnings.add('Uma subpasta nao apresentou URL de navegacao; resultado parcial.')
                    continue
                if child not in seen:
                    tasks.append((child, (rel + '/' if rel else '') + folder['name'], depth + 1))
        if tasks or len(entries) > self.limits.max_files:
            self.warnings.add('Limite de arquivos/pastas atingido; resultado parcial.')
        if not entries:
            raise OneDriveError(
                'OneDrive: nenhum downloadUrl foi recebido. A pasta pode estar vazia, exigir login, '
                'ter download bloqueado ou nao carregar no runner. '
                'Abra o link em uma janela anonima; links diretos capturados pelo userscript tambem sao aceitos.'
            )
        # Stable identity/order across the picker and the second Actions run.
        dedup = {(e['path'], e['od_uid']): e for e in entries}
        result = sorted(dedup.values(), key=lambda e: (e['path'].casefold(), e['name'].casefold(), e['od_uid']))
        return {'title': title, 'entries': result[:self.limits.max_files],
                'warn': '; '.join(sorted(self.warnings)), 'note': 'OneDrive (downloadUrl via navegador)'}


def expand_public(url: str, *, recursive: bool = True, limits: Limits | None = None) -> dict:
    url = clean_url(url)
    if not is_onedrive_url(url) or not _microsoft_url(url):
        raise OneDriveError('OneDrive: URL de compartilhamento invalida.')
    if is_direct_url(url):
        return {'title': 'OneDrive', 'entries': [{'url': url, 'name': None, 'server_name': True}],
                'warn': '', 'note': 'OneDrive (link direto)'}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise OneDriveError('OneDrive: falta Playwright. Atualize tambem o workflow uploader.yml.') from None
    with sync_playwright() as pw:
        launch = {'headless': True}
        executable = os.getenv('ONEDRIVE_CHROMIUM_EXECUTABLE')
        if executable:
            launch['executable_path'] = executable
        try:
            browser = pw.chromium.launch(**launch)
        except Exception:
            executable = executable or shutil.which('chromium') or shutil.which('google-chrome')
            if not executable:
                raise OneDriveError('OneDrive: Chromium ausente. Execute python -m playwright install --with-deps chromium.') from None
            try:
                browser = pw.chromium.launch(headless=True, executable_path=executable)
            except Exception:
                raise OneDriveError('OneDrive: Chromium nao iniciou. Confira a etapa de instalacao no Actions.') from None
        try:
            context = browser.new_context(locale='en-US', accept_downloads=False, service_workers='allow')
            page = context.new_page()
            return BrowserResolver(limits).collect(context, page, url, recursive)
        except OneDriveError:
            raise
        except Exception:
            raise OneDriveError('OneDrive: falha na leitura pelo navegador. Confira acesso publico e dependencias do Chromium.') from None
        finally:
            browser.close()


_REFRESH: dict[str, tuple[float, list[dict]]] = {}


def fresh_url(entry: dict, *, force: bool = False) -> str:
    """Refresh from the original folder, not from an expired signed URL."""
    now = time.time()
    if not force and now - float(entry.get('od_resolved_at') or 0) < 120:
        return entry['url']
    source = entry.get('od_page')
    if not source:
        return entry['url']
    stamp, files = _REFRESH.get(source, (0, []))
    if force or now - stamp >= 90:
        files = expand_public(source, recursive=False)['entries']
        stamp = time.time()
        _REFRESH[source] = (stamp, files)
    match = next((f for f in files if f.get('od_uid') == entry.get('od_uid')), None)
    if match is None:
        match = next((f for f in files if f.get('name') == entry.get('name')), None)
    if not match:
        raise OneDriveError('OneDrive: o arquivo selecionado nao foi encontrado novamente na pasta.')
    entry['url'], entry['od_resolved_at'] = match['url'], stamp
    return entry['url']
