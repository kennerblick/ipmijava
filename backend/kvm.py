"""KVM session manager.

Each session owns:
  - an Xvfb virtual display
  - an x11vnc server on that display
  - a websockify WebSocket→TCP bridge
  - a Java process running the iKVM JAR directly (no javaws)

Sessions are tracked in-process; use a single gunicorn worker.
"""

import os
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
from typing import Callable, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

_DISPLAY_START  = 10
_PORT_VNC_START = 5910
_PORT_WS_START  = 6080
_MAX_SESSIONS   = 10
_RESOLUTION     = '1280x800x24'

# ── State ─────────────────────────────────────────────────────────────────────

_lock: threading.Lock = threading.Lock()
_sessions: dict = {}  # session_id -> KvmSession


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class KvmSession:
    session_id: str
    server_id:  str
    display:    int
    port_vnc:   int
    port_ws:    int
    tmpdir:     str
    procs:  list = field(default_factory=list)
    status: str  = 'starting'   # starting | running | error | stopped
    error:  str  = ''


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
    raise RuntimeError('Kein freies X-Display verfügbar')


def _alloc_port(start: int) -> int:
    used = _used_ports()
    for p in range(start, start + _MAX_SESSIONS * 3):
        if p in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', p)) != 0:
                return p
    raise RuntimeError(f'Kein freier Port ab {start}')


# ── JNLP parser ───────────────────────────────────────────────────────────────

def _parse_jnlp(text: str, base: str) -> tuple:
    """Return (jar_urls, nativelib_urls, main_class, arguments)."""
    # Strip namespace prefixes so ElementTree queries stay simple
    text = text.replace(' xmlns=', ' _xmlns=')
    root = ET.fromstring(text)
    codebase = root.get('codebase', base).rstrip('/')

    jars, native_jars = [], []
    for res in root.iter('resources'):
        for el in res:
            href = el.get('href', '').strip()
            if not href:
                continue
            url = href if href.startswith('http') else f"{codebase}/{href.lstrip('/')}"
            if el.tag == 'nativelib':
                native_jars.append(url)
            elif el.tag == 'jar':
                jars.append(url)

    app_el = root.find('.//application-desc')
    main_class = app_el.get('main-class', '') if app_el is not None else ''
    arguments  = [a.text or '' for a in (app_el.findall('argument') if app_el is not None else [])]

    return jars, native_jars, main_class, arguments


# ── Public API ────────────────────────────────────────────────────────────────

def start_session(server: dict,
                  bmc_login_fn: Callable,
                  jnlp_fetch_fn: Callable) -> KvmSession:
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
        args=(sess, server, bmc_login_fn, jnlp_fetch_fn),
        daemon=True,
        name=f'kvm-{sid}',
    ).start()
    return sess


def get_session(session_id: str) -> Optional[KvmSession]:
    return _sessions.get(session_id)


def get_session_for_server(server_id: str) -> Optional[KvmSession]:
    for s in _sessions.values():
        if s.server_id == server_id and s.status in ('starting', 'running'):
            return s
    return None


def stop_session(session_id: str) -> None:
    with _lock:
        sess = _sessions.pop(session_id, None)
    if sess:
        _kill(sess)


# ── Session lifecycle ─────────────────────────────────────────────────────────

def _run(sess: KvmSession, server: dict, bmc_login_fn, jnlp_fetch_fn) -> None:
    try:
        _do_run(sess, server, bmc_login_fn, jnlp_fetch_fn)
    except Exception as e:
        sess.status = 'error'
        sess.error  = str(e)
        _kill(sess)
        _sessions.pop(sess.session_id, None)


def _do_run(sess: KvmSession, server: dict, bmc_login_fn, jnlp_fetch_fn) -> None:
    base   = f"https://{server['ip']}"
    tmpdir = Path(sess.tmpdir)
    disp   = f':{sess.display}'

    # 1 ── Xvfb
    xvfb = subprocess.Popen(
        ['Xvfb', disp, '-screen', '0', _RESOLUTION, '-ac', '+extension', 'GLX'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sess.procs.append(xvfb)
    time.sleep(0.8)
    if xvfb.poll() is not None:
        raise RuntimeError('Xvfb konnte nicht gestartet werden')

    # 2 ── x11vnc
    vnc = subprocess.Popen(
        ['x11vnc', '-display', disp,
         '-rfbport', str(sess.port_vnc),
         '-nopw', '-forever', '-shared', '-quiet', '-noxdamage', '-noipv6'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sess.procs.append(vnc)
    time.sleep(0.4)

    # 3 ── websockify (WebSocket → VNC TCP bridge)
    ws = subprocess.Popen(
        ['websockify', str(sess.port_ws), f'127.0.0.1:{sess.port_vnc}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sess.procs.append(ws)
    time.sleep(0.3)

    # 4 ── BMC login + JNLP download
    http_sess, sid = bmc_login_fn(base, server['username'], server['password'])
    resp = jnlp_fetch_fn(http_sess, base, sid)
    if not resp or '<jnlp' not in resp.text.lower():
        raise RuntimeError('Kein JNLP vom BMC erhalten — Login fehlgeschlagen?')

    # 5 ── Parse JNLP
    jars, native_jars, main_class, arguments = _parse_jnlp(resp.text, base)
    if not main_class:
        raise RuntimeError('Keine main-class im JNLP')

    # 6 ── Download JARs
    cp_paths = []
    for url in jars:
        name = url.split('/')[-1].split('?')[0] or 'ikvm.jar'
        dest = tmpdir / name
        r = http_sess.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        cp_paths.append(str(dest))

    if not cp_paths:
        raise RuntimeError('Keine JARs im JNLP gefunden')

    # 7 ── Download + extract native JARs (.so files)
    for url in native_jars:
        name = url.split('/')[-1].split('?')[0] or 'native.jar'
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

    # 8 ── Launch Java
    env = os.environ.copy()
    env['DISPLAY'] = disp

    cmd = [
        'java',
        f'-Djava.library.path={tmpdir}',
        '-Dawt.useSystemAAFontSettings=on',
        '-Dswing.aatext=true',
        # Allow AWT from headless JRE variants
        '-Djava.awt.headless=false',
        '-cp', ':'.join(cp_paths),
        main_class,
    ] + arguments

    java = subprocess.Popen(
        cmd, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(tmpdir),
    )
    sess.procs.append(java)
    sess.status = 'running'

    # Block until Java exits, then clean up automatically
    java.wait()
    stop_session(sess.session_id)


def _kill(sess: KvmSession) -> None:
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
