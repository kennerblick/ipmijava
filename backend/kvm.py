"""KVM session manager.

Each session owns:
  - a websockify WebSocket→TCP bridge to the BMC's VNC port (IKVM_PORT from GETPORTSINFO)
  - noVNC in the browser connects via the WebSocket bridge

Sessions are tracked in-process; gunicorn must run with a single worker.
"""

import asyncio
import re
import shutil
import socket
import ssl as _ssl
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests as _req
import urllib3 as _urllib3
_urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)

# ── Playwright JNLP fetch ──────────────────────────────────────────────────────

def _fetch_jnlp_playwright(base: str, username: str, password: str,
                            progress_fn=None) -> Optional[str]:
    """Use headless Chromium to fetch the JNLP from BMC.

    Strategy:
    1. Login via Chromium (establishes cookie-based session)
    2. Load man_ikvm so the BMC's JavaScript (pollServer / GetJNLPRequest) runs
    3. Fetch the JNLP URL using the browser's fetch() API (keeps session cookies)
    4. Fallback: page.goto() directly to the JNLP URL and read the body

    This works where python-requests fails because the BMC session is tied to the
    IP that first established it.  Chromium runs inside the same container, so the
    IP matches.  Python-requests creates a *separate* session with the same IP but
    different internal state — Chromium also executes the BMC's JavaScript fully.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if progress_fn:
            progress_fn('Playwright nicht installiert — überspringe')
        return None

    def _pw_login(page, base, username, password):
        """Login to ATEN/AMI BMC via Playwright.

        ATEN login form quirks:
        - Submit button: <input id="login_word" onclick="javascript: checkform(this)">
          NOT type="submit" — requires clicking #login_word to trigger checkform().
        - checkform() validates the form and then submits it.
        - Never bypass checkform() with a raw form.submit() — the BMC may reject it.
        """
        page.goto(f"{base}/cgi/login.cgi",
                  wait_until='domcontentloaded', timeout=15_000)
        # Fill credentials (same field names regardless of firmware generation)
        for name_sel in ['input[name="name"]', 'input[name="username"]', 'input#name']:
            try:
                page.locator(name_sel).first.fill(username, timeout=2_000)
                break
            except Exception:
                continue
        for pwd_sel in ['input[name="pwd"]', 'input[name="password"]', 'input#pwd']:
            try:
                page.locator(pwd_sel).first.fill(password, timeout=2_000)
                break
            except Exception:
                continue
        # Click the login button — ATEN uses onclick="checkform(this)" not type=submit
        btn_sel = (
            '#login_word, '                        # ATEN legacy
            'input[onclick*="checkform" i], '       # ATEN with checkform()
            'input[value*="login" i], '             # value="Login"
            'input[type="submit"], '                # standard
            'button[type="submit"]'                 # standard
        )
        page.locator(btn_sel).first.click(timeout=5_000)
        page.wait_for_load_state('networkidle', timeout=12_000)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--ignore-certificate-errors',
                ],
            )
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()

            if progress_fn:
                progress_fn('Playwright: Login am BMC…')
            _pw_login(page, base, username, password)

            # Inject SID into window scope BEFORE man_ikvm's JavaScript runs.
            # ATEN's utils.js references top.SID in GETPORTSINFO / upgrade_process
            # XHR calls.  When man_ikvm loads outside a frameset (top === window),
            # top.SID is undefined → the BMC rejects the API call → JS redirects
            # to login.  add_init_script() fires before any in-page script.
            sid_val = next(
                (c['value'] for c in ctx.cookies() if c['name'] == 'SID'), ''
            )
            if sid_val:
                ctx.add_init_script(
                    f"window.SID = '{sid_val}'; "
                    "window.lang_setting = window.lang_setting || 'English';"
                )

            # Set the navigation cookies man_ikvm expects
            bmc_host = base.replace('https://', '')
            for cname, cval in [('mainpage', 'remote'), ('subpage', 'man_ikvm')]:
                ctx.add_cookies([
                    {'name': cname, 'value': cval, 'domain': bmc_host, 'path': '/'},
                ])

            # Load man_ikvm — with SID injected, pollServer() / GetJNLPRequest() will run
            if progress_fn:
                progress_fn('Playwright: Lade man_ikvm (pollServer)…')
            try:
                page.goto(
                    f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm",
                    wait_until='domcontentloaded', timeout=15_000,
                )
                page.wait_for_timeout(3_500)   # give pollServer() time to fire
            except Exception:
                pass

            jnlp_url = f"{base}/cgi/url_redirect.cgi?url_name=ikvm"

            # ── Method A: browser fetch() (keeps session cookies, same IP) ───
            if progress_fn:
                progress_fn('Playwright: fetch JNLP via browser…')
            try:
                jnlp_text = page.evaluate(
                    f"""async () => {{
                        const r = await fetch({jnlp_url!r}, {{credentials:'include'}});
                        return await r.text();
                    }}"""
                )
                if jnlp_text and '<jnlp' in jnlp_text.lower():
                    browser.close()
                    if progress_fn:
                        progress_fn('Playwright: JNLP via fetch erhalten!')
                    return jnlp_text
            except Exception:
                pass

            # ── Method B: page.goto() directly (captures response body) ──────
            if progress_fn:
                progress_fn('Playwright: goto JNLP URL…')
            try:
                resp = page.goto(jnlp_url, wait_until='commit', timeout=10_000)
                if resp:
                    body = resp.body().decode('utf-8', errors='replace')
                    if '<jnlp' in body.lower():
                        browser.close()
                        if progress_fn:
                            progress_fn('Playwright: JNLP via goto erhalten!')
                        return body
                # Also check rendered page source (for text/xml responses)
                try:
                    content = page.content()
                    if '<jnlp' in content.lower():
                        browser.close()
                        return content
                except Exception:
                    pass
            except Exception:
                pass

            # ── Method C: try other known JNLP paths via browser fetch ────────
            for path in [
                '/cgi/CGI_GetJNLPContent.cgi',
                '/cgi/url_redirect.cgi?url_name=launch',
                '/cgi/url_redirect.cgi?url_name=man_ikvm',
            ]:
                try:
                    jnlp_text = page.evaluate(
                        f"""async () => {{
                            const r = await fetch({(base + path)!r}, {{credentials:'include'}});
                            return await r.text();
                        }}"""
                    )
                    if jnlp_text and '<jnlp' in jnlp_text.lower():
                        browser.close()
                        return jnlp_text
                except Exception:
                    continue

            browser.close()
            if progress_fn:
                progress_fn('Playwright: kein JNLP gefunden')
            return None

    except Exception as exc:
        if progress_fn:
            progress_fn(f'Playwright fehlgeschlagen: {exc}')
        return None

# ── Constants ─────────────────────────────────────────────────────────────────

_DISPLAY_START  = 10
_PORT_VNC_START = 5910
_PORT_WS_START  = 6080
_MAX_SESSIONS   = 10
_RESOLUTION     = '1280x800x24'

_BROWSER_UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# JNLP URL candidates — tried in order, most common first
_JNLP_PATHS = [
    # Some ATEN/Supermicro firmware appends JNLP XML to the man_ikvm page
    # response AFTER the iKVM service poll confirms readiness.  Try it first
    # in pass 2 (extra_header=True, Referer=man_ikvm, mainpage/subpage cookies set).
    '/cgi/url_redirect.cgi?url_name=man_ikvm',
    '/cgi/url_redirect.cgi?url_name=ikvm',
    '/cgi/url_redirect.cgi?url_name=launch',
    '/cgi/url_redirect.cgi?url_name=java_iview',
    '/cgi/url_redirect.cgi?url_name=iview',
    '/cgi/url_redirect.cgi?url_name=kvm',
    '/cgi/url_redirect.cgi?url_name=kvmjnlp',
    '/cgi/CGI_GetJNLPContent.cgi',
    '/cgi/getJNLP.cgi',
    '/cgi/ikvm.cgi',
    '/launch.jnlp',
    '/iKVM.jnlp',
    '/iKVM/iKVM.jnlp',
]

# ── State ─────────────────────────────────────────────────────────────────────

_lock:     threading.Lock = threading.Lock()
_sessions: dict           = {}   # session_id -> KvmSession


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class KvmSession:
    session_id:   str
    server_id:    str
    display:      int
    port_vnc:     int
    port_ws:      int
    tmpdir:       str
    procs:        list = field(default_factory=list)
    status:       str  = 'starting'   # starting | running | error | stopped
    message:      str  = 'Initialisiere…'
    error:        str  = ''
    vnc_password: str  = ''
    bmc_ip:       str  = ''
    bmc_cookies:  dict = field(default_factory=dict)


# ── Allocation helpers ────────────────────────────────────────────────────────

def _used_displays() -> set:
    return {s.display for s in _sessions.values()}


def _used_ports() -> set:
    return {s.port_vnc for s in _sessions.values()} | {s.port_ws for s in _sessions.values()}


def _alloc_display() -> int:
    used = _used_displays()
    for d in range(_DISPLAY_START, _DISPLAY_START + _MAX_SESSIONS):
        if d not in used and not Path(f'/tmp/.X{d}-lock').exists():
            return d
    raise RuntimeError('Kein freies X-Display')


def _alloc_port(start: int) -> int:
    used = _used_ports()
    for p in range(start, start + _MAX_SESSIONS * 3):
        if p in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', p)) != 0:
                return p
    raise RuntimeError(f'Kein freier Port ab {start}')


# ── BMC login (simplified, fast — no browser-state simulation needed) ────────

def _bmc_login(base: str, username: str, password: str) -> tuple:
    """Login to BMC, return (requests.Session, sid_or_None).

    Handles both ATEN legacy (SID in HTML body) and AMI MegaRAC (SID cookie).
    Intentionally avoids the expensive browser navigation simulation used by
    the browser-relay endpoint — for the proxy we only need the SID.
    """
    sess = _req.Session()
    sess.verify = False
    sess.headers['User-Agent'] = _BROWSER_UA

    try:
        r = sess.post(
            f"{base}/cgi/login.cgi",
            data={'name': username, 'pwd': password},
            timeout=10,
            allow_redirects=True,
        )
    except Exception as e:
        raise RuntimeError(f"BMC nicht erreichbar: {e}")

    # SID from cookie (AMI) or HTML body (ATEN legacy)
    sid = None
    for name in ('SID', 'sid', 'QSESSIONID'):
        v = sess.cookies.get(name) or r.cookies.get(name)
        if v:
            sid = v
            break
    if not sid:
        for pat in (r'[?&]SID=([a-fA-F0-9]+)',
                    r'[Ss][Ii][Dd]\s*[=:]\s*[\'"]([a-fA-F0-9]{8,})'):
            m = re.search(pat, r.text)
            if m:
                sid = m.group(1)
                break

    if sid:
        sess.cookies.set('SID', sid)

    return sess, sid


def _fetch_jnlp(sess, base: str, sid: Optional[str], progress_fn=None) -> Optional[_req.Response]:
    """Fetch JNLP from BMC.

    Pass 1 – direct paths (no navigation; works for X8/X9/X10 with SID cookie).
    Pass 2 – minimal navigation to man_ikvm + single poll (needed for newer AMI
              firmware that checks session state before serving url_name=ikvm).
    """
    jnlp_ref = f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm"

    def _try_paths(extra_header=None):
        hdrs = {'Referer': jnlp_ref} if extra_header else {}
        for path in _JNLP_PATHS:
            candidates = [f"{base}{path}"]
            if sid:
                sep = '&' if '?' in path else '?'
                candidates.append(f"{base}{path}{sep}SID={sid}")
            for url in candidates:
                try:
                    r = sess.get(url, timeout=3, headers=hdrs)
                    ct = r.headers.get('Content-Type', '')
                    if '<jnlp' in r.text.lower() or 'jnlp' in ct.lower():
                        return r
                    # Only skip SID variant for static file paths (not CGI).
                    # CGI paths return 404 on auth failure, not missing file,
                    # so the SID variant may still succeed.
                    if r.status_code == 404 and '/cgi/' not in path:
                        break
                except Exception:
                    continue
        return None

    if progress_fn:
        progress_fn('Suche JNLP (direkte Pfade)…')
    result = _try_paths()
    if result:
        return result

    # Minimal navigation — skip expensive topmenu/man_ikvm scraping
    if progress_fn:
        progress_fn('BMC-Navigation (man_ikvm + poll)…')

    topmenu_url = f"{base}/cgi/url_redirect.cgi?url_name=topmenu"
    for url, timeout in [
        (f"{base}/cgi/url_redirect.cgi?url_name=mainmenu", 6),
        (topmenu_url, 8),
    ]:
        try:
            sess.get(url, timeout=timeout)
        except Exception:
            pass

    sess.cookies.set('mainpage', 'remote')
    sess.cookies.set('subpage',  'man_ikvm')

    try:
        sess.get(jnlp_ref, timeout=10, headers={'Referer': topmenu_url})
    except Exception:
        pass

    # Two polls mirror what the browser does before serving url_name=ikvm
    poll_hdrs = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': jnlp_ref,
    }
    for _ in range(2):
        try:
            sess.post(
                f"{base}/cgi/upgrade_process.cgi",
                data='fwtype=255',
                headers=poll_hdrs,
                timeout=8,
            )
        except Exception:
            pass

    # Wait briefly — BMC iKVM service may take a moment to start after the polls
    time.sleep(2)

    # Retry scan up to 3 times; some BMCs need a few extra seconds
    for attempt in range(1, 4):
        if progress_fn:
            progress_fn(f'Suche JNLP (Versuch {attempt}/3)…')
        result = _try_paths(extra_header=True)
        if result:
            return result
        if attempt < 3:
            time.sleep(3)

    return None


# ── JNLP parser ───────────────────────────────────────────────────────────────

def _parse_jnlp(text: str, base: str) -> tuple:
    """Return (jar_urls, nativelib_urls, main_class, arguments).

    Handles mixed HTML+JNLP responses: some ATEN BMCs return the man_ikvm HTML
    page with the JNLP XML appended after </html>.  We extract just the JNLP
    portion before passing it to ElementTree.

    Only includes nativelib entries for Linux x86_64 (the container OS).
    """
    lo = text.lower()
    start = lo.find('<jnlp')
    end   = lo.rfind('</jnlp>')
    if start != -1:
        text = text[start : end + 7] if end != -1 else text[start:]

    text = text.replace(' xmlns=', ' _xmlns=')
    root = ET.fromstring(text)
    codebase = root.get('codebase', base).rstrip('/')

    jars, native_jars = [], []
    for res in root.iter('resources'):
        res_os   = (res.get('os',   '') or '').lower()
        res_arch = (res.get('arch', '') or '').lower()

        # Skip OS-specific blocks that don't match the Linux container
        if res_os:
            if 'linux' not in res_os:
                continue                                    # Windows / Mac
            if res_arch and res_arch not in ('x86_64', 'amd64'):
                continue                                    # 32-bit Linux

        for el in res:
            href = el.get('href', '').strip()
            if not href:
                continue
            url = href if href.startswith('http') else f"{codebase}/{href.lstrip('/')}"
            if el.tag == 'nativelib':
                native_jars.append(url)
            elif el.tag == 'jar':
                jars.append(url)

    app_el    = root.find('.//application-desc')
    main_cls  = app_el.get('main-class', '') if app_el is not None else ''
    arguments = [a.text or '' for a in (app_el.findall('argument') if app_el is not None else [])]
    return jars, native_jars, main_cls, arguments


# ── Public API ────────────────────────────────────────────────────────────────

def start_session(server: dict) -> 'KvmSession':
    """Allocate a session and launch it in a background thread."""
    with _lock:
        active = [s for s in _sessions.values() if s.status in ('starting', 'running')]
        if len(active) >= _MAX_SESSIONS:
            raise RuntimeError('Zu viele aktive KVM-Sessions (max 10)')
        sid     = str(uuid.uuid4())[:8]
        port_ws = _alloc_port(_PORT_WS_START)
        tmpdir  = tempfile.mkdtemp(prefix='kvm_')
        sess    = KvmSession(sid, server['id'], 0, 0, port_ws, tmpdir)
        _sessions[sid] = sess

    threading.Thread(
        target=_run,
        args=(sess, server),
        daemon=True,
        name=f'kvm-{sid}',
    ).start()
    return sess


def get_session(session_id: str) -> Optional['KvmSession']:
    return _sessions.get(session_id)


def get_session_for_server(server_id: str) -> Optional['KvmSession']:
    for s in _sessions.values():
        if s.server_id == server_id and s.status in ('starting', 'running', 'error'):
            return s
    return None


def stop_session(session_id: str) -> None:
    with _lock:
        sess = _sessions.pop(session_id, None)
    if sess:
        _kill(sess)


# ── Session lifecycle ─────────────────────────────────────────────────────────

def _run(sess: 'KvmSession', server: dict) -> None:
    try:
        _do_run(sess, server)
    except Exception as e:
        sess.status  = 'error'
        sess.error   = str(e)
        sess.message = f'Fehler: {e}'
        _kill(sess)
        # Keep in _sessions for 60 s so the frontend poll can read the error,
        # then clean up. (Immediate pop causes silent 404 in the UI.)
        def _deferred_remove():
            time.sleep(60)
            _sessions.pop(sess.session_id, None)
        threading.Thread(target=_deferred_remove, daemon=True).start()


def _run_ws_proxy(local_port: int, bmc_uri: str, cookies: dict,
                  stop_event: threading.Event) -> None:
    """Asyncio WebSocket-to-WebSocket proxy.

    Accepts plain ws:// connections from the browser and forwards them to the
    BMC's TLS WebSocket at wss://BMC:443/ with the BMC session cookies injected.
    The BMC uses the ATEN-proprietary RFB 055.008 protocol which requires its
    own noVNC client — this proxy is transparent to the RFB protocol.
    """
    import websockets  # installed at runtime; not in top-level imports

    ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE

    bmc_host = bmc_uri.split('/')[2]
    cookie_header = '; '.join(f'{k}={v}' for k, v in cookies.items())
    extra_headers = {
        'Cookie': cookie_header,
        'Origin': f'https://{bmc_host}',
    }

    async def proxy_handler(browser_ws):
        try:
            async with websockets.connect(
                bmc_uri,
                ssl=ssl_ctx,
                additional_headers=extra_headers,
                ping_interval=None,
                max_size=None,
                compression=None,
            ) as bmc_ws:
                async def fwd(src, dst):
                    try:
                        async for msg in src:
                            await dst.send(msg)
                    except Exception:
                        pass

                t1 = asyncio.create_task(fwd(browser_ws, bmc_ws))
                t2 = asyncio.create_task(fwd(bmc_ws, browser_ws))
                done, pending = await asyncio.wait(
                    [t1, t2], return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
        except Exception:
            pass

    def select_subprotocol(connection, protocols):
        if 'binary' in protocols:
            return 'binary'
        if protocols:
            return protocols[0]
        return None  # accept connections without subprotocol too

    async def main():
        async with websockets.serve(
            proxy_handler,
            '0.0.0.0',
            local_port,
            select_subprotocol=select_subprotocol,
            ping_interval=None,
            max_size=None,
            compression=None,
        ):
            while not stop_event.is_set():
                await asyncio.sleep(0.5)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def _activate_ikvm_service(http_sess, base: str) -> None:
    """Simulate full browser navigation to activate the BMC's iKVM service.

    Some ATEN firmware variants (e.g. B16NA) require the browser to navigate
    to man_ikvm and poll upgrade_process.cgi before the WebSocket endpoint at
    wss://BMC:443/ will start sending RFB data.  Without this sequence the BMC
    accepts the WebSocket connection silently but never initiates the handshake.
    """
    topmenu_url  = f"{base}/cgi/url_redirect.cgi?url_name=topmenu"
    man_ikvm_url = f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm"
    poll_url     = f"{base}/cgi/upgrade_process.cgi"

    for url in (f"{base}/cgi/url_redirect.cgi?url_name=mainmenu", topmenu_url):
        try:
            http_sess.get(url, timeout=6)
        except Exception:
            pass

    http_sess.cookies.set('mainpage', 'remote')
    http_sess.cookies.set('subpage',  'man_ikvm')

    try:
        http_sess.get(man_ikvm_url, timeout=10, headers={'Referer': topmenu_url})
    except Exception:
        pass

    poll_hdrs = {
        'Referer':      man_ikvm_url,
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    for _ in range(2):
        try:
            http_sess.post(poll_url, data='fwtype=255', headers=poll_hdrs, timeout=8)
        except Exception:
            pass


def _do_run(sess: 'KvmSession', server: dict) -> None:
    base = f"https://{server['ip']}"

    def msg(text: str) -> None:
        sess.message = text

    # ── 1. BMC login ─────────────────────────────────────────────────────────
    msg(f'Login am BMC {server["ip"]}…')
    http_sess, _sid = _bmc_login(base, server['username'], server['password'])

    # ── 2. Activate iKVM service (navigate to man_ikvm + poll upgrade_process) ─
    # Some ATEN firmware (e.g. B16NA) requires this before the BMC WebSocket
    # starts sending RFB data. For firmware that doesn't need it this is a no-op.
    msg('Aktiviere iKVM-Dienst am BMC…')
    _activate_ikvm_service(http_sess, base)

    # ── 3. Fetch entry_value auth token from HTML5 bootstrap page ────────────
    msg('Hole HTML5-KVM Authentifizierungstoken…')
    try:
        r = http_sess.get(
            f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm_html5_bootstrap",
            timeout=10,
        )
        m = re.search(r'id="entry_value"\s+value="([^"]+)"', r.text)
        entry_value = m.group(1) if m else ''
    except Exception:
        entry_value = ''

    sess.vnc_password = entry_value
    sess.bmc_ip = server['ip']
    # Use raw iteration to avoid CookieConflictError from duplicate-name cookies
    sess.bmc_cookies = {c.name: c.value for c in http_sess.cookies}

    # ── 3. Start WebSocket proxy: ws://localhost:port_ws → wss://BMC:443/ ────
    bmc_ws_uri = f"wss://{server['ip']}:443/"
    stop_event = threading.Event()
    sess._stop_event = stop_event  # type: ignore[attr-defined]

    msg(f'Starte WebSocket-Proxy → {server["ip"]}:443 (Port {sess.port_ws})…')
    proxy_thread = threading.Thread(
        target=_run_ws_proxy,
        args=(sess.port_ws, bmc_ws_uri, sess.bmc_cookies, stop_event),
        daemon=True,
        name=f'ws-proxy-{sess.session_id}',
    )
    proxy_thread.start()
    time.sleep(0.5)
    if not proxy_thread.is_alive():
        raise RuntimeError('WebSocket-Proxy konnte nicht gestartet werden')

    sess.status  = 'running'
    sess.message = f'KVM bereit — HTML5-Proxy Port {sess.port_ws}'

    while proxy_thread.is_alive():
        time.sleep(2)

    raise RuntimeError('WebSocket-Proxy unerwartet beendet')


def _kill(sess: 'KvmSession') -> None:
    stop_ev = getattr(sess, '_stop_event', None)
    if stop_ev is not None:
        stop_ev.set()
    for p in reversed(sess.procs):
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(0.4)
    for p in sess.procs:
        try:
            p.kill()
        except Exception:
            pass
    shutil.rmtree(sess.tmpdir, ignore_errors=True)
    sess.status = 'stopped'
