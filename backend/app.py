import asyncio as _asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import ipaddress
import concurrent.futures

import requests as req_lib
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask, jsonify, redirect, request, send_from_directory, Response

app = Flask(__name__)
CONFIG_PATH = os.environ.get('CONFIG_PATH', '/config/servers.json')
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'frontend')

# kvm.py sits next to app.py — add its directory to sys.path for direct import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import kvm as _kvm
    _KVM_AVAILABLE = True
except Exception:
    _kvm = None  # type: ignore
    _KVM_AVAILABLE = False


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"groups": []}
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)


def save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def find_server(config: dict, server_id: str):
    for group in config['groups']:
        for server in group['servers']:
            if server['id'] == server_id:
                return server, group
    return None, None


# ── IPMI helpers ────────────────────────────────────────────────────────────────

def check_bmc_reachable(ip: str, timeout: float = 1.5) -> bool:
    """Return True as soon as any BMC port (443/80/623) responds.

    All three ports are tried in parallel so unreachable IPs only add one
    timeout delay instead of three.
    """
    def _try(port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            ok = s.connect_ex((ip, port)) == 0
            s.close()
            return ok
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_try, p) for p in (443, 80, 623)]
        for f in concurrent.futures.as_completed(futs):
            if f.result():
                return True
    return False


def run_ipmitool(server: dict, *args, timeout: int = 15) -> tuple[bool, str, str]:
    cmd = [
        'ipmitool', '-I', 'lanplus',
        '-H', server['ip'],
        '-U', server['username'],
        '-P', server['password'],
        '-C', '3',
    ] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, '', 'Timeout'
    except FileNotFoundError:
        return False, '', 'ipmitool not found in container'
    except Exception as e:
        return False, '', str(e)


# ── Static frontend ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/<path:path>')
def static_files(path):
    full = os.path.join(FRONTEND_DIR, path)
    if os.path.isfile(full):
        return send_from_directory(FRONTEND_DIR, path)
    return send_from_directory(FRONTEND_DIR, 'index.html')


# ── Groups ────────────────────────────────────────────────────────────────────────

@app.route('/api/groups', methods=['GET'])
def api_get_groups():
    return jsonify(load_config()['groups'])


@app.route('/api/groups', methods=['POST'])
def api_create_group():
    name = (request.json or {}).get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name erforderlich'}), 400
    config = load_config()
    group = {'id': str(uuid.uuid4()), 'name': name, 'servers': []}
    config['groups'].append(group)
    save_config(config)
    return jsonify(group), 201


@app.route('/api/groups/<group_id>', methods=['PUT'])
def api_update_group(group_id):
    config = load_config()
    for g in config['groups']:
        if g['id'] == group_id:
            g['name'] = (request.json or {}).get('name', g['name']).strip()
            save_config(config)
            return jsonify(g)
    return jsonify({'error': 'Nicht gefunden'}), 404


@app.route('/api/groups/<group_id>', methods=['DELETE'])
def api_delete_group(group_id):
    config = load_config()
    config['groups'] = [g for g in config['groups'] if g['id'] != group_id]
    save_config(config)
    return '', 204


# ── Servers ───────────────────────────────────────────────────────────────────────

@app.route('/api/groups/<group_id>/servers', methods=['POST'])
def api_create_server(group_id):
    config = load_config()
    for g in config['groups']:
        if g['id'] == group_id:
            d = request.json or {}
            if not d.get('name') or not d.get('ip'):
                return jsonify({'error': 'Name und IP erforderlich'}), 400
            server = {
                'id': str(uuid.uuid4()),
                'name': d['name'].strip(),
                'ip': d['ip'].strip(),
                'username': d.get('username', 'ADMIN').strip(),
                'password': d.get('password', ''),
                'description': d.get('description', '').strip(),
            }
            g['servers'].append(server)
            save_config(config)
            return jsonify(server), 201
    return jsonify({'error': 'Gruppe nicht gefunden'}), 404


@app.route('/api/servers/<server_id>', methods=['PUT'])
def api_update_server(server_id):
    config = load_config()
    server, group = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404
    d = request.json or {}
    for key in ('name', 'ip', 'username', 'password', 'description'):
        if key in d:
            server[key] = d[key].strip() if isinstance(d[key], str) else d[key]
    new_group_id = d.get('group_id')
    if new_group_id and new_group_id != group['id']:
        target = next((g for g in config['groups'] if g['id'] == new_group_id), None)
        if target:
            group['servers'] = [s for s in group['servers'] if s['id'] != server_id]
            target['servers'].append(server)
    save_config(config)
    return jsonify(server)


@app.route('/api/servers/<server_id>', methods=['DELETE'])
def api_delete_server(server_id):
    config = load_config()
    for g in config['groups']:
        g['servers'] = [s for s in g['servers'] if s['id'] != server_id]
    save_config(config)
    return '', 204


@app.route('/api/scan/import', methods=['POST'])
def api_scan_import():
    config = load_config()
    ips = (request.json or {}).get('ips', [])
    if not ips:
        return jsonify({'error': 'Keine IPs angegeben'}), 400
    ungrouped = next((g for g in config['groups'] if g.get('ungrouped')), None)
    if not ungrouped:
        ungrouped = {'id': str(uuid.uuid4()), 'name': 'Ungruppiert', 'ungrouped': True, 'servers': []}
        config['groups'].append(ungrouped)
    existing_ips = {s['ip'] for g in config['groups'] for s in g['servers']}
    created = 0
    for ip in ips:
        if ip not in existing_ips:
            ungrouped['servers'].append({
                'id': str(uuid.uuid4()),
                'name': f"BMC-{ip.split('.')[-1]}",
                'ip': ip,
                'username': 'ADMIN',
                'password': '',
                'description': '',
            })
            created += 1
    save_config(config)
    return jsonify({'created': created, 'skipped': len(ips) - created, 'group_id': ungrouped['id']}), 201


# ── WebSocket RFB check ───────────────────────────────────────────────────────

def _check_ws_rfb(ip: str, cookies: dict, timeout: float = 4.0) -> bool:
    """Return True if BMC WebSocket at wss://IP:443/ sends RFB data within timeout.

    Some older ATEN firmware has the HTML5 bootstrap page and entry_value token
    but the WebSocket never initiates the RFB handshake.  This distinguishes those
    from firmware where the WebSocket KVM actually works.
    """
    import asyncio as _aio
    import ssl as _ssl_mod
    import threading as _threading

    try:
        import websockets as _ws_lib
    except ImportError:
        return False

    ssl_ctx = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl_mod.CERT_NONE
    cookie_hdr = '; '.join(f'{k}={v}' for k, v in cookies.items())

    async def _test() -> bool:
        try:
            async with _ws_lib.connect(
                f'wss://{ip}:443/',
                ssl=ssl_ctx,
                additional_headers={
                    'Origin': f'https://{ip}',
                    'Cookie': cookie_hdr,
                },
                ping_interval=None,
                open_timeout=timeout,
            ) as ws:
                data = await _aio.wait_for(ws.recv(), timeout=timeout)
                return b'RFB' in data
        except Exception:
            return False

    result: list[bool] = [False]

    def _run_in_thread():
        result[0] = _aio.run(_test())

    t = _threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    t.join(timeout=timeout + 3)
    return result[0]


# ── Capability probe ──────────────────────────────────────────────────────────

def _probe_server_caps(server: dict) -> dict:
    """Probe a server for supported features and return a caps dict."""
    caps = {
        'bmc_http':  False,
        'ipmi':      False,
        'kvm_aten':  False,   # ATEN HTML5 KVM via WebSocket proxy
        'ikvm_java': False,   # Java JNLP available
    }

    # 1. BMC reachable via HTTP(S)
    caps['bmc_http'] = check_bmc_reachable(server['ip'])

    # 2. IPMI (ipmitool lanplus)
    ok, _, _ = run_ipmitool(server, 'power', 'status', timeout=10)
    caps['ipmi'] = ok

    if not caps['bmc_http']:
        return caps

    base = f"https://{server['ip']}"

    # 3. Try BMC login — needed for HTML5 KVM and JNLP probes
    try:
        sess, sid = _bmc_login(base, server['username'], server['password'])
    except Exception:
        return caps

    if not sid:
        return caps

    # 4. ATEN HTML5 KVM: bootstrap page must contain entry_value AND WebSocket must
    #    send RFB data.  Some older ATEN firmware (e.g. B16NA) has the bootstrap page
    #    and entry_value but the WebSocket at wss://BMC:443/ never sends RFB data —
    #    in that case kvm_aten stays False even though the page loads.
    try:
        r = sess.get(
            f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm_html5_bootstrap",
            timeout=10,
        )
        if r.status_code == 200 and re.search(r'entry_value', r.text, re.I):
            bmc_cookies = {c.name: c.value for c in sess.cookies}
            caps['kvm_aten'] = _check_ws_rfb(server['ip'], bmc_cookies)
    except Exception:
        pass

    # 5. Java JNLP — try all static paths + navigation scrape
    if not caps['ikvm_java']:
        try:
            jnlp = _fetch_jnlp(sess, base, sid)
            caps['ikvm_java'] = bool(jnlp and '<jnlp' in jnlp.text.lower())
        except Exception:
            pass

    return caps


@app.route('/api/servers/<server_id>/probe', methods=['POST'])
def api_probe_server(server_id):
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404
    caps = _probe_server_caps(server)
    server['caps'] = caps
    save_config(config)
    return jsonify(caps)


# ── Status & Power ──────────────────────────────────────────────────────────────

@app.route('/api/servers/<server_id>/status', methods=['GET'])
def api_server_status(server_id):
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404

    online = check_bmc_reachable(server['ip'])
    power = 'unknown'
    if online:
        ok, out, _ = run_ipmitool(server, 'power', 'status', timeout=5)
        if ok:
            lower = out.lower()
            power = 'on' if 'on' in lower else 'off' if 'off' in lower else 'unknown'

    return jsonify({'id': server_id, 'online': online, 'power': power})


def _check_server_status(server: dict) -> dict:
    online = check_bmc_reachable(server['ip'])
    power = 'unknown'
    if online:
        ok, out, _ = run_ipmitool(server, 'power', 'status', timeout=5)
        if ok:
            lower = out.lower()
            power = 'on' if 'on' in lower else 'off' if 'off' in lower else 'unknown'
    return {'id': server['id'], 'online': online, 'power': power}


@app.route('/api/bulk-status', methods=['GET'])
def api_bulk_status():
    """Return status for all servers in one request, checked in parallel.

    Avoids the N×RTT overhead of individual /status calls when there are
    many servers (e.g. 100 entries).  Backend uses a thread pool so all
    TCP reachability checks and ipmitool calls run concurrently.
    """
    config = load_config()
    servers = [s for g in config['groups'] for s in g['servers']]
    if not servers:
        return jsonify({})
    workers = min(40, len(servers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_check_server_status, servers))
    return jsonify({r['id']: r for r in results})


POWER_ACTIONS = {
    'on':        ['power', 'on'],
    'soft':      ['power', 'soft'],
    'forceoff':  ['power', 'off'],
    'reset':     ['power', 'reset'],
    'cycle':     ['power', 'cycle'],
}


@app.route('/api/servers/<server_id>/power', methods=['POST'])
def api_power_action(server_id):
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404

    action = (request.json or {}).get('action', '')
    if action not in POWER_ACTIONS:
        return jsonify({'error': f'Ungültige Aktion: {action}'}), 400

    ok, out, err = run_ipmitool(server, *POWER_ACTIONS[action])
    return jsonify({'success': ok, 'output': out, 'error': err})


@app.route('/api/servers/<server_id>/console-url', methods=['GET'])
def api_console_url(server_id):
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404
    return jsonify({
        'jnlp_proxy': f"/api/servers/{server_id}/jnlp",
        'java_fix':   f"/api/servers/{server_id}/java-fix.ps1",
        'bmc_url':    f"https://{server['ip']}",
    })


_JAVA_FIX_TMPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'java-fix.ps1.tmpl')


@app.route('/api/servers/<server_id>/java-fix.ps1', methods=['GET'])
def api_java_fix(server_id):
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404

    ip   = server['ip']
    bmc  = f"https://{ip}"
    with open(_JAVA_FIX_TMPL, 'r', encoding='utf-8') as fh:
        script = (fh.read()
                  .replace('{SERVER_NAME}', server['name'])
                  .replace('{SERVER_IP}',   ip)
                  .replace('{BMC_URL}',     bmc))

    return Response(
        script,
        content_type='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="java-fix-{re.sub(r"[^a-zA-Z0-9]", "_", ip)}.ps1"',
        },
    )


# ── BMC session helpers ───────────────────────────────────────────────────────

# Static JNLP URL candidates (tried in order before mainmenu scraping)
_JNLP_PATHS = [
    # url_redirect.cgi variants
    '/cgi/url_redirect.cgi?url_name=ikvm',
    '/cgi/url_redirect.cgi?url_name=ikvm&url_type=jwsk',  # older ATEN (B16NA) firmware
    '/cgi/url_redirect.cgi?url_name=launch',       # matches downloaded filename "launch.jnlp"
    '/cgi/url_redirect.cgi?url_name=java_iview',
    '/cgi/url_redirect.cgi?url_name=iview',
    '/cgi/url_redirect.cgi?url_name=kvm',
    '/cgi/url_redirect.cgi?url_name=kvmjnlp',
    # Direct CGI endpoints seen in ATEN firmware
    '/cgi/CGI_GetJNLPContent.cgi',                 # ATEN firmware API
    '/cgi/getJNLP.cgi',
    '/cgi/ikvm.cgi',
    '/cgi/ikvm',
    '/cgi/launchKVM.cgi',
    # AMI MegaRAC / GoAhead (B04ND, Supermicro X8/X9 with AMI firmware)
    '/Java/jviewer.jnlp',
    '/Java/JViewer.jnlp',
    # Root-level JNLP (codebase in JNLP is root "/", file named "launch.jnlp")
    '/launch.jnlp',
    '/iKVM.jnlp',
    '/iKVM/iKVM.jnlp',
    '/iview.jnlp',
    '/kvm.jnlp',
]

# url_name values that are navigation frames/pages — skip as JNLP candidates
_MENU_NAMES = frozenset({
    'mainmenu', 'topmenu', 'index', 'login', 'logout', 'top', 'home',
    'sol', 'virtual_media', 'bmc_update', 'bios_update',
    'maintenance', 'network', 'user_management',
})


# Mimic a real browser so ATEN's web server doesn't reject python-requests UA
_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}


def _bmc_login(base: str, username: str, password: str) -> tuple[req_lib.Session, str | None]:
    """Authenticate against the BMC and return (session, sid_or_None).

    Handles two BMC generations:
    - Legacy ATEN (X8/X9): login.cgi returns 200 with SID embedded in JavaScript
    - AMI MegaRAC (X10/X11/X12+): login.cgi sets SID as an HTTP cookie

    After login, simulates browser navigation (mainmenu → topmenu) so that
    ATEN's session-state checks pass when the JNLP URL is later requested.
    """
    sess = req_lib.Session()
    sess.verify = False
    sess.headers.update(_BROWSER_HEADERS)

    try:
        r = sess.post(
            f"{base}/cgi/login.cgi",
            data={'name': username, 'pwd': password},
            timeout=10,
            allow_redirects=False,
        )
    except Exception:
        return sess, None

    # AMI path: cookie on the 302 response
    sid = _pick_sid(r.cookies) or _pick_sid(sess.cookies)

    if not sid:
        loc = r.headers.get('Location', '')
        if loc and r.status_code in (301, 302, 303, 307, 308):
            target = loc if loc.startswith('http') else f"{base}{loc}"
            try:
                r2 = sess.get(target, timeout=10, allow_redirects=True)
                sid = _pick_sid(sess.cookies) or _pick_sid(r2.cookies)
                if not sid:
                    sid = _extract_sid_from_body(r2.text)
            except Exception:
                pass

    # ATEN path on 200: SID in the JS body of login.cgi itself
    if not sid and r.status_code == 200:
        sid = _extract_sid_from_body(r.text)

    if sid:
        sess.cookies.set('SID', sid)
        # Simulate browser navigation: ATEN's url_redirect.cgi?url_name=ikvm
        # checks internal session state that is only set after visiting topmenu.
        _bmc_navigate_to_kvm(sess, base)
        return sess, sid

    # ATEN login yielded no SID — try GoAhead (AMI MegaRAC) firmware
    sid = _bmc_login_goahead(sess, base, username, password)
    if sid:
        sess.cookies.set('SID', sid)
        sess.cookies.set('SessionCookie', sid)  # GoAhead uses this cookie name
    return sess, sid


def _bmc_navigate_to_kvm(sess: req_lib.Session, base: str) -> None:
    """Simulate the browser navigation path to the iKVM console page.

    Full browser flow (from man_ikvm page source analysis):
      1. mainmenu  → erases navigation cookies, loads topmenu frame
      2. topmenu   → renders navigation bar
      3. Set mainpage/subpage cookies (what page_mapping('remote','man_ikvm') does)
      4. man_ikvm  → Console Redirection page; onload calls pollServer()
      5. POST upgrade_process.cgi fwtype=255  → pollServer() — checks iKVM service
         available; only after this POST does url_name=ikvm return the JNLP.
      6. POST upgrade_process.cgi fwtype=255 again — browser does a second poll
         on button click before actually navigating to the JNLP URL.
    """
    topmenu_ref  = f"{base}/cgi/url_redirect.cgi?url_name=topmenu"
    man_ikvm_url = f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm"
    poll_url     = f"{base}/cgi/upgrade_process.cgi"

    try:
        sess.get(f"{base}/cgi/url_redirect.cgi?url_name=mainmenu", timeout=5)
    except Exception:
        pass
    try:
        sess.get(topmenu_ref, timeout=8)
    except Exception:
        pass

    # Set the navigation cookies that page_mapping('remote', 'man_ikvm') would set
    sess.cookies.set('mainpage', 'remote')
    sess.cookies.set('subpage',  'man_ikvm')

    try:
        sess.get(man_ikvm_url, timeout=10, headers={'Referer': topmenu_ref})
    except Exception:
        pass

    # Mirror the two pollServer() calls the browser makes.
    # POST fwtype=255 to upgrade_process.cgi checks/starts the iKVM service.
    # The BMC only serves url_name=ikvm after at least one successful poll.
    poll_headers = {
        'Referer':      man_ikvm_url,
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    for _ in range(2):
        try:
            sess.post(poll_url, data='fwtype=255', headers=poll_headers, timeout=8)
        except Exception:
            pass


def _pick_sid(cookies) -> str | None:
    for name in ('SID', 'sid', 'QSESSIONID'):
        v = cookies.get(name)
        if v:
            return v
    return None


def _extract_sid_from_body(text: str) -> str | None:
    """Extract SID from JavaScript responses (legacy ATEN BMC)."""
    patterns = [
        r'[?&]SID=([a-fA-F0-9]+)',                        # URL parameter
        r'[Ss][Ii][Dd]\s*[=:]\s*[\'"]([a-fA-F0-9]{8,})', # var SID = '...'
        r'SID[,\s]*([a-fA-F0-9]{16,})',                   # bare assignment
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _bmc_login_goahead(sess: req_lib.Session, base: str, username: str, password: str) -> str | None:
    """AMI MegaRAC / GoAhead BMC login via /rpc/WEBSES/create.asp.

    Returns the SESSION_COOKIE token, or None on failure.
    """
    try:
        r = sess.post(
            f"{base}/rpc/WEBSES/create.asp",
            data={'WEBVAR_USERNAME': username, 'WEBVAR_PASSWORD': password},
            timeout=10,
        )
        m = re.search(r"""['"]SESSION_COOKIE['"]\s*:\s*['"]([^'"]+)['"]""", r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _read_response_body(r: req_lib.Response) -> bytes:
    """Read response body, tolerating servers that lie about Content-Length.

    GoAhead (AMI MegaRAC) sends Content-Length: 3757 but closes after ~2013 bytes.
    Setting fp.length=None on the underlying http.client response bypasses the check.
    """
    fp = getattr(r.raw, '_fp', None)
    if fp is not None:
        orig_len = getattr(fp, 'length', None)
        try:
            fp.length = None
            body = fp.read()
            return body if body else b''
        except Exception as exc:
            partial = getattr(exc, 'partial', b'')
            return partial if partial else b''
        finally:
            try:
                fp.length = orig_len
            except Exception:
                pass
    body = b''
    try:
        for chunk in r.iter_content(8192):
            body += chunk
    except Exception:
        pass
    return body


def _fetch_jnlp(sess: req_lib.Session, base: str, sid: str | None) -> req_lib.Response | None:
    """Try all known JNLP paths, then scrape navigation pages for firmware-specific URLs."""
    # Referer mimics the browser being on the Console Redirection page (man_ikvm),
    # which is the last page visited by _bmc_navigate_to_kvm before the JNLP fetch.
    jnlp_headers = {'Referer': f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm"}

    candidates = []
    for path in _JNLP_PATHS:
        candidates.append(f"{base}{path}")
        if sid:
            sep = '&' if '?' in path else '?'
            candidates.append(f"{base}{path}{sep}SID={sid}")

    for url in candidates:
        try:
            r = sess.get(url, timeout=8, headers=jnlp_headers, stream=True)
            body = _read_response_body(r)
            r._content = body
            if b'<jnlp' in body.lower():
                return r
        except Exception:
            continue

    # Static paths exhausted — scrape navigation pages for firmware-specific KVM links
    return _fetch_jnlp_from_mainmenu(sess, base, sid)


def _fetch_jnlp_from_mainmenu(sess: req_lib.Session, base: str, sid: str | None) -> req_lib.Response | None:
    """Scan multiple BMC navigation pages for JNLP links or KVM url_name values.

    ATEN framesets use mainmenu → topmenu → section pages. We scan all of them
    to discover firmware-specific url_name values for the KVM/iKVM feature.
    """
    # Pages to scan for KVM links (frameset pages that reference section URLs)
    scan_pages = [
        '/cgi/url_redirect.cgi?url_name=mainmenu',
        '/cgi/url_redirect.cgi?url_name=topmenu',
        '/cgi/url_redirect.cgi?url_name=remote_control',
        '/cgi/url_redirect.cgi?url_name=remote_ctrl',
    ]

    all_url_names: set[str] = set()

    for rel_path in scan_pages:
        try:
            r = sess.get(f"{base}{rel_path}", timeout=10)
        except Exception:
            continue
        text = r.text

        # Direct .jnlp href/src/location references
        for m in re.finditer(r'(?:href|src|action|location)\s*[=:]\s*["\']([^"\']+\.jnlp[^"\']*)["\']', text, re.I):
            url = m.group(1)
            if not url.startswith('http'):
                url = f"{base}/{url.lstrip('/')}"
            try:
                resp = sess.get(url, timeout=8)
                if '<jnlp' in resp.text.lower():
                    return resp
            except Exception:
                continue

        # Collect all url_name= values from JS/HTML
        for m in re.finditer(r'url_name=([a-zA-Z_0-9]+)', text):
            all_url_names.add(m.group(1).lower())

    # Remove known navigation/menu names, sort KVM-like names first
    candidates = sorted(
        [n for n in all_url_names if n not in _MENU_NAMES],
        key=lambda n: 0 if any(k in n for k in ('kvm', 'ikvm', 'iview', 'java', 'remote', 'console', 'virtual')) else 1,
    )

    for name in candidates:
        for url in ([f"{base}/cgi/url_redirect.cgi?url_name={name}"] +
                    ([f"{base}/cgi/url_redirect.cgi?url_name={name}&SID={sid}"] if sid else [])):
            try:
                resp = sess.get(url, timeout=8)
                if '<jnlp' in resp.text.lower():
                    return resp
            except Exception:
                continue

    return None


# ── Java KVM TCP proxy ────────────────────────────────────────────────────────
# Each Java iKVM session gets a dedicated TCP proxy port (6090-6099).
# The JNLP codebase and connection args are rewritten to point to the Docker
# host, so the client never needs direct network access to the BMC.

_JAVA_KVM_PORTS = range(6090, 6100)
_java_kvm_sessions: dict[int, dict] = {}   # local_port → session info
_java_kvm_lock = threading.Lock()
_jnlp_codebase_cache: dict[str, str] = {}  # server_id → original codebase URL


def _start_java_tcp_proxy(local_port: int, remote_ip: str, remote_port: int) -> threading.Event:
    """Start a raw TCP proxy in a daemon thread. Returns stop_event."""
    stop_ev = threading.Event()

    def _run():
        async def _handle(reader, writer):
            try:
                rr, rw = await _asyncio.wait_for(
                    _asyncio.open_connection(remote_ip, remote_port), timeout=10
                )
            except Exception:
                try: writer.close()
                except Exception: pass
                return

            async def _fwd(src, dst):
                try:
                    while not stop_ev.is_set():
                        try:
                            data = await _asyncio.wait_for(src.read(65536), timeout=2.0)
                        except _asyncio.TimeoutError:
                            continue
                        if not data:
                            break
                        dst.write(data)
                        await dst.drain()
                except Exception:
                    pass
                finally:
                    try: dst.close()
                    except Exception: pass

            await _asyncio.gather(_fwd(reader, rw), _fwd(rr, writer), return_exceptions=True)

        async def _serve():
            srv = await _asyncio.start_server(_handle, '0.0.0.0', local_port)
            async with srv:
                while not stop_ev.is_set():
                    await _asyncio.sleep(0.3)

        _asyncio.run(_serve())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.3)  # let the asyncio server bind before we return
    return stop_ev


def _java_kvm_alloc(bmc_ip: str, bmc_port: int, server_id: str) -> int | None:
    """Allocate a proxy port, start TCP proxy, return local port (or None if full)."""
    with _java_kvm_lock:
        now = time.time()
        for p in list(_java_kvm_sessions):
            if _java_kvm_sessions[p]['expires'] < now:
                _java_kvm_sessions[p]['stop_ev'].set()
                del _java_kvm_sessions[p]
        port = next((p for p in _JAVA_KVM_PORTS if p not in _java_kvm_sessions), None)
        if port is None:
            return None
        stop_ev = _start_java_tcp_proxy(port, bmc_ip, bmc_port)
        _java_kvm_sessions[port] = {
            'stop_ev':   stop_ev,
            'bmc_ip':    bmc_ip,
            'bmc_port':  bmc_port,
            'server_id': server_id,
            'expires':   now + 3600,
        }
        return port


def _rewrite_jnlp(jnlp_bytes: bytes, bmc_ip: str, server_id: str,
                   proxy_host: str, proxy_port: int, app_base: str) -> bytes:
    """Rewrite JNLP so the client connects through the Docker host:
    - codebase  → /api/servers/<id>/jnlp-res  (JAR proxy)
    - first IP <argument>  → proxy_host
    - port <argument> directly after IP  → proxy_port
    """
    text = jnlp_bytes.decode('utf-8', errors='replace')

    # Cache and rewrite codebase attribute
    m_cb = re.search(r'codebase=["\']([^"\']+)["\']', text, re.I)
    if m_cb:
        _jnlp_codebase_cache[server_id] = m_cb.group(1)
        text = (text[:m_cb.start()] +
                f'codebase="{app_base}/api/servers/{server_id}/jnlp-res"' +
                text[m_cb.end():])

    # Rewrite BMC IP argument → proxy_host
    text = text.replace(f'<argument>{bmc_ip}</argument>',
                        f'<argument>{proxy_host}</argument>', 1)

    # Rewrite the port argument immediately following the (rewritten) IP
    text = re.sub(
        rf'(<argument>{re.escape(proxy_host)}</argument>\s*<argument>)(\d{{4,5}})(</argument>)',
        lambda m: m.group(1) + str(proxy_port) + m.group(3),
        text,
        count=1,
    )

    return text.encode('utf-8')


# ── Console endpoints ─────────────────────────────────────────────────────────

@app.route('/api/servers/<server_id>/jnlp', methods=['GET'])
def api_console_jnlp(server_id):
    """Login to BMC server-side, rewrite JNLP to proxy through Docker host, return as download.

    The client never needs direct network access to the BMC:
    - JAR/native-lib downloads are proxied via /api/servers/<id>/jnlp-res/<path>
    - The KVM TCP connection is forwarded on a port from the range 6090–6099
    Works for both ATEN (url_redirect.cgi) and AMI MegaRAC / GoAhead firmwares.
    """
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Server nicht gefunden'}), 404

    bmc_ip   = server['ip']
    base     = f"https://{bmc_ip}"
    ip_safe  = re.sub(r'[^a-zA-Z0-9]', '_', bmc_ip)

    # Docker host address the client uses to reach us
    proxy_host = request.host.split(':')[0]
    scheme     = 'https' if request.is_secure else 'http'
    app_base   = f"{scheme}://{request.host}"

    # Login (handles ATEN and GoAhead automatically)
    sess, sid = _bmc_login(base, server['username'], server['password'])
    if not sid:
        return jsonify({'error': 'BMC-Anmeldung fehlgeschlagen'}), 503

    # Fetch JNLP from BMC (server-side; same session IP → ATEN IP-check passes)
    jnlp_r = _fetch_jnlp(sess, base, sid)
    if not jnlp_r or not jnlp_r.content or b'<jnlp' not in jnlp_r.content.lower():
        return jsonify({'error': 'JNLP nicht gefunden oder ungültig'}), 404

    jnlp_text = jnlp_r.content.decode('utf-8', errors='replace')

    # Find the KVM port: first numeric argument directly after the BMC IP argument
    m_port = re.search(
        rf'<argument>{re.escape(bmc_ip)}</argument>\s*<argument>(\d{{4,5}})</argument>',
        jnlp_text,
    )
    kvm_port = int(m_port.group(1)) if m_port else 5901

    # Allocate proxy port and start TCP forwarder
    proxy_port = _java_kvm_alloc(bmc_ip, kvm_port, server_id)
    if proxy_port is None:
        return jsonify({'error': 'Alle Java-KVM-Ports belegt (max 10 parallele Sitzungen)'}), 503

    # Rewrite JNLP (codebase + IP + KVM port)
    jnlp_out = _rewrite_jnlp(jnlp_r.content, bmc_ip, server_id,
                               proxy_host, proxy_port, app_base)

    return Response(
        jnlp_out,
        content_type='application/x-java-jnlp-file',
        headers={'Content-Disposition': f'attachment; filename="jviewer-{ip_safe}.jnlp"'},
    )


@app.route('/api/servers/<server_id>/jnlp-res', defaults={'res_path': ''}, methods=['GET'])
@app.route('/api/servers/<server_id>/jnlp-res/<path:res_path>', methods=['GET'])
def api_jnlp_resource(server_id, res_path):
    """Proxy JNLP resources (JARs, native libs) from BMC to browser.

    Java Web Start downloads resources relative to the rewritten codebase URL.
    We forward them from the original BMC codebase so the client never needs
    direct network access to the BMC.
    """
    codebase = _jnlp_codebase_cache.get(server_id)
    if not codebase:
        config = load_config()
        srv, _ = find_server(config, server_id)
        if not srv:
            return jsonify({'error': 'Nicht gefunden'}), 404
        codebase = f"https://{srv['ip']}/Java"  # AMI MegaRAC default

    url = codebase.rstrip('/') + ('/' + res_path.lstrip('/') if res_path else '')
    try:
        r = req_lib.get(url, verify=False, stream=True, timeout=60)
        ct = r.headers.get('Content-Type', 'application/octet-stream')
        return Response(r.iter_content(65536), status=r.status_code, content_type=ct)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502


@app.route('/api/servers/<server_id>/html5-kvm', methods=['GET'])
def api_html5_kvm(server_id):
    """Relay page: logs the browser into the BMC via a popup, then navigates to
    the BMC's built-in HTML5 KVM viewer (man_ikvm_html5). No Java required.
    Designed to run inside an iframe (kvmModal) or as a standalone tab.
    Sends window.postMessage({type:'kvm-ready'}) to the parent before navigating,
    so the parent modal can update its status badge.
    """
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404

    import html as html_mod
    ip        = html_mod.escape(server['ip'])
    username  = html_mod.escape(server['username'])
    password  = html_mod.escape(server['password'])
    bmc_base  = f"https://{server['ip']}"
    login_url = f"{bmc_base}/cgi/login.cgi"
    # man_ikvm_html5 redirects to man_ikvm_html5_bootstrap on this firmware;
    # use the bootstrap URL directly to avoid an extra redirect hop.
    html5_url = f"{bmc_base}/cgi/url_redirect.cgi?url_name=man_ikvm_html5_bootstrap"

    page = f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <title>HTML5 KVM – {ip}</title>
  <style>
    body {{ font-family:sans-serif; background:#0d1117; color:#e6edf3;
            display:flex; flex-direction:column; align-items:center;
            justify-content:center; min-height:100vh; margin:0; padding:1rem;
            text-align:center; }}
    h2  {{ margin-bottom:.5rem; }}
    p   {{ color:#8b949e; margin:.4rem 0; font-size:.95rem; }}
    .btn {{ display:inline-block; padding:.55rem 1.4rem; background:#1f6feb;
             color:#fff; border:none; border-radius:6px; cursor:pointer;
             font-size:1rem; text-decoration:none; margin:.4rem .2rem; }}
    .btn:hover {{ background:#388bfd; }}
    .btn-sec {{ background:#30363d; color:#e6edf3; }}
    .btn-sec:hover {{ background:#484f58; }}
    .spinner {{ display:inline-block; width:1rem; height:1rem;
                border:3px solid #30363d; border-top-color:#0dcaf0;
                border-radius:50%; animation:spin .8s linear infinite;
                vertical-align:middle; margin-right:.4rem; }}
    @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
    #step-error {{ display:none; color:#f85149; }}
    #step-retry {{ display:none; }}
  </style>
</head>
<body>
  <form id="lf" action="{login_url}" method="post">
    <input type="hidden" name="name" value="{username}">
    <input type="hidden" name="pwd"  value="{password}">
  </form>

  <div id="step-working">
    <h2>HTML5 KVM — {ip}</h2>
    <p><span class="spinner"></span><span id="status-msg">Anmeldung am BMC…</span></p>
  </div>

  <div id="step-retry">
    <h2>HTML5 KVM — {ip}</h2>
    <p>Popup-Fenster wurde vom Browser blockiert.</p>
    <button class="btn" onclick="startLogin()">&#128273; Anmelden &amp; KVM öffnen</button>
    <br>
    <a href="{bmc_base}" target="_blank" class="btn btn-sec" style="margin-top:.6rem">BMC direkt öffnen</a>
  </div>

  <div id="step-error">
    <h2>Fehler</h2>
    <p id="error-msg">Anmeldung fehlgeschlagen oder Timeout.</p>
    <button class="btn" onclick="startLogin()">Erneut versuchen</button>
    <a href="{bmc_base}" target="_blank" class="btn btn-sec">BMC direkt öffnen</a>
  </div>

  <script>
  var CONSOLE_URL = '{html5_url}';
  var pollTimer   = null;
  var timeoutId   = null;
  var loginPopup  = null;
  var inIframe    = (window.self !== window.top);

  function startLogin() {{
    document.getElementById('step-retry').style.display   = 'none';
    document.getElementById('step-error').style.display   = 'none';
    document.getElementById('step-working').style.display = 'block';
    document.getElementById('status-msg').textContent = 'Anmeldung am BMC…';

    loginPopup = window.open(
      'about:blank', 'bmc_login_popup',
      'width=1,height=1,left=-200,top=-200,menubar=no,toolbar=no,status=no,scrollbars=no'
    );

    if (!loginPopup) {{
      document.getElementById('step-working').style.display = 'none';
      document.getElementById('step-retry').style.display   = 'block';
      return;
    }}

    var form = document.getElementById('lf');
    form.setAttribute('target', 'bmc_login_popup');
    form.submit();

    pollTimer = setInterval(checkPopup, 150);
    timeoutId = setTimeout(onTimeout, 15000);
  }}

  function checkPopup() {{
    if (!loginPopup || loginPopup.closed) {{
      clearInterval(pollTimer); pollTimer = null;
      return;
    }}
    try {{
      var _unused = loginPopup.location.href;
    }} catch (e) {{
      // SecurityError: popup navigated to BMC domain — login cookie is set
      clearInterval(pollTimer); pollTimer = null;
      clearTimeout(timeoutId);
      setTimeout(function() {{
        try {{ loginPopup.close(); }} catch (_) {{}}
        document.getElementById('status-msg').textContent = 'Öffne HTML5 KVM…';
        // Notify parent frame (kvmModal) before we navigate away
        if (inIframe) {{
          try {{ window.parent.postMessage({{type:'kvm-ready',ip:'{ip}'}}, '*'); }} catch (_) {{}}
        }}
        window.location.href = CONSOLE_URL;
      }}, 300);
    }}
  }}

  function onTimeout() {{
    clearInterval(pollTimer); pollTimer = null;
    if (loginPopup && !loginPopup.closed) {{ try {{ loginPopup.close(); }} catch(_) {{}} }}
    document.getElementById('step-working').style.display = 'none';
    document.getElementById('step-error').style.display   = 'block';
    document.getElementById('error-msg').textContent =
      'Timeout — BMC nicht erreichbar oder falsche Zugangsdaten.';
    if (inIframe) {{
      try {{ window.parent.postMessage({{type:'kvm-error',ip:'{ip}'}}, '*'); }} catch (_) {{}}
    }}
  }}

  startLogin();
  </script>
</body>
</html>"""
    return Response(page, content_type='text/html')


@app.route('/api/servers/<server_id>/playwright-debug', methods=['GET'])
def api_playwright_debug(server_id):
    """Run Playwright and return a step-by-step report of what the BMC returns."""
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Server nicht gefunden'}), 404

    base     = f"https://{server['ip']}"
    username = server['username']
    password = server['password']
    steps    = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return jsonify({'error': 'Playwright nicht installiert'}), 500

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'],
            )
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()

            # Step 1: Login
            try:
                r1 = page.goto(f"{base}/cgi/login.cgi",
                               wait_until='domcontentloaded', timeout=15_000)
                steps.append({'step':'1_load_login','url':r1.url,'status':r1.status,
                               'cookies': [c['name']+'='+c['value'] for c in ctx.cookies()]})
                page.locator('input[name="name"], input[name="username"]').first.fill(username, timeout=3_000)
                page.locator('input[name="pwd"], input[name="password"]').first.fill(password, timeout=3_000)
                # ATEN login button: id=login_word, onclick="checkform(this)" — NOT type=submit
                page.locator(
                    '#login_word, input[onclick*="checkform" i], '
                    'input[value*="login" i], input[type="submit"], button[type="submit"]'
                ).first.click(timeout=5_000)
                page.wait_for_load_state('networkidle', timeout=12_000)
                steps.append({'step':'2_after_login','url':page.url,
                               'cookies': [c['name']+'='+c['value'] for c in ctx.cookies()]})
            except Exception as e:
                steps.append({'step':'login_error','error':str(e)})

            # Step 2b: set nav cookies + inject SID into window before man_ikvm JS runs
            bmc_host = server['ip']
            sid_val = next((c['value'] for c in ctx.cookies() if c['name'] == 'SID'), '')
            for cname, cval in [('mainpage','remote'),('subpage','man_ikvm')]:
                ctx.add_cookies([{'name':cname,'value':cval,'domain':bmc_host,'path':'/'}])
            # CRITICAL: ATEN's utils.js uses top.SID in XHR calls.
            # When man_ikvm loads directly (no frameset), top.SID is undefined and
            # GETPORTSINFO returns a redirect.  Inject SID before any page script runs.
            ctx.add_init_script(
                f"window.SID = '{sid_val}'; "
                f"window.lang_setting = window.lang_setting || 'English';"
            )
            steps.append({'step':'2b_nav_cookies_set',
                          'injected_SID': sid_val,
                          'cookies': [c['name']+'='+c['value'] for c in ctx.cookies()]})

            # Step 3: load man_ikvm (JS: pollServer → GetJNLPRequest)
            try:
                r2 = page.goto(f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm",
                               wait_until='domcontentloaded', timeout=15_000)
                page.wait_for_timeout(3_500)   # wait for pollServer() JS
                steps.append({'step':'3_man_ikvm','url':r2.url,'status':r2.status,
                               'cookies': [c['name']+'='+c['value'] for c in ctx.cookies()],
                               'page_title': page.title()})
            except Exception as e:
                steps.append({'step':'man_ikvm_error','error':str(e)})

            # Step 4: POST GETPORTSINFO.XML via browser fetch (mirrors pollServer())
            try:
                result = page.evaluate(f"""async () => {{
                    const r = await fetch({(base+'/cgi/ipmi.cgi')!r}, {{
                        method: 'POST',
                        credentials: 'include',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: 'op=GETPORTSINFO.XML&r=(0,0)'
                    }});
                    const t = await r.text();
                    return {{status: r.status, body: t.substring(0,500)}};
                }}""")
                steps.append({'step':'4_GETPORTSINFO', **result})
            except Exception as e:
                steps.append({'step':'4_GETPORTSINFO_error','error':str(e)})

            # Step 5: fetch url_name=ikvm via browser fetch() WITH correct cookies
            jnlp_url = f"{base}/cgi/url_redirect.cgi?url_name=ikvm"
            try:
                result = page.evaluate(f"""async () => {{
                    const r = await fetch({jnlp_url!r}, {{credentials:'include'}});
                    const text = await r.text();
                    return {{status: r.status, ct: r.headers.get('content-type'), body: text.substring(0,600)}};
                }}""")
                steps.append({'step':'5_fetch_ikvm', **result,
                              'is_jnlp': '<jnlp' in (result.get('body') or '').lower()})
            except Exception as e:
                steps.append({'step':'5_fetch_ikvm_error','error':str(e)})

            # Step 6: list buttons on current page
            try:
                btn_info = page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('input[type=button],input[type=submit],button'));
                    return btns.map(b => ({id:b.id, value:b.value||b.textContent.trim(),
                                           onclick:b.getAttribute('onclick')||'',
                                           disabled: b.disabled}));
                }""")
                steps.append({'step':'6_buttons_on_man_ikvm', 'buttons': btn_info[:10]})
            except Exception as e:
                steps.append({'step':'6_buttons_error','error':str(e)})

            # Step 7: click Launch button then capture ikvm response
            try:
                el = page.locator('#Launch, input[value*="Launch" i], input[onclick*="JNLP" i]').first
                if el.count() > 0:
                    el.click(timeout=3_000)
                    page.wait_for_timeout(2_000)
                    # After click: fetch ikvm again
                    result2 = page.evaluate(f"""async () => {{
                        const r = await fetch({jnlp_url!r}, {{credentials:'include'}});
                        const text = await r.text();
                        return {{status: r.status, ct: r.headers.get('content-type'), body: text.substring(0,600)}};
                    }}""")
                    steps.append({'step':'7_after_click_fetch_ikvm', **result2,
                                  'is_jnlp': '<jnlp' in (result2.get('body') or '').lower()})
                else:
                    steps.append({'step':'7_no_launch_button'})
            except Exception as e:
                steps.append({'step':'7_click_error','error':str(e)})

            # Step 8: page.goto() directly to ikvm
            try:
                r3 = page.goto(jnlp_url, wait_until='commit', timeout=8_000)
                body = r3.body().decode('utf-8', errors='replace') if r3 else ''
                steps.append({'step':'8_goto_ikvm',
                               'status': r3.status if r3 else None,
                               'ct': r3.headers.get('content-type','') if r3 else '',
                               'body_len': len(body),
                               'body_preview': body[:600],
                               'is_jnlp': '<jnlp' in body.lower()})
            except Exception as e:
                steps.append({'step':'8_goto_ikvm_error','error':str(e)})

            browser.close()

    except Exception as e:
        steps.append({'step':'playwright_crash','error':str(e)})

    import html as html_mod
    rows = ''
    for s in steps:
        rows += f'<tr><th colspan="2" style="background:#21262d;padding:8px">{html_mod.escape(str(s.get("step","")))}</th></tr>'
        for k, v in s.items():
            if k == 'step': continue
            rows += (f'<tr><td style="color:#8b949e;padding:4px 8px;white-space:nowrap">{html_mod.escape(str(k))}</td>'
                     f'<td style="padding:4px 8px"><pre style="margin:0;white-space:pre-wrap;word-break:break-all">'
                     f'{html_mod.escape(str(v))}</pre></td></tr>')
    page_html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Playwright Debug</title>
<style>body{{font-family:monospace;background:#0d1117;color:#e6edf3;padding:1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #30363d;vertical-align:top}}
pre{{background:#161b22;padding:6px;border-radius:4px;font-size:.8rem}}</style></head>
<body><h2>Playwright Debug: {html_mod.escape(server['ip'])}</h2>
<table>{rows}</table></body></html>"""
    return Response(page_html, content_type='text/html')


@app.route('/api/servers/<server_id>/jnlp-debug', methods=['GET'])
def api_jnlp_debug(server_id):
    """Diagnostic endpoint — shows exactly what the BMC returns during login + JNLP fetch."""
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Server nicht gefunden'}), 404

    base = f"https://{server['ip']}"
    steps = []

    sess = req_lib.Session()
    sess.verify = False

    # Step 1: raw login POST (allow_redirects=False to see 302 details)
    try:
        r = sess.post(
            f"{base}/cgi/login.cgi",
            data={'name': server['username'], 'pwd': server['password']},
            timeout=10,
            allow_redirects=False,
        )
        steps.append({
            'step': '1_login_post',
            'status': r.status_code,
            'location': r.headers.get('Location', ''),
            'set_cookie': r.headers.get('Set-Cookie', ''),
            'cookies_after': dict(sess.cookies),
            'body_preview': r.text[:600],
            'sid_in_body': _extract_sid_from_body(r.text),
        })
        # follow redirect if present
        loc = r.headers.get('Location', '')
        if loc and r.status_code in (301, 302, 303, 307, 308):
            target = loc if loc.startswith('http') else f"{base}{loc}"
            r2 = sess.get(target, timeout=10, allow_redirects=True)
            steps.append({
                'step': '2_login_redirect',
                'url': target,
                'status': r2.status_code,
                'cookies_after': dict(sess.cookies),
                'body_preview': r2.text[:400],
                'sid_in_body': _extract_sid_from_body(r2.text),
            })
    except Exception as e:
        steps.append({'step': '1_login_post', 'error': str(e)})

    # Step 2: try all static JNLP paths (skip duplicates)
    sid = _pick_sid(sess.cookies)
    seen_urls: set[str] = set()
    for path in _JNLP_PATHS:
        for suffix in ([''] if not sid else ['', f'{"&" if "?" in path else "?"}SID={sid}']):
            url = f"{base}{path}{suffix}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                r = sess.get(url, timeout=8)
                steps.append({
                    'step': 'jnlp_try',
                    'url': url,
                    'status': r.status_code,
                    'content_type': r.headers.get('Content-Type', ''),
                    'body_length': len(r.content),
                    'is_jnlp': '<jnlp' in r.text.lower(),
                    'body_preview': r.text[:400],
                })
            except Exception as e:
                steps.append({'step': 'jnlp_try', 'url': url, 'error': str(e)})

    # Step 3+: scan navigation pages
    for label, url_name, body_limit in [
        ('3_mainmenu',       'mainmenu',       800),
        ('4_topmenu',        'topmenu',        8000),   # need full JS to find KVM cookie values
        ('5_man_ikvm',       'man_ikvm',       5000),   # Console Redirection page — calls GetIKVMStatus()
        ('6_remote_control', 'remote_control', 2000),
        ('7_remote_ctrl',    'remote_ctrl',    2000),
    ]:
        try:
            pg = sess.get(f"{base}/cgi/url_redirect.cgi?url_name={url_name}", timeout=10)
            names = sorted({m.group(1) for m in re.finditer(r'url_name=([a-zA-Z_0-9]+)', pg.text)})
            jnlp_h = re.findall(r'(?:href|src|open|location)[^"\']*["\']([^"\']*\.jnlp[^"\']*)["\']', pg.text, re.I)
            # Also extract window.open() and href calls that might contain the KVM URL
            js_opens = re.findall(r'(?:window\.open|location\.href|location\s*=)\s*\(\s*["\']([^"\']+)["\']', pg.text, re.I)
            steps.append({
                'step': label,
                'url': f"/cgi/url_redirect.cgi?url_name={url_name}",
                'status': pg.status_code,
                'body_length': len(pg.content),
                'url_names_found': str(names),
                'jnlp_hrefs_found': str(jnlp_h),
                'js_opens': str(js_opens),
                'body_preview': pg.text[:body_limit],
            })
        except Exception as e:
            steps.append({'step': label, 'error': str(e)})

    # Step 5b: POST upgrade_process.cgi fwtype=255 — mirrors browser's pollServer()
    # man_ikvm page calls this before serving the JNLP URL
    try:
        pg = sess.post(
            f"{base}/cgi/upgrade_process.cgi",
            data='fwtype=255',
            headers={
                'Referer':      f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm",
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=8,
        )
        steps.append({
            'step': '5b_upgrade_process_poll',
            'status': pg.status_code,
            'body_length': len(pg.content),
            'body_preview': pg.text[:800],
        })
    except Exception as e:
        steps.append({'step': '5b_upgrade_process_poll', 'error': str(e)})

    # Step 5c: set navigation cookies (as JS page_mapping would), then re-fetch
    # man_ikvm — hypothesis: firmware appends JNLP to man_ikvm response after poll
    man_ikvm_url   = f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm"
    topmenu_url_d  = f"{base}/cgi/url_redirect.cgi?url_name=topmenu"
    sess.cookies.set('mainpage', 'remote')
    sess.cookies.set('subpage',  'man_ikvm')
    for step_label, url, extra_sid in [
        ('5c_man_ikvm_after_poll',         man_ikvm_url,                  False),
        ('5c2_man_ikvm_after_poll_sid',    man_ikvm_url,                  True),
        ('5c3_ikvm_after_poll',            f"{base}/cgi/url_redirect.cgi?url_name=ikvm", False),
        ('5c4_ikvm_after_poll_sid',        f"{base}/cgi/url_redirect.cgi?url_name=ikvm", True),
    ]:
        _sid = _pick_sid(sess.cookies)
        _url = url + (f'&SID={_sid}' if extra_sid and _sid else '')
        try:
            pg = sess.get(_url, timeout=8,
                          headers={'Referer': topmenu_url_d})
            steps.append({
                'step': step_label,
                'url': _url,
                'status': pg.status_code,
                'content_type': pg.headers.get('Content-Type', ''),
                'body_length': len(pg.content),
                'is_jnlp': '<jnlp' in pg.text.lower(),
                'body_preview': pg.text[:4500],
            })
        except Exception as e:
            steps.append({'step': step_label, 'error': str(e)})

    # Step 6b: extract KVM navigation cookies from full topmenu body
    try:
        topmenu_full = sess.get(f"{base}/cgi/url_redirect.cgi?url_name=topmenu", timeout=10)
        txt = topmenu_full.text
        # Find SetCookie calls within ±600 chars of KVM/IKVM/remote keywords
        kvm_cookies_found = []
        for m in re.finditer(r'(?:ikvm|kvm|remote|iview|console)', txt, re.I):
            chunk = txt[max(0, m.start()-600):min(len(txt), m.end()+600)]
            for n, v in re.findall(r'SetCookie\s*\(\s*["\'](\w+)["\'],\s*["\']([^"\']+)["\']', chunk):
                kvm_cookies_found.append(f'{n}={v}')
        # Find c_mainpage/c_subpage assignments near KVM
        mainpage_vals = re.findall(r'c_mainpage\s*[=,]\s*["\']([^"\']+)["\']', txt)
        subpage_vals  = re.findall(r'c_subpage\s*[=,]\s*["\']([^"\']+)["\']', txt)
        # Full SetCookie list
        all_setcookies = re.findall(r'SetCookie\s*\(\s*["\'](\w+)["\'],\s*["\']([^"\']+)["\']', txt)
        steps.append({
            'step': '6b_topmenu_kvm_cookies',
            'kvm_cookies_near_kvm_keyword': list(set(kvm_cookies_found)),
            'all_c_mainpage_values': list(set(mainpage_vals)),
            'all_c_subpage_values':  list(set(subpage_vals)),
            'all_setcookie_calls':   str(list(set(all_setcookies))[:30]),
            'current_session_cookies': dict(sess.cookies),
        })
    except Exception as e:
        steps.append({'step': '6b_topmenu_kvm_cookies', 'error': str(e)})

    # Step 8: fetch JS utility file — often contains the iKVM launch URL
    for js_path in ['/js/utils.js', '/js/util.js', '/js/ikvm.js', '/js/kvm.js']:
        try:
            r = sess.get(f"{base}{js_path}", timeout=8)
            if r.status_code == 200 and len(r.text) > 50:
                txt = r.text
                # Extract any .jnlp or cgi references from the JS
                jnlp_refs = re.findall(r'["\']([^"\']*(?:jnlp|ikvm|launch|kvm)[^"\']*)["\']', txt, re.I)
                # Find ALL quoted strings that look like URL paths
                all_url_strings = re.findall(r'["\']([^"\']{4,}(?:cgi|jnlp|\.cgi)[^"\']*)["\']', txt, re.I)
                # Find IKVM_SERVICE definition specifically
                ikvm_svc_m = re.search(r'IKVM_SERVICE\s*=\s*["\']([^"\']+)["\']', txt)
                # Show 1500 chars around IKVM_SERVICE reference
                ikvm_svc_ctx = ''
                m2 = re.search(r'IKVM_SERVICE', txt)
                if m2:
                    ikvm_svc_ctx = txt[max(0, m2.start()-300):min(len(txt), m2.end()+1200)]
                # Find the full GetIKVMStatus / get_ikvm_vm_status / GetPortInfo function
                fn_ctx = ''
                for fn_pat in (r'function\s+GetIKVMStatus', r'function\s+get_ikvm_vm_status',
                               r'function\s+GetPortInfo', r'function\s+GetPortStatus',
                               r'function\s+StartIKVM', r'function\s+LaunchIKVM',
                               r'function\s+ikvm_launch', r'function\s+OpenKVM'):
                    m3 = re.search(fn_pat, txt, re.I)
                    if m3:
                        fn_ctx += f'\n--- {fn_pat} ---\n'
                        fn_ctx += txt[m3.start():min(len(txt), m3.start()+3000)]
                # XMLHttpRequest / fetch calls (to find the URL being polled)
                xhr_urls = re.findall(r'open\s*\(\s*["\'][A-Z]+["\'],\s*["\']([^"\']+)["\']', txt, re.I)
                steps.append({
                    'step': f'8_js_{js_path.split("/")[-1]}',
                    'url': js_path,
                    'status': r.status_code,
                    'body_length': len(r.content),
                    'jnlp_kvm_refs': str(jnlp_refs[:30]),
                    'all_cgi_url_strings': str(list(dict.fromkeys(all_url_strings))[:40]),
                    'xhr_open_urls': str(xhr_urls[:20]),
                    'IKVM_SERVICE_value': ikvm_svc_m.group(1) if ikvm_svc_m else 'not found as string literal',
                    'IKVM_SERVICE_context_1500': ikvm_svc_ctx,
                    'kvm_function_bodies': fn_ctx[:6000] if fn_ctx else 'none found',
                    'body_first_5000': txt[:5000],
                    'body_5000_10000': txt[5000:10000] if len(txt) > 5000 else '',
                    'body_10000_15000': txt[10000:15000] if len(txt) > 10000 else '',
                })
        except Exception:
            pass

    # Step 9: full man_ikvm body (page is ~4042 bytes — show it completely)
    try:
        pg9 = sess.get(f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm", timeout=10,
                       headers={'Referer': f"{base}/cgi/url_redirect.cgi?url_name=topmenu"})
        steps.append({
            'step': '9_man_ikvm_full_body',
            'status': pg9.status_code,
            'body_length': len(pg9.content),
            'full_body': pg9.text,  # show everything — it's only ~4KB
        })
    except Exception as e:
        steps.append({'step': '9_man_ikvm_full_body', 'error': str(e)})

    # Step 9b: POST /cgi/ipmi.cgi op=GETPORTSINFO.XML — this is what get_ikvm_vm_status() calls
    # in utils.js (called from topmenu to check IKVM_SERVICE availability). Try this BEFORE
    # url_name=ikvm to see if it "activates" the endpoint.
    try:
        pg9b = sess.post(
            f"{base}/cgi/ipmi.cgi",
            data='op=GETPORTSINFO.XML&r=(0,0)',
            headers={
                'Referer':      f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm",
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=8,
        )
        steps.append({
            'step': '9b_ipmi_cgi_GETPORTSINFO',
            'status': pg9b.status_code,
            'body_length': len(pg9b.content),
            'body_preview': pg9b.text[:800],
        })
        # Immediately try url_name=ikvm after GETPORTSINFO
        _sid_now = _pick_sid(sess.cookies)
        for _lbl, _url in [
            ('9c_ikvm_after_GETPORTSINFO',
             f"{base}/cgi/url_redirect.cgi?url_name=ikvm"),
            ('9d_ikvm_after_GETPORTSINFO_SID',
             f"{base}/cgi/url_redirect.cgi?url_name=ikvm&SID={_sid_now}" if _sid_now else None),
        ]:
            if not _url:
                continue
            try:
                pg = sess.get(_url, timeout=8,
                              headers={'Referer': f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm"})
                steps.append({
                    'step': _lbl, 'url': _url,
                    'status': pg.status_code,
                    'content_type': pg.headers.get('Content-Type', ''),
                    'body_length': len(pg.content),
                    'is_jnlp': '<jnlp' in pg.text.lower(),
                    'body_preview': pg.text[:1000],
                })
            except Exception as e:
                steps.append({'step': _lbl, 'error': str(e)})
    except Exception as e:
        steps.append({'step': '9b_ipmi_cgi_GETPORTSINFO', 'error': str(e)})

    # Step 9e: man_ikvm_html5 — BMC's built-in HTML5 KVM viewer (no Java required)
    for _lbl, _url in [
        ('9e_man_ikvm_html5',        f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm_html5"),
        ('9f_man_ikvm_html5_novnc',  f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm_html5_novnc"),
    ]:
        try:
            pg = sess.get(_url, timeout=10,
                          headers={'Referer': f"{base}/cgi/url_redirect.cgi?url_name=topmenu"})
            steps.append({
                'step': _lbl, 'url': _url,
                'status': pg.status_code,
                'content_type': pg.headers.get('Content-Type', ''),
                'body_length': len(pg.content),
                'x_frame_options': pg.headers.get('X-Frame-Options', '(not set)'),
                'is_jnlp': '<jnlp' in pg.text.lower(),
                'body_preview': pg.text[:2000],
            })
        except Exception as e:
            steps.append({'step': _lbl, 'error': str(e)})

    # Step 10: alternative JNLP fetch methods
    _sid = _pick_sid(sess.cookies)
    alt_attempts = [
        # POST to man_ikvm (some firmware requires POST to trigger JNLP generation)
        ('10a_man_ikvm_POST',
         lambda: sess.post(f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm",
                           data='', headers={'Referer': f"{base}/cgi/url_redirect.cgi?url_name=topmenu"},
                           timeout=8)),
        # man_ikvm with JNLP Accept header
        ('10b_man_ikvm_accept_jnlp',
         lambda: sess.get(f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm",
                          headers={'Referer': f"{base}/cgi/url_redirect.cgi?url_name=topmenu",
                                   'Accept': 'application/x-java-jnlp-file,*/*'},
                          timeout=8)),
        # CGI_GetJNLPContent.cgi (GET with SID as query param)
        ('10c_CGI_GetJNLPContent_GET',
         lambda: sess.get(f"{base}/cgi/CGI_GetJNLPContent.cgi" + (f"?SID={_sid}" if _sid else ''),
                          timeout=8)),
        # CGI_GetJNLPContent.cgi POST with SID in body
        ('10d_CGI_GetJNLPContent_POST',
         lambda: sess.post(f"{base}/cgi/CGI_GetJNLPContent.cgi",
                           data=f"SID={_sid}" if _sid else '',
                           headers={'Content-Type': 'application/x-www-form-urlencoded'},
                           timeout=8)),
        # url_redirect with SID in URL + JNLP Accept
        ('10e_ikvm_SID_jnlp_accept',
         lambda: sess.get(f"{base}/cgi/url_redirect.cgi?url_name=ikvm" + (f"&SID={_sid}" if _sid else ''),
                          headers={'Accept': 'application/x-java-jnlp-file,*/*'},
                          timeout=8)),
        # GetJNLPContent without CGI prefix
        ('10f_GetJNLPContent_root',
         lambda: sess.get(f"{base}/GetJNLPContent.cgi" + (f"?SID={_sid}" if _sid else ''), timeout=8)),
        # man_ikvm with &action=launch (fictional but worth trying)
        ('10g_man_ikvm_action_launch',
         lambda: sess.get(f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm&action=launch",
                          timeout=8)),
    ]
    for label, fn in alt_attempts:
        try:
            pg = fn()
            steps.append({
                'step': label,
                'status': pg.status_code,
                'content_type': pg.headers.get('Content-Type', ''),
                'body_length': len(pg.content),
                'is_jnlp': '<jnlp' in pg.text.lower(),
                'body_preview': pg.text[:800],
            })
        except Exception as e:
            steps.append({'step': label, 'error': str(e)})

    result = {
        'server_ip': server['ip'],
        'username': server['username'],
        'session_cookies': dict(sess.cookies),
        'extracted_sid': _extract_sid_from_body(''.join(
            s.get('body_preview', '') for s in steps
        )),
        'steps': steps,
    }

    # Return as nicely formatted HTML for easy reading in browser
    import html as html_mod
    rows = ''
    for s in steps:
        rows += f'<tr><th colspan="2" style="background:#21262d;padding:8px">{html_mod.escape(s.get("step",""))}</th></tr>'
        for k, v in s.items():
            if k == 'step':
                continue
            rows += (f'<tr><td style="color:#8b949e;padding:4px 8px;white-space:nowrap">{html_mod.escape(str(k))}</td>'
                     f'<td style="padding:4px 8px"><pre style="margin:0;white-space:pre-wrap;word-break:break-all">'
                     f'{html_mod.escape(str(v))}</pre></td></tr>')

    page = f"""<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<title>JNLP Debug – {html_mod.escape(server['ip'])}</title>
<style>body{{font-family:monospace;background:#0d1117;color:#e6edf3;padding:1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #30363d;vertical-align:top}}
pre{{background:#161b22;padding:6px;border-radius:4px;font-size:.8rem}}</style></head>
<body>
<h2>JNLP Debug: {html_mod.escape(server['ip'])}</h2>
<p>Session Cookies: <code>{html_mod.escape(str(dict(sess.cookies)))}</code></p>
<table>{rows}</table>
<p style="margin-top:1rem;color:#8b949e">Bitte diesen Output für die weitere Diagnose bereitstellen.</p>
</body></html>"""
    return Response(page, content_type='text/html')


def _jnlp_error(ip: str, message: str, debug_url: str = '') -> Response:
    debug_link = (f'<p><a href="{debug_url}" target="_blank" '
                  f'style="color:#e3b341">🔍 Debug-Diagnose öffnen &rarr;</a></p>') if debug_url else ''
    html = f"""<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<title>KVM Fehler</title>
<style>body{{font-family:sans-serif;padding:2rem;background:#0d1117;color:#e6edf3}}
  h2{{color:#f85149}}a{{color:#79b8ff}}</style></head><body>
<h2>KVM Konsolenfehler</h2>
<p>{message}</p>
{debug_link}
<p><a href="https://{ip}" target="_blank">BMC Web-Interface direkt öffnen &rarr;</a></p>
</body></html>"""
    return Response(html, content_type='text/html', status=502)


# ── KVM HTML5 reverse-proxy ───────────────────────────────────────────────────────
#
# The BMC uses RFB 055.008 (ATEN proprietary) — only the BMC's own noVNC understands it.
# We proxy the BMC's HTML5 bootstrap page through Flask, patch nav_ui.js to connect
# to our local WebSocket proxy port instead of BMC:443 directly, and proxy all
# static assets (JS/CSS/images) from the BMC with session cookies injected.

@app.route('/kvm/<session_id>/view')
def kvm_html5_view(session_id):
    sess = _kvm.get_session(session_id) if _KVM_AVAILABLE else None
    if not sess or sess.status == 'stopped':
        return Response('<p>Session nicht gefunden</p>', status=404, content_type='text/html')

    bmc_ip = getattr(sess, 'bmc_ip', '')
    bmc_cookies = getattr(sess, 'bmc_cookies', {})

    if not bmc_ip:
        # Session still initialising — return auto-refresh page
        return Response(
            f'<html><head><meta http-equiv="refresh" content="1"></head>'
            f'<body style="background:#0d1117;color:#e6edf3;font-family:sans-serif;'
            f'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
            f'<p>Initialisiere KVM-Session…</p></body></html>',
            content_type='text/html',
        )

    s = req_lib.Session()
    s.verify = False
    s.headers.update(_BROWSER_HEADERS)
    s.cookies.update(bmc_cookies)

    try:
        r = s.get(
            f'https://{bmc_ip}/cgi/url_redirect.cgi?url_name=man_ikvm_html5_bootstrap',
            timeout=10,
        )
        html = r.text
    except Exception as e:
        return Response(f'<p>Bootstrap-Seite nicht abrufbar: {e}</p>', status=502, content_type='text/html')

    # Rewrite ALL quoted ../xxx references — both HTML attributes (src, href) and
    # JavaScript string literals (e.g. loadScript("../novnc/include/nav_ui.js")).
    # This handles firmware variants that load nav_ui.js dynamically instead of
    # via a static <script src="..."> tag.
    proxy_prefix = f'/kvm/{session_id}/proxy/'

    def _rewrite_url(m):
        quote, path = m.group(1), m.group(2)
        return f'{quote}{proxy_prefix}{path}{quote}'

    # Replace "../xxx" and '../xxx' everywhere
    html = re.sub(r'(["\'])\.\./([^"\']+)\1', _rewrite_url, html)

    # Inject INCLUDE_URI so Util.load_scripts() fetches scripts through our proxy.
    # Handles both direct <script src="...util.js"> and dynamic loadScript() cases.
    include_inject = (
        f'<script>var INCLUDE_URI = "{proxy_prefix}novnc/include/";</script>\n'
    )
    util_js_tag = f'<script src="{proxy_prefix}novnc/include/util.js'
    idx = html.find(util_js_tag)
    if idx != -1:
        html = html[:idx] + include_inject + html[idx:]
    else:
        # Fallback: inject before first </head> or at the start of <body>
        for anchor in ('</head>', '<body'):
            i = html.lower().find(anchor)
            if i != -1:
                html = html[:i] + include_inject + html[i:]
                break

    return Response(html, content_type='text/html; charset=utf-8')


@app.route('/kvm/<session_id>/proxy/<path:asset_path>')
def kvm_proxy_asset(session_id, asset_path):
    sess = _kvm.get_session(session_id) if _KVM_AVAILABLE else None
    if not sess:
        return Response('Session not found', status=404)

    bmc_ip = getattr(sess, 'bmc_ip', '')
    bmc_cookies = getattr(sess, 'bmc_cookies', {})
    ws_port = sess.port_ws

    s = req_lib.Session()
    s.verify = False
    s.headers.update(_BROWSER_HEADERS)
    s.cookies.update(bmc_cookies)

    try:
        r = s.get(f'https://{bmc_ip}/{asset_path}', timeout=10)
    except Exception as e:
        return Response(f'Proxy error: {e}', status=502)

    content = r.content
    ct = r.headers.get('Content-Type', 'application/octet-stream')

    # Patch nav_ui.js: hardcode our WebSocket proxy port and disable encryption.
    # Two firmware variants exist:
    #   Newer (B26NA+): get_port() function → replace entire function
    #   Older (.192/.178): inline port= assignment → regex-replace the assignment block
    if 'nav_ui.js' in asset_path.split('/')[-1]:
        try:
            text = content.decode('utf-8', errors='replace')
            # Strategy 1: replace the get_port() function wholesale (newer ATEN)
            new_fn = f'function get_port(){{return{{port:{ws_port},encrypt:false}}}}'
            patched = re.sub(
                r'function get_port\(\)\{(?:[^{}]|\{[^{}]*\})*\}',
                new_fn,
                text,
            )
            if patched == text:
                # Strategy 2: patch inline port= assignment (older ATEN)
                patched = re.sub(
                    r'port=window\.location\.port;'
                    r'if\(window\.location\.protocol\.substring\(0,5\)=="https"\)\{[^}]*\}'
                    r'(?:else if\(window\.location\.protocol\.substring\(0,4\)=="http"\)\{[^}]*\})?',
                    f'port={ws_port};encrypt=false;',
                    text,
                )
            content = patched.encode('utf-8')
        except Exception:
            pass

    return Response(content, content_type=ct)


# ── KVM Browser Sessions ─────────────────────────────────────────────────────────

@app.route('/api/servers/<server_id>/kvm-session', methods=['POST'])
def api_kvm_start(server_id):
    if not _KVM_AVAILABLE:
        return jsonify({'error': 'KVM nicht verfügbar (Xvfb/x11vnc nicht installiert)'}), 503
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404

    existing = _kvm.get_session_for_server(server_id)
    if existing:
        return jsonify({
            'session_id': existing.session_id,
            'ws_port':    existing.port_ws,
            'status':     existing.status,
            'message':    existing.message,
            'error':      existing.error,
        })

    try:
        sess = _kvm.start_session(server)
        return jsonify({
            'session_id': sess.session_id,
            'ws_port':    sess.port_ws,
            'status':     sess.status,
            'message':    sess.message,
        }), 201
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 503


@app.route('/api/servers/<server_id>/kvm-viewer', methods=['GET'])
def api_kvm_viewer(server_id):
    """Redirect to the proxied BMC HTML5 KVM viewer for the active session."""
    sess = _kvm.get_session_for_server(server_id) if _KVM_AVAILABLE else None
    if not sess or sess.status not in ('running', 'starting'):
        return Response(
            '<html><body style="background:#0d1117;color:#e6edf3;font-family:sans-serif;'
            'display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
            '<p>Keine aktive KVM-Session</p></body></html>',
            content_type='text/html',
        )
    return redirect(f'/kvm/{sess.session_id}/view')


@app.route('/api/servers/<server_id>/kvm-session', methods=['GET'])
def api_kvm_status(server_id):
    if not _KVM_AVAILABLE:
        return jsonify({'status': 'unavailable'}), 503
    sess = _kvm.get_session_for_server(server_id)
    if not sess:
        return jsonify({'status': 'none'}), 404
    return jsonify({
        'session_id': sess.session_id,
        'ws_port':    sess.port_ws,
        'status':     sess.status,
        'message':    sess.message,
        'error':      sess.error,
    })


@app.route('/api/servers/<server_id>/kvm-session', methods=['DELETE'])
def api_kvm_stop(server_id):
    if _KVM_AVAILABLE:
        sess = _kvm.get_session_for_server(server_id)
        if sess:
            _kvm.stop_session(sess.session_id)
    return '', 204


# ── Network Scan ──────────────────────────────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
def api_scan():
    network = (request.json or {}).get('network', '').strip()
    if not network:
        return jsonify({'error': 'Netzwerk erforderlich'}), 400
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    hosts = list(net.hosts())
    if len(hosts) > 1024:
        return jsonify({'error': 'Netzwerk zu groß (max /22)'}), 400

    # Try nmap first (more accurate), fall back to socket scan
    nmap_result = _scan_nmap(str(net))
    if nmap_result is not None:
        return jsonify({'found': nmap_result, 'total': len(hosts), 'method': 'nmap'})

    def probe(ip):
        return str(ip) if check_bmc_reachable(str(ip), timeout=1.0) else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        results = list(ex.map(probe, hosts))

    found = [r for r in results if r]
    return jsonify({'found': found, 'total': len(hosts), 'method': 'socket'})


def _scan_nmap(network: str) -> list[str] | None:
    """Use nmap to find hosts with IPMI/BMC ports open. Returns None if nmap unavailable."""
    try:
        r = subprocess.run(
            ['nmap', '-p', '623,443,80', '--open', '-n', '-T4', '-oG', '-', network],
            capture_output=True, text=True, timeout=180
        )
        found = []
        for line in r.stdout.splitlines():
            if line.startswith('Host:') and '/open/' in line:
                ip = line.split()[1]
                found.append(ip)
        return found
    except FileNotFoundError:
        return None
    except Exception:
        return None


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9193, debug=False)
