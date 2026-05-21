import json
import os
import re
import socket
import subprocess
import uuid
import ipaddress
import concurrent.futures

import requests as req_lib
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask, jsonify, request, send_from_directory, Response

app = Flask(__name__)
CONFIG_PATH = os.environ.get('CONFIG_PATH', '/config/servers.json')
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'frontend')


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

def check_bmc_reachable(ip: str, timeout: float = 2.0) -> bool:
    for port in (443, 80, 623):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            if s.connect_ex((ip, port)) == 0:
                s.close()
                return True
            s.close()
        except Exception:
            pass
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
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404
    d = request.json or {}
    for key in ('name', 'ip', 'username', 'password', 'description'):
        if key in d:
            server[key] = d[key].strip() if isinstance(d[key], str) else d[key]
    save_config(config)
    return jsonify(server)


@app.route('/api/servers/<server_id>', methods=['DELETE'])
def api_delete_server(server_id):
    config = load_config()
    for g in config['groups']:
        g['servers'] = [s for s in g['servers'] if s['id'] != server_id]
    save_config(config)
    return '', 204


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
        ok, out, _ = run_ipmitool(server, 'power', 'status', timeout=10)
        if ok:
            lower = out.lower()
            power = 'on' if 'on' in lower else 'off' if 'off' in lower else 'unknown'

    return jsonify({'id': server_id, 'online': online, 'power': power})


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
        'html5_url':  f"https://{server['ip']}/kvm.html",
        'bmc_url':    f"https://{server['ip']}",
    })


# ── BMC session helpers ───────────────────────────────────────────────────────

# Static JNLP URL candidates (tried in order before mainmenu scraping)
_JNLP_PATHS = [
    '/cgi/url_redirect.cgi?url_name=ikvm',
    '/cgi/url_redirect.cgi?url_name=java_iview',
    '/cgi/url_redirect.cgi?url_name=iview',
    '/cgi/url_redirect.cgi?url_name=kvm',
    '/cgi/url_redirect.cgi?url_name=kvmjnlp',
    '/cgi/ikvm.cgi',
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


def _bmc_login(base: str, username: str, password: str) -> tuple[req_lib.Session, str | None]:
    """Authenticate against the BMC and return (session, sid_or_None).

    Handles two BMC generations:
    - Legacy ATEN (X8/X9): login.cgi returns 200 with SID embedded in JavaScript
    - AMI MegaRAC (X10/X11/X12+): login.cgi sets SID as an HTTP cookie
    """
    sess = req_lib.Session()
    sess.verify = False

    try:
        r = sess.post(
            f"{base}/cgi/login.cgi",
            data={'name': username, 'pwd': password},
            timeout=10,
            allow_redirects=False,   # keep=False so we see the raw Set-Cookie on the 302
        )
    except Exception:
        return sess, None

    # AMI path: cookie already on the 302 response
    sid = _pick_sid(r.cookies) or _pick_sid(sess.cookies)

    if not sid:
        # Follow the redirect manually so the session accumulates cookies
        loc = r.headers.get('Location', '')
        if loc and r.status_code in (301, 302, 303, 307, 308):
            target = loc if loc.startswith('http') else f"{base}{loc}"
            try:
                r2 = sess.get(target, timeout=10, allow_redirects=True)
                sid = _pick_sid(sess.cookies) or _pick_sid(r2.cookies)
                # ATEN path: SID may be embedded in the JS of the redirected page
                if not sid:
                    sid = _extract_sid_from_body(r2.text)
            except Exception:
                pass

    # ATEN path on 200: SID in the JS body of login.cgi itself
    if not sid and r.status_code == 200:
        sid = _extract_sid_from_body(r.text)

    if sid:
        sess.cookies.set('SID', sid)

    return sess, sid


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


def _fetch_jnlp(sess: req_lib.Session, base: str, sid: str | None) -> req_lib.Response | None:
    """Try all known JNLP paths, then scrape mainmenu for unknown firmware variants."""
    candidates = []
    for path in _JNLP_PATHS:
        candidates.append(f"{base}{path}")
        if sid:
            sep = '&' if '?' in path else '?'
            candidates.append(f"{base}{path}{sep}SID={sid}")

    for url in candidates:
        try:
            r = sess.get(url, timeout=8)
            if '<jnlp' in r.text.lower():
                return r
        except Exception:
            continue

    # Static paths exhausted — scrape mainmenu for firmware-specific KVM links
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


# ── Console endpoints ─────────────────────────────────────────────────────────

@app.route('/api/servers/<server_id>/jnlp', methods=['GET'])
def api_console_jnlp(server_id):
    """Log in to the BMC, fetch iKVM JNLP, proxy it to the browser.

    Handles both ATEN (SID in JS body) and AMI (SID as HTTP cookie) BMC firmware.
    Java Web Start then connects directly from the client to the BMC.
    """
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Server nicht gefunden'}), 404

    base = f"https://{server['ip']}"
    try:
        sess, sid = _bmc_login(base, server['username'], server['password'])
        jnlp_resp = _fetch_jnlp(sess, base, sid)

        if jnlp_resp is None:
            debug_url = f"/api/servers/{server_id}/jnlp-debug"
            return _jnlp_error(
                server['ip'],
                'BMC lieferte keine JNLP-Datei.<br>'
                'Mögliche Ursachen: falsches Passwort, nur HTML5-KVM verfügbar, '
                'oder abweichende Firmware-Version.',
                debug_url,
            )

        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', server['name'])
        return Response(
            jnlp_resp.content,
            content_type='application/x-java-jnlp-file',
            headers={
                'Content-Disposition': f'attachment; filename="{safe_name}.jnlp"',
                'Cache-Control': 'no-store',
            },
        )

    except req_lib.exceptions.SSLError:
        return _jnlp_error(server['ip'], 'SSL-Fehler (selbstsigniertes Zertifikat).')
    except req_lib.exceptions.ConnectionError:
        return _jnlp_error(server['ip'], f'BMC nicht erreichbar: {server["ip"]}')
    except req_lib.exceptions.Timeout:
        return _jnlp_error(server['ip'], 'Timeout beim Verbinden mit BMC.')
    except Exception as e:
        return _jnlp_error(server['ip'], str(e))


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

    # Step 3+: scan all navigation pages and show url_name values + body
    for label, url_name in [
        ('3_mainmenu', 'mainmenu'),
        ('4_topmenu',  'topmenu'),
        ('5_remote_control', 'remote_control'),
    ]:
        try:
            pg = sess.get(f"{base}/cgi/url_redirect.cgi?url_name={url_name}", timeout=10)
            names = sorted({m.group(1) for m in re.finditer(r'url_name=([a-zA-Z_0-9]+)', pg.text)})
            jnlp_h = re.findall(r'(?:href|src)=["\']([^"\']+\.jnlp[^"\']*)["\']', pg.text, re.I)
            steps.append({
                'step': label,
                'url': f"/cgi/url_redirect.cgi?url_name={url_name}",
                'status': pg.status_code,
                'body_length': len(pg.content),
                'url_names_found': str(names),
                'jnlp_hrefs_found': str(jnlp_h),
                'body_preview': pg.text[:1200],
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
