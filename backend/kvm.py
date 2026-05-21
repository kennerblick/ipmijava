"""KVM session manager.

Each session owns:
  - an Xvfb virtual display
  - an x11vnc server on that display
  - a websockify WebSocket→TCP bridge
  - a Java process running the iKVM JAR directly (no javaws)

Sessions are tracked in-process; gunicorn must run with a single worker.
"""

import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests as _req
import urllib3 as _urllib3
_urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)

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
    session_id: str
    server_id:  str
    display:    int
    port_vnc:   int
    port_ws:    int
    tmpdir:     str
    procs:   list = field(default_factory=list)
    status:  str  = 'starting'   # starting | running | error | stopped
    message: str  = 'Initialisiere…'
    error:   str  = ''


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
        sid      = str(uuid.uuid4())[:8]
        display  = _alloc_display()
        port_vnc = _alloc_port(_PORT_VNC_START)
        port_ws  = _alloc_port(_PORT_WS_START)
        tmpdir   = tempfile.mkdtemp(prefix='kvm_')
        sess     = KvmSession(sid, server['id'], display, port_vnc, port_ws, tmpdir)
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


def _do_run(sess: 'KvmSession', server: dict) -> None:
    base   = f"https://{server['ip']}"
    tmpdir = Path(sess.tmpdir)
    disp   = f':{sess.display}'

    def msg(text: str) -> None:
        sess.message = text

    # ── 1. TigerVNC (X-Server + VNC integriert, ersetzt Xvfb + x11vnc) ──────
    msg('Starte TigerVNC (X + VNC)…')
    vnc = subprocess.Popen(
        ['Xtigervnc', disp,
         '-rfbport', str(sess.port_vnc),
         '-SecurityTypes', 'None',
         '-geometry', '1280x800',
         '-depth', '24',
         '-ac', '+extension', 'GLX'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sess.procs.append(vnc)
    time.sleep(1.5)
    if vnc.poll() is not None:
        raise RuntimeError('Xtigervnc konnte nicht gestartet werden')

    # ── 2. websockify ─────────────────────────────────────────────────────────
    msg(f'Starte WebSocket-Proxy (Port {sess.port_ws})…')
    ws = subprocess.Popen(
        ['websockify', str(sess.port_ws), f'127.0.0.1:{sess.port_vnc}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sess.procs.append(ws)
    time.sleep(0.3)

    # ── 4. BMC login ──────────────────────────────────────────────────────────
    msg(f'Login am BMC {server["ip"]}…')
    http_sess, sid = _bmc_login(base, server['username'], server['password'])

    # ── 5. JNLP fetch ─────────────────────────────────────────────────────────
    resp = _fetch_jnlp(http_sess, base, sid, progress_fn=msg)
    if not resp or '<jnlp' not in resp.text.lower():
        raise RuntimeError(
            f'JNLP nicht gefunden — BMC Login OK (SID: {"ja" if sid else "nein"}), '
            'aber keiner der JNLP-Pfade hat geantwortet. '
            'BMC-Firmware-Version prüfen oder Debug-Endpoint nutzen.'
        )

    # ── 6. Parse JNLP ─────────────────────────────────────────────────────────
    msg('Analysiere JNLP…')
    jars, native_jars, main_class, arguments = _parse_jnlp(resp.text, base)
    if not main_class:
        raise RuntimeError(
            f'Keine main-class im JNLP. JNLP-Inhalt (Anfang): {resp.text[:300]}'
        )

    # ── 7. JARs herunterladen ─────────────────────────────────────────────────
    cp_paths = []
    for i, url in enumerate(jars, 1):
        name = url.split('/')[-1].split('?')[0] or f'ikvm{i}.jar'
        msg(f'Lade JAR {i}/{len(jars)}: {name}…')
        dest = tmpdir / name
        r = http_sess.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        cp_paths.append(str(dest))

    if not cp_paths:
        raise RuntimeError('Keine JARs im JNLP gefunden')

    # ── 8. Native JARs (*.so) extrahieren ─────────────────────────────────────
    for url in native_jars:
        name = url.split('/')[-1].split('?')[0] or 'native.jar'
        msg(f'Lade Native-JAR: {name}…')
        dest = tmpdir / name
        r = http_sess.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        try:
            with zipfile.ZipFile(dest) as z:
                for entry in z.namelist():
                    if any(entry.endswith(ext) for ext in ('.so', '.dylib', '.dll')):
                        z.extract(entry, path=str(tmpdir))
        except Exception:
            pass

    # ── 9. Java starten ───────────────────────────────────────────────────────
    msg(f'Starte Java ({main_class.split(".")[-1]})…')
    env = os.environ.copy()
    env['DISPLAY'] = disp

    cmd = [
        'java',
        f'-Djava.library.path={tmpdir}',
        '-Dawt.useSystemAAFontSettings=on',
        '-Dswing.aatext=true',
        '-Djava.awt.headless=false',
        # For older JARs that use reflection internals (Java 9+ module guard)
        '--add-opens=java.base/java.lang=ALL-UNNAMED',
        '--add-opens=java.desktop/sun.awt=ALL-UNNAMED',
        '--add-opens=java.desktop/java.awt=ALL-UNNAMED',
        '-cp', ':'.join(cp_paths),
        main_class,
    ] + arguments

    java = subprocess.Popen(
        cmd, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(tmpdir),
    )
    sess.procs.append(java)
    sess.status  = 'running'
    sess.message = f'Java läuft (PID {java.pid}) — iKVM wird geladen…'

    # Wait for Java to exit, then auto-cleanup
    java.wait()
    sess.message = 'Java beendet'
    stop_session(sess.session_id)


def _kill(sess: 'KvmSession') -> None:
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
