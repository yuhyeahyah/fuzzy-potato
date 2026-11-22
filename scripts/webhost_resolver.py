"""Send/adminForge and BuzzHeavier: preserve the browser's original download.

No CAPTCHA solvers, credential import, token synthesis or HTTP replay. Send's
own client decrypts using the URL fragment; the API stream is NOT a plain file.
The transfer runs in a bounded subprocess and is published only after validation.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from html import unescape
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit
import zipfile


class WebHostError(RuntimeError):
    """Sanitized user-facing error: no signed URLs, fragments or cookies."""


SEND_HOSTS = frozenset({'send.adminforge.de'})
BUZZ_HOST = 'buzzheavier.com'
RESERVE_BYTES = 256 * 1024 * 1024


def clean_url(value: str) -> str:
    value = unescape(str(value or '').strip().strip('<>'))
    value = re.sub(r'\\([:/.?&=_%#!()\-])', r'\1', value).rstrip('\\')
    if not value.startswith(('https://', 'http://')):
        value = 'https://' + value
    return value


def _parts(url: str):
    try:
        p = urlsplit(clean_url(url))
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            raise ValueError
        if not p.hostname or any(ord(c) < 32 for c in url):
            raise ValueError
        return p
    except (ValueError, TypeError):
        raise WebHostError('Host: link HTTPS invalido ou com credenciais/porta nao permitidas.') from None


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith('.' + domain)


def is_send_url(url: str) -> bool:
    try:
        return (urlsplit(clean_url(url)).hostname or '').lower() in SEND_HOSTS
    except ValueError:
        return False


def is_buzzheavier_url(url: str) -> bool:
    try:
        return _under((urlsplit(clean_url(url)).hostname or '').lower(), BUZZ_HOST)
    except ValueError:
        return False


def needs_browser(url: str) -> bool:
    # Detection is intentionally broader than validation: invalid links get a
    # clear provider error, not generic HTML fallback.
    return is_send_url(url) or is_buzzheavier_url(url)


@dataclass(frozen=True)
class Link:
    provider: str
    kind: str
    file_id: str
    original: str
    page: str

    @property
    def label(self) -> str:
        return 'Send/adminForge' if self.provider == 'send' else 'BuzzHeavier'


def parse_link(url: str) -> Link:
    source = clean_url(url)
    p = _parts(source)
    host = (p.hostname or '').lower()
    if host in SEND_HOSTS:
        match = re.fullmatch(r'/(?:api/)?download/([a-fA-F0-9]{10,64})/?', p.path)
        if not match:
            raise WebHostError('Send/adminForge: use o link /download/ID/#CHAVE compartilhado pelo site.')
        # A Send v3 secret is a 16-byte base64url value. Do not strip its #.
        if not p.fragment:
            raise WebHostError('Send/adminForge: falta a chave depois de #. O link /api/download/ID sozinho '
                               'nao permite descriptografar. Envie o link completo /download/ID/#CHAVE.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{22}(?:==)?', p.fragment):
            raise WebHostError('Send/adminForge: chave do link invalida. Copie o link completo do botao Compartilhar.')
        page = urlunsplit((p.scheme, p.netloc, '/download/' + match[1] + '/', p.query, p.fragment))
        return Link('send', 'page', match[1], source, page)
    if _under(host, BUZZ_HOST):
        if host in (BUZZ_HOST, 'www.' + BUZZ_HOST):
            match = re.fullmatch(r'/([A-Za-z0-9]{6,32})(/download)?/?', p.path)
            kind = 'endpoint' if match and match[2] else 'page'
        else:
            match = re.fullmatch(r'/d/([A-Za-z0-9]{6,32})/?', p.path)
            kind = 'cdn'
        if not match:
            raise WebHostError('BuzzHeavier: use /ID, /ID/download?token ou o link assinado /d/ID do servidor de arquivos.')
        return Link('buzzheavier', kind, match[1], source, 'https://buzzheavier.com/' + match[1])
    raise WebHostError('Host: este modulo aceita Send/adminForge e BuzzHeavier.')


def expand_public(url: str) -> dict:
    """No GET on the download API and no consumed download while discovering.

    A Send archive is one download, not independently downloadable members.
    /unzip telegram can split it after the one successful decrypted transfer.
    """
    spec = parse_link(url)
    name = ('Send-' if spec.provider == 'send' else 'BuzzHeavier-') + spec.file_id
    warn = ('O Send entrega um arquivo ou um pacote ZIP. Para separar o conteudo de um ZIP, '
            'use /unzip telegram com o mesmo link completo.' if spec.provider == 'send' else '')
    return {'title': spec.label, 'entries': [{
        'url': spec.original, 'presign': spec.provider, 'name': name, 'size': 0,
        'path': '', 'server_name': True,
    }], 'warn': warn, 'note': spec.label + ' (nome e download pelo navegador)'}


def fresh_url(entry: dict) -> str:
    # An authenticated/decrypted browser download is not a portable raw URL.
    spec = parse_link(entry.get('url', ''))
    if spec.provider == 'send':
        raise WebHostError('Send/adminForge: nao ha URL direta do arquivo descriptografado; '
                           'use /uploader com o link completo, incluindo #CHAVE.')
    if spec.kind == 'cdn':
        return spec.original  # Preserve v= byte for byte; do not claim validation.
    raise WebHostError('BuzzHeavier: o link final e temporario e pode depender da sessao. '
                       'Use /uploader sem o modo link para baixar no navegador.')


def safe_name(value: str) -> str:
    name = unicodedata.normalize('NFC', str(value or ''))
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', name).strip(' .')
    if name in ('', '.', '..'):
        return 'arquivo'
    stem, suffix = os.path.splitext(name)
    suffix = suffix[:24]
    while len((stem + suffix).encode('utf-8')) > 220:
        stem = stem[:-1]
    return (stem or 'arquivo') + suffix


def _same_name(a: str, b: str) -> bool:
    return safe_name(a).casefold() == safe_name(b).casefold()


def validate_file(path: Path, name: str, exact_size: int | None = None) -> int:
    size = path.stat().st_size
    if exact_size is not None and size != exact_size:
        raise WebHostError('Download: tamanho recebido diferente do esperado; arquivo nao enviado.')
    with path.open('rb') as stream:
        head = stream.read(4096).lstrip(b'\xef\xbb\xbf\x00\t\r\n ')
    suffix = Path(name).suffix.lower()
    if suffix not in ('.html', '.htm') and head.lower().startswith(
            (b'<!doctype html', b'<html', b'<head', b'<body')):
        raise WebHostError('Download: o navegador recebeu HTML, nao o arquivo. Nada foi enviado.')
    if suffix == '.zip' and not zipfile.is_zipfile(path):
        raise WebHostError('Download: ZIP incompleto ou dados ainda criptografados. Nada foi enviado.')
    # Catch a small API error masquerading as arbitrary media, not valid JSON files.
    if suffix not in ('.json', '.txt', '.html', '.htm') and size <= 4096 and head.startswith(b'{'):
        try:
            obj = json.loads(head)
        except (ValueError, UnicodeDecodeError):
            obj = None
        if isinstance(obj, dict) and any(k in obj for k in ('error', 'errors', 'message')):
            raise WebHostError('Download: resposta de erro JSON em vez do arquivo. Nada foi enviado.')
    return size


def log(provider: str, phase: str, event: str, **metrics) -> None:
    # Callers pass code-generated categories/numbers only, never page text/URLs.
    print('[' + provider + ':v1] ' + json.dumps({
        'phase': phase, 'event': event, **metrics,
    }, ensure_ascii=True), flush=True)


@dataclass
class Limits:
    page_seconds: float = 60
    event_seconds: float = 90
    poll_ms: int = 250
    sw_seconds: float = 10


@contextmanager
def open_browser(downloads: Path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise WebHostError('Hosts: falta Playwright; atualize .github/workflows/uploader.yml.') from None
    with sync_playwright() as pw:
        executable = os.getenv('WEBHOST_CHROMIUM_EXECUTABLE')
        opts = {'headless': True, 'downloads_path': str(downloads)}
        if executable:
            opts['executable_path'] = executable
        else:
            opts['channel'] = 'chromium'
        try:
            browser = pw.chromium.launch(**opts)
        except Exception:
            executable = executable or shutil.which('chromium') or shutil.which('google-chrome')
            if not executable:
                raise WebHostError('Hosts: Chromium nao instalado. Atualize o workflow; '
                                   'ele deve executar python -m playwright install --with-deps chromium.') from None
            try:
                browser = pw.chromium.launch(headless=True, executable_path=executable,
                                             downloads_path=str(downloads))
            except Exception:
                raise WebHostError('Hosts: Chromium nao iniciou. Confira a instalacao no Actions.') from None
        context = None
        try:
            context = browser.new_context(accept_downloads=True, service_workers='allow',
                locale='en-US', viewport={'width': 1280, 'height': 900})
            page = context.new_page()
            page.set_default_timeout(5000)
            # Real download links stay in this tab; advertising popups are not
            # sources. Do not export cookies or inject a challenge solution.
            context.on('page', lambda other: other.close() if other != page else None)
            yield context, page
        except WebHostError:
            raise
        except Exception as exc:
            raise WebHostError('Hosts: falha no navegador (' + type(exc).__name__ + '). '
                               'Veja [send:v1] ou [buzzheavier:v1] no log.') from None
        finally:
            if context is not None:
                context.close()
            browser.close()


def _trusted_download(url: str, spec: Link) -> bool:
    try:
        if url.startswith('blob:'):
            return spec.provider == 'send' and urlsplit(url[5:]).hostname == urlsplit(spec.page).hostname
        p = _parts(url)
        if spec.provider == 'send':
            return (p.hostname or '').lower() == urlsplit(spec.page).hostname and bool(
                re.fullmatch(r'/(?:api/)?download/' + re.escape(spec.file_id) + r'/?', p.path))
        parsed = parse_link(url)
        return parsed.provider == spec.provider and parsed.file_id == spec.file_id and parsed.kind != 'page'
    except WebHostError:
        return False


class Capture:
    def __init__(self, spec: Link):
        self.spec = spec
        self.armed = False
        self.expected_name = ''
        self.download = None
        self.http = Counter()
        self.failed_requests = Counter()
        self.download_events = 0
        self.rejected = 0
        self.hx_redirect = None
        self.js_errors = 0
        self.sw_responses = 0
        self.metadata_status = None
        self.document_status = None
        self.send_sw_ready = False
        self.uncontrolled_send_downloads = 0

    def on_download(self, download):
        self.download_events += 1
        allowed = self.armed and self.download is None and _trusted_download(download.url, self.spec)
        if allowed and self.spec.provider == 'send' and not download.url.startswith('blob:') and not self.send_sw_ready:
            # An API attachment without the decryption worker can be ciphertext.
            # Do not send it just because Content-Disposition names a real file.
            self.uncontrolled_send_downloads += 1
            allowed = False
        if allowed and self.expected_name:
            allowed = _same_name(download.suggested_filename, self.expected_name)
        if allowed:
            self.download = download
        else:
            self.rejected += 1
            try:
                download.cancel()
            except Exception:
                pass

    def on_response(self, response):
        try:
            p = urlsplit(response.url)
            if self.spec.provider == 'send':
                known = p.hostname == urlsplit(self.spec.page).hostname
            else:
                known = _under(p.hostname or '', BUZZ_HOST)
            if not known:
                return
            self.http[str(response.status)] += 1
            if response.from_service_worker:
                self.sw_responses += 1
                if self.spec.provider == 'send' and _trusted_download(response.url, self.spec):
                    self.send_sw_ready = True
            if response.request.resource_type == 'document':
                self.document_status = response.status
            if self.spec.provider == 'send' and '/api/metadata/' in p.path:
                self.metadata_status = response.status
            # Only a successful response for THIS file's /download may supply
            # HX-Redirect. Never follow arbitrary redirects from ads/API errors.
            if self.spec.provider == 'buzzheavier' and response.status < 400 and re.fullmatch(
                    '/' + re.escape(self.spec.file_id) + '/download/?', p.path):
                loc = response.headers.get('hx-redirect')
                if loc:
                    candidate = urljoin(response.url, loc)
                    if _trusted_download(candidate, self.spec):
                        self.hx_redirect = candidate
        except Exception:
            pass

    def attach(self, context, page):
        page.on('download', self.on_download)
        context.on('response', self.on_response)
        def failed(request):
            code = re.search(r'net::[A-Z0-9_]+', str(request.failure or ''))
            self.failed_requests[code[0] if code else 'other'] += 1
        context.on('requestfailed', failed)
        def js_error(_):
            self.js_errors += 1
        page.on('pageerror', js_error)

    def report(self, phase: str, event: str):
        log(self.spec.provider, phase, event, http=dict(self.http),
            network_failures=dict(self.failed_requests), download_events=self.download_events,
            retained_downloads=int(self.download is not None), rejected_downloads=self.rejected,
            service_worker_responses=self.sw_responses, js_errors=self.js_errors,
            metadata_status=self.metadata_status, document_status=self.document_status,
            service_worker_controlled=self.send_sw_ready,
            uncontrolled_send_downloads=self.uncontrolled_send_downloads)


# Uses the exact download controls in the HTML supplied by the user, not generic
# text matches that could accidentally press an advertisement's download button.
SEND_STATE_JS = r"""() => {
  const arc = document.querySelector('send-archive');
  const b = document.querySelector('#download-btn');
  const name = arc && arc.querySelector('h1');
  return {name: name ? name.textContent.trim() : '',
    members: arc ? arc.querySelectorAll('send-file').length : 0,
    button: !!(b && b.getClientRects().length && !b.disabled),
    password: [...document.querySelectorAll('input[type=password]')]
      .some(e => e.getClientRects().length > 0),
    sw: !!(navigator.serviceWorker && navigator.serviceWorker.controller)};
}"""
BUZZ_STATE_JS = r"""() => {
  const n = document.querySelector('.file-name');
  const links = [...document.querySelectorAll('.download-row .download-btn, a.download-btn')];
  return {name: n ? n.textContent.trim() : '',
    links: links.filter(e => e.getClientRects().length).map(e => ({
      hx: e.getAttribute('hx-get') || '', href: e.getAttribute('href') || ''}))};
}"""
ERROR_STATE_JS = r"""() => {
  const text = (document.body ? document.body.innerText : '').toLowerCase();
  const title = document.title.toLowerCase();
  // Merely loading the Turnstile script is NOT an active challenge.
  const challengeText = /verify (that )?you are human|verifique.*humano|checking your browser|performing security verification/.test(text);
  const challengeTitle = /just a moment|attention required/.test(title);
  const visibleChallenge = [...document.querySelectorAll('.cf-turnstile, #challenge-running, #challenge-stage')]
    .some(e => e.getClientRects().length > 0);
  return {challenge: challengeTitle || (challengeText && visibleChallenge),
    expired: /link has expired|link expirou|download link has expired|file has expired|arquivo expirou|arquivo foi removido/.test(text),
    denied: /access denied|acesso negado/.test(title)};
}"""


def _state(page, script):
    try:
        return page.evaluate(script) or {}
    except Exception:
        return {}


def _check_page_error(page, cap: Capture, *, signed_retry: bool = False) -> None:
    error = _state(page, ERROR_STATE_JS)
    if error.get('challenge'):
        raise WebHostError(cap.spec.label + ': o site exige verificacao humana/Cloudflare no runner. '
                           'O bot nao resolve CAPTCHA nem contorna esse bloqueio.')
    if signed_retry:
        return
    if error.get('expired') or cap.metadata_status in (404, 410):
        raise WebHostError(cap.spec.label + ': compartilhamento expirado, removido ou com limite esgotado.')
    if error.get('denied'):
        raise WebHostError(cap.spec.label + ': acesso recusado pelo site neste runner.')


def _navigate(page, cap: Capture, url: str, limits: Limits, *, referer: str | None = None):
    cap.document_status = None
    try:
        response = page.goto(url, wait_until='domcontentloaded', timeout=limits.page_seconds * 1000,
                             **({'referer': referer} if referer else {}))
        if response is not None:
            cap.document_status = response.status
    except Exception as exc:
        # Successful native downloads often make goto raise "Download is starting".
        # Do not print its exception: it contains the signed URL.
        log(cap.spec.provider, 'navigation', 'interrupted', error_type=type(exc).__name__)
    page.wait_for_timeout(limits.poll_ms)


def _wait_download(page, cap: Capture, limits: Limits, *, seconds: float | None = None):
    deadline = time.monotonic() + (limits.event_seconds if seconds is None else seconds)
    while cap.download is None and time.monotonic() < deadline:
        _check_page_error(page, cap)
        page.wait_for_timeout(limits.poll_ms)
    return cap.download


def _send_download(context, page, cap: Capture, limits: Limits):
    _navigate(page, cap, cap.spec.page, limits)
    deadline = time.monotonic() + limits.page_seconds
    state = {}
    while time.monotonic() < deadline:
        _check_page_error(page, cap)
        state = _state(page, SEND_STATE_JS)
        if state.get('password'):
            raise WebHostError('Send/adminForge: este compartilhamento pede senha adicional. '
                               'Esta integracao aceita links sem senha adicional; a chave # continua obrigatoria.')
        if state.get('button') and state.get('name'):
            break
        if cap.document_status in (401, 403, 404, 410):
            raise WebHostError('Send/adminForge: pagina recusada ou indisponivel '
                               '(HTTP ' + str(cap.document_status) + ').')
        page.wait_for_timeout(limits.poll_ms)
    else:
        raise WebHostError('Send/adminForge: a pagina nao apresentou o arquivo e o botao Baixar. '
                           'Chave incorreta, expiracao ou falha de carregamento; veja [send:v1].')
    # Send can stream a decrypted API response through its service worker, or
    # use a same-origin blob fallback. Wait for worker activation before clicking.
    sw_deadline = time.monotonic() + min(limits.sw_seconds, limits.page_seconds)
    while not state.get('sw') and time.monotonic() < sw_deadline:
        page.wait_for_timeout(limits.poll_ms)
        current = _state(page, SEND_STATE_JS)
        state['sw'] = current.get('sw', False)
    cap.send_sw_ready = bool(state.get('sw'))
    cap.expected_name = state['name']
    log('send', 'page', 'ready', archive_members=int(state.get('members', 0)),
        service_worker_controlled=bool(state.get('sw')))
    cap.armed = True
    # Exactly one click: retrying a Send download may exhaust its download count.
    try:
        page.locator('#download-btn').click(timeout=10000, no_wait_after=True)
    except Exception as exc:
        if cap.download is None:
            log('send', 'click', 'interrupted', error_type=type(exc).__name__)
    got = _wait_download(page, cap, limits)
    if got is None:
        if cap.uncontrolled_send_downloads:
            raise WebHostError('Send/adminForge: a pagina tentou baixar a API sem o service worker de '
                               'descriptografia. O arquivo foi recusado para nao enviar dados criptografados.')
        raise WebHostError('Send/adminForge: o download descriptografado nao iniciou. '
                           'Nenhuma chamada direta a /api/download foi tentada; veja [send:v1].')
    return got


def _buzz_download(context, page, cap: Capture, limits: Limits):
    spec = cap.spec
    if spec.kind != 'page':
        # Preserve the COMPLETE supplied signed URL and try it first.
        cap.armed = True
        if spec.kind == 'endpoint':
            page.set_extra_http_headers({'HX-Request': 'true', 'HX-Current-URL': spec.page})
        _navigate(page, cap, spec.original, limits, referer=spec.page)
        page.set_extra_http_headers({})
        if cap.download is not None:
            return cap.download
        _check_page_error(page, cap, signed_retry=True)
        if cap.hx_redirect:
            direct, cap.hx_redirect = cap.hx_redirect, None
            _navigate(page, cap, direct, limits, referer=spec.page)
            if cap.download is not None:
                return cap.download
        # Refresh the public share page ONCE after an expired/unusable signed
        # link. Do not fabricate /ID/download, t= or v= values.
        cap.report('signed-link', 'refresh-share-page')
        cap.armed = False
        cap.hx_redirect = None
    _navigate(page, cap, spec.original if spec.kind == 'page' else spec.page, limits)
    deadline = time.monotonic() + limits.page_seconds
    button = None
    state = {}
    while time.monotonic() < deadline:
        _check_page_error(page, cap)
        state = _state(page, BUZZ_STATE_JS)
        for link in state.get('links', []):
            action = link.get('hx') or link.get('href')
            target = urljoin(page.url, action)
            if action and _trusted_download(target, spec):
                # JSON encoding makes a safe attribute selector including &, quotes.
                attr = 'hx-get' if link.get('hx') else 'href'
                button = page.locator('a.download-btn[' + attr + '=' + json.dumps(action) + ']').first
                break
        if button is not None:
            break
        if cap.document_status in (401, 403, 404, 410):
            raise WebHostError('BuzzHeavier: pagina recusada pelo site (HTTP ' + str(cap.document_status) + '). '
                               'O link foi preservado; o runner pode estar bloqueado ou o arquivo indisponivel.')
        page.wait_for_timeout(limits.poll_ms)
    if button is None:
        raise WebHostError('BuzzHeavier: botao de download deste arquivo nao apareceu. Veja [buzzheavier:v1].')
    cap.expected_name = state.get('name') or ''
    cap.armed = True
    log('buzzheavier', 'page', 'ready', has_filename=bool(cap.expected_name))
    try:
        button.click(timeout=10000, no_wait_after=True)
    except Exception as exc:
        if cap.download is None:
            log('buzzheavier', 'click', 'interrupted', error_type=type(exc).__name__)
    got = _wait_download(page, cap, limits)
    if got is None:
        raise WebHostError('BuzzHeavier: clique realizado, mas nenhum download valido iniciou. '
                           'O site pode exigir verificacao ou recusar o runner; veja [buzzheavier:v1].')
    return got


def _publish_download(download, directory: Path, provider: str, expected_name: str = '') -> Path:
    try:
        name = download.suggested_filename
        if not name or (expected_name and not _same_name(name, expected_name)):
            raise WebHostError('Download: nome recebido nao corresponde ao arquivo da pagina.')
        # path() waits for the ORIGINAL request; never requests.get(download.url).
        log(provider, 'transfer', 'started', original_download=True)
        completed = Path(download.path())
    except WebHostError:
        raise
    except Exception as exc:
        log(provider, 'transfer', 'failed', error_type=type(exc).__name__)
        raise WebHostError('Download: a transferencia iniciada no navegador nao terminou. '
                           'Nenhum arquivo parcial foi enviado.') from None
    name = safe_name(expected_name or name)
    size = validate_file(completed, name)
    log(provider, 'transfer', 'validated', bytes=size, zip_structure=Path(name).suffix.lower() == '.zip')
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(10000):
        target = directory / (name if n == 0 else f'{Path(name).stem} ({n}){Path(name).suffix}')
        try:
            os.link(completed, target)
            break
        except FileExistsError:
            continue
        except OSError:
            raise WebHostError('Download: nao foi possivel preservar o arquivo no disco do runner.') from None
    else:
        raise WebHostError('Download: muitos arquivos locais com o mesmo nome.')
    completed.unlink(missing_ok=True)
    log(provider, 'transfer', 'saved', bytes=size)
    return target


def _download_in_browser(entry: dict, directory: Path, downloads: Path, *, limits: Limits | None = None) -> Path:
    spec = parse_link(entry.get('url', ''))
    limits = limits or Limits()
    downloads.mkdir(parents=True, exist_ok=True)
    cap = Capture(spec)
    with open_browser(downloads) as (context, page):
        cap.attach(context, page)
        try:
            if spec.provider == 'send':
                download = _send_download(context, page, cap, limits)
            else:
                download = _buzz_download(context, page, cap, limits)
            cap.report('resolve', 'download-started')
            # Preserve before the context closes; PW deletes its originals.
            return _publish_download(download, directory, spec.provider, cap.expected_name)
        finally:
            cap.report('browser', 'finished')


def _kill_process(proc) -> None:
    if os.name == 'posix':
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()
    proc.wait()


def _bytes_on_disk(directory: Path) -> int:
    total = 0
    for path in directory.iterdir():
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def download_public(entry: dict, directory: Path, *, timeout: float | None = None) -> Path:
    spec = parse_link(entry.get('url', ''))
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if timeout is None:
        try:
            timeout = float(os.getenv('WEBHOST_DOWNLOAD_TIMEOUT', '3600'))
        except ValueError:
            raise WebHostError('Hosts: WEBHOST_DOWNLOAD_TIMEOUT deve ser um numero de segundos.') from None
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 14400:
        raise WebHostError('Hosts: timeout deve estar entre 1 e 14400 segundos.')
    if shutil.disk_usage(directory).free < RESERVE_BYTES:
        raise WebHostError('Download: disco do runner sem espaco livre suficiente.')
    with tempfile.TemporaryDirectory(prefix='.webhost-', dir=directory) as tmp:
        work = Path(tmp)
        downloads = work / 'downloads'
        downloads.mkdir()
        result_file = work / 'result.json'
        env = dict(os.environ)
        for key in ('BOT_TOKEN', 'TG_API_ID', 'TG_API_HASH', 'TG_PREMIUM_SESSION', 'PAYLOAD', 'GITHUB_TOKEN'):
            env.pop(key, None)
        env['PYTHONUNBUFFERED'] = '1'
        env['TMPDIR'] = str(work)
        data = json.dumps({'entry': {'url': spec.original}, 'directory': str(directory),
                           'downloads': str(downloads), 'result_file': str(result_file)})
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--download-entry'],
            stdin=subprocess.PIPE, text=True, env=env, start_new_session=(os.name == 'posix'))
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_process(proc)
                    raise WebHostError(spec.label + ': tempo limite de download excedido; transferencia cancelada.')
                try:
                    proc.communicate(input=data, timeout=min(15, remaining))
                    break
                except subprocess.TimeoutExpired:
                    data = None
                    log(spec.provider, 'transfer', 'waiting', bytes_on_disk=_bytes_on_disk(downloads))
                    if shutil.disk_usage(directory).free < RESERVE_BYTES:
                        _kill_process(proc)
                        raise WebHostError('Download: disco do runner ficou sem espaco; transferencia cancelada.')
            try:
                result = json.loads(result_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise WebHostError('Download: navegador encerrou sem resultado valido.') from None
            if proc.returncode != 0 or not result.get('ok'):
                raise WebHostError(result.get('error') or 'Download: navegador falhou.')
            target = Path(result.get('path', '')).resolve()
            if target.parent != directory or not target.is_file():
                raise WebHostError('Download: arquivo final nao foi preservado no destino esperado.')
            return target
        except BaseException:
            if proc.poll() is None:
                _kill_process(proc)
            raise


def _worker() -> int:
    result_file = None
    try:
        spec = json.load(sys.stdin)
        result_file = Path(spec['result_file'])
        path = _download_in_browser(spec['entry'], Path(spec['directory']), Path(spec['downloads']))
        result, code = {'ok': True, 'path': str(path)}, 0
    except WebHostError as exc:
        result, code = {'ok': False, 'error': str(exc)}, 1
    except Exception as exc:
        result, code = {'ok': False, 'error': 'Download: falha interna (' + type(exc).__name__ + ').'}, 1
    if result_file is not None:
        result_file.write_text(json.dumps(result), encoding='utf-8')
    return code


if __name__ == '__main__':
    if sys.argv[1:] == ['--download-entry']:
        raise SystemExit(_worker())
    print('This module is called by scripts/uploader.py.', file=sys.stderr)
    raise SystemExit(2)
