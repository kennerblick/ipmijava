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
        'java_fix':   f"/api/servers/{server_id}/java-fix.ps1",
        'bmc_url':    f"https://{server['ip']}",
    })


@app.route('/api/servers/<server_id>/java-fix.ps1', methods=['GET'])
def api_java_fix(server_id):
    """Return a PowerShell script that configures Java Web Start to trust this BMC.

    ATEN iKVM JARs are signed with SHA1withRSA, which Java 8u211+ disables by
    default.  The script configures the per-user Java deployment settings
    (no admin rights required) and adds this BMC to the exception-site list.
    """
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Nicht gefunden'}), 404

    ip    = server['ip']
    name  = server['name']
    bmc   = f"https://{ip}"

    script = f"""# Java Web Start – Sicherheitsfix für ATEN iKVM
# Server: {name}  ({ip})
# Einmalig ausführen; danach JNLP direkt starten.

$ErrorActionPreference = 'Stop'

$deployDir   = "$env:APPDATA\\Sun\\Java\\Deployment"
$secDir      = "$deployDir\\security"
$propsFile   = "$deployDir\\deployment.properties"
$sitesFile   = "$secDir\\exception.sites"

Write-Host "=== Java iKVM Sicherheitsfix ===" -ForegroundColor Cyan
Write-Host "Konfiguriere Java fuer: {bmc}"

# --- 1. Verzeichnisse erstellen -------------------------------------------------
New-Item -ItemType Directory -Force -Path $secDir | Out-Null

# --- 2. Exception-Site hinzufügen -----------------------------------------------
$site = '{bmc}'
if (Test-Path $sitesFile) {{
    $existing = Get-Content $sitesFile -Raw
}} else {{
    $existing = ''
}}
if ($existing -notmatch [regex]::Escape($site)) {{
    Add-Content -Path $sitesFile -Value $site -Encoding UTF8
    Write-Host "  [OK] Exception-Site hinzugefuegt: $site" -ForegroundColor Green
}} else {{
    Write-Host "  [--] Exception-Site bereits vorhanden" -ForegroundColor Yellow
}}

# --- 3. deployment.properties aktualisieren ------------------------------------
$needed = @{{
    'deployment.security.level'                = 'MEDIUM'
    'deployment.security.validation.crl'       = 'false'
    'deployment.security.validation.ocsp'      = 'false'
    'deployment.security.expired.warning'      = 'false'
    'deployment.security.jsse.hostmismatch.warning' = 'false'
    'deployment.manifest.attributes.check'    = 'false'
}}

if (Test-Path $propsFile) {{
    $lines = [System.Collections.Generic.List[string]](Get-Content $propsFile -Encoding UTF8)
}} else {{
    $lines = [System.Collections.Generic.List[string]]::new()
}}

foreach ($key in $needed.Keys) {{
    $val   = $needed[$key]
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {{
        if ($lines[$i] -match "^\\s*$([regex]::Escape($key))\\s*=") {{
            $lines[$i] = "$key=$val"
            $found = $true
            break
        }}
    }}
    if (-not $found) {{ $lines.Add("$key=$val") }}
    Write-Host "  [OK] $key=$val" -ForegroundColor Green
}}

$lines | Set-Content $propsFile -Encoding UTF8

# --- 4. SHA1 in java.security entsperren ----------------------------------------
#  Liest java.security zeilenweise, entfernt SHA1-Tokens aus den beiden
#  disabledAlgorithms-Properties (inkl. Fortsetzungszeilen mit \).
#  Kein \r/\n im Regex noetig → kein Python-f-string-Escape-Problem.

function Remove-SHA1Restrictions {{
    param([string]$Path)
    $lines    = [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)
    $out      = [System.Collections.Generic.List[string]]::new()
    $changed  = $false
    $skipCont = $false

    for ($i = 0; $i -lt $lines.Count; $i++) {{
        $line = $lines[$i]
        if ($skipCont) {{
            $changed = $true
            if (-not $line.TrimEnd().EndsWith('\')) {{ $skipCont = $false }}
            continue
        }}
        if ($line -match '^(jdk\\.jar\\.disabledAlgorithms|jdk\\.certpath\\.disabledAlgorithms)\\s*=') {{
            $propName = $Matches[1]
            $val = ($line -replace '^[^=]+=\\s*', '').TrimEnd('\').Trim()
            while ($line.TrimEnd().EndsWith('\') -and ($i + 1) -lt $lines.Count) {{
                $i++; $line = $lines[$i]
                $val += ',' + $line.TrimEnd('\').Trim()
            }}
            $parts = ($val -split ',') | Where-Object {{ $_.Trim() -notmatch '^SHA1' }}
            $out.Add("$propName=" + ($parts -join ', '))
            $changed = $true
        }} else {{
            $out.Add($line)
        }}
    }}
    if ($changed) {{
        [System.IO.File]::WriteAllText($Path, ($out -join "`r`n"), [System.Text.Encoding]::UTF8)
    }}
    return $changed
}}

# Per-user Override (kein Admin noetig, Java 8u171+)
$userSecDir  = "$env:APPDATA\\Sun\\Java\\Deployment\\security"
$userSecFile = "$userSecDir\\java.security"
New-Item -ItemType Directory -Force -Path $userSecDir | Out-Null
if (-not (Test-Path $userSecFile)) {{
    $minContent = "# iKVM SHA1-Fix`r`n" +
                  "jdk.jar.disabledAlgorithms=MD2, MD5, RSA keySize < 1024, DSA keySize < 1024`r`n" +
                  "jdk.certpath.disabledAlgorithms=MD2, MD5, DSA keySize < 1024, RSA keySize < 1024`r`n"
    [System.IO.File]::WriteAllText($userSecFile, $minContent, [System.Text.Encoding]::UTF8)
    Write-Host "  [OK] Per-user java.security angelegt: $userSecFile" -ForegroundColor Green
}} else {{
    if (Remove-SHA1Restrictions $userSecFile) {{
        Write-Host "  [OK] SHA1-Restrictions entfernt: $userSecFile" -ForegroundColor Green
    }} else {{
        Write-Host "  [--] Per-user java.security bereits bereinigt" -ForegroundColor Yellow
    }}
}}

# Zusaetzlich JRE-Systemdatei patchen (erfordert Admin-Rechte)
$javaHomes = [System.Collections.Generic.List[string]]::new()
if ($env:JAVA_HOME) {{ $javaHomes.Add($env:JAVA_HOME) }}
$javawsCmd = Get-Command javaws -ErrorAction SilentlyContinue
if ($javawsCmd) {{ $javaHomes.Add((Split-Path -Parent (Split-Path -Parent $javawsCmd.Source))) }}
foreach ($d in @('C:\\Program Files\\Java', 'C:\\Program Files (x86)\\Java')) {{
    if (Test-Path $d) {{
        Get-ChildItem $d -ErrorAction SilentlyContinue |
            ForEach-Object {{ $javaHomes.Add($_.FullName) }}
    }}
}}

$patched = $false
foreach ($jh in $javaHomes) {{
    $sec = "$jh\\lib\\security\\java.security"
    if (-not (Test-Path $sec)) {{ continue }}
    Write-Host "  Versuche JRE-Datei: $sec"
    try {{
        Copy-Item $sec "$sec.bak" -Force -ErrorAction Stop
        if (Remove-SHA1Restrictions $sec) {{
            Write-Host "  [OK] SHA1-Einschraenkungen entfernt (Backup: $sec.bak)" -ForegroundColor Green
            $patched = $true
        }} else {{
            Write-Host "  [--] Keine SHA1-Einschraenkungen in JRE-Datei" -ForegroundColor Yellow
        }}
        break
    }} catch {{
        Write-Host "  [!!] Kein Schreibzugriff – Skript als Admin ausfuehren fuer JRE-Patch" -ForegroundColor Yellow
        break
    }}
}}
if (-not $patched) {{
    Write-Host "  [i] Per-user Override aktiv – reicht fuer die meisten Faelle." -ForegroundColor Cyan
}}

Write-Host ""
Write-Host "Fertig! Bitte JNLP-Datei erneut starten." -ForegroundColor Green
Write-Host "Druecke eine Taste zum Beenden..."
$null = $host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
"""

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
    # Also sets mainpage/subpage cookies needed for the KVM section.
    _bmc_navigate_to_kvm(sess, base)

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
            r = sess.get(url, timeout=8, headers=jnlp_headers)
            if '<jnlp' in r.text.lower():
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


# ── Console endpoints ─────────────────────────────────────────────────────────

@app.route('/api/servers/<server_id>/jnlp', methods=['GET'])
def api_console_jnlp(server_id):
    """Return an HTML relay page that logs the user's browser into the BMC
    and opens the Console Redirection page (man_ikvm) in a new tab.

    Background: url_redirect.cgi?url_name=ikvm validates the requesting client IP
    against the session's registered IP. A backend proxy therefore always gets 404 —
    the JNLP is only served to the browser that originally established the session.
    Solution: log the browser in via a hidden iframe (sets SID cookie in the user's
    browser for the BMC domain), then open man_ikvm in a new tab where the user
    clicks "Launch iKVM" to download the JNLP directly.
    """
    config = load_config()
    server, _ = find_server(config, server_id)
    if not server:
        return jsonify({'error': 'Server nicht gefunden'}), 404

    import html as html_mod
    ip          = html_mod.escape(server['ip'])
    username    = html_mod.escape(server['username'])
    password    = html_mod.escape(server['password'])
    bmc_base    = f"https://{server['ip']}"
    login_url   = f"{bmc_base}/cgi/login.cgi"
    console_url = f"{bmc_base}/cgi/url_redirect.cgi?url_name=man_ikvm"

    page = f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <title>iKVM – {ip}</title>
  <style>
    body {{ font-family:sans-serif; background:#0d1117; color:#e6edf3;
            display:flex; flex-direction:column; align-items:center;
            justify-content:center; min-height:100vh; margin:0; padding:1rem;
            text-align:center; }}
    h2 {{ margin-bottom:.5rem; }}
    p  {{ color:#8b949e; margin:.4rem 0; font-size:.95rem; }}
    .btn {{ display:inline-block; padding:.55rem 1.4rem; background:#1f6feb;
             color:#fff; border:none; border-radius:6px; cursor:pointer;
             font-size:1rem; text-decoration:none; margin:.4rem .2rem; }}
    .btn:hover {{ background:#388bfd; }}
    .btn-sec {{ background:#30363d; color:#e6edf3; }}
    .btn-sec:hover {{ background:#484f58; }}
    .spinner {{ display:inline-block; width:1rem; height:1rem;
                border:3px solid #30363d; border-top-color:#58a6ff;
                border-radius:50%; animation:spin .8s linear infinite;
                vertical-align:middle; margin-right:.4rem; }}
    @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
    #step-error {{ display:none; color:#f85149; }}
    #step-retry {{ display:none; }}
  </style>
</head>
<body>
  <!--
    Login form — submitted into a tiny popup window.
    A popup (not an iframe) establishes its own first-party browsing context
    for the BMC domain, so the SID cookie goes into the unpartitioned global
    store.  When this relay tab then navigates to man_ikvm the cookie is
    present and the "session timed out" error does not occur.
  -->
  <form id="lf" action="{login_url}" method="post">
    <input type="hidden" name="name" value="{username}">
    <input type="hidden" name="pwd"  value="{password}">
  </form>

  <div id="step-working">
    <h2>iKVM — {ip}</h2>
    <p><span class="spinner"></span><span id="status-msg">Anmeldung am BMC…</span></p>
  </div>

  <div id="step-retry">
    <h2>iKVM — {ip}</h2>
    <p>Popup-Fenster wurde vom Browser blockiert.</p>
    <button class="btn" onclick="startLogin()">🔑 Anmelden &amp; Konsole öffnen</button>
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
  var CONSOLE_URL = '{console_url}';
  var pollTimer   = null;
  var timeoutId   = null;
  var loginPopup  = null;

  function startLogin() {{
    document.getElementById('step-retry').style.display  = 'none';
    document.getElementById('step-error').style.display  = 'none';
    document.getElementById('step-working').style.display = 'block';
    document.getElementById('status-msg').textContent = 'Anmeldung am BMC…';

    // Open a tiny off-screen popup — it is its own top-level browsing context,
    // so cookies the BMC sets inside it land in the global (unpartitioned)
    // first-party store for the BMC domain.
    loginPopup = window.open(
      'about:blank', 'bmc_login_popup',
      'width=1,height=1,left=-200,top=-200,menubar=no,toolbar=no,status=no,scrollbars=no'
    );

    if (!loginPopup) {{
      // Popup blocker active — show manual button
      document.getElementById('step-working').style.display = 'none';
      document.getElementById('step-retry').style.display   = 'block';
      return;
    }}

    // Route form submission into the popup
    var form = document.getElementById('lf');
    form.setAttribute('target', 'bmc_login_popup');
    form.submit();

    // Poll until the popup crosses into BMC domain (becomes cross-origin)
    pollTimer = setInterval(checkPopup, 150);
    timeoutId = setTimeout(onTimeout, 15000);
  }}

  function checkPopup() {{
    if (!loginPopup || loginPopup.closed) {{
      clearInterval(pollTimer); pollTimer = null;
      return;
    }}
    try {{
      // As long as the popup is on about:blank or our own origin this succeeds
      var _unused = loginPopup.location.href;
    }} catch (e) {{
      // SecurityError: popup navigated to the BMC domain —
      // login response has been received, SID cookie is now set (first-party).
      clearInterval(pollTimer); pollTimer = null;
      clearTimeout(timeoutId);
      // Brief pause so the browser fully commits the cookie, then navigate
      setTimeout(function() {{
        try {{ loginPopup.close(); }} catch (_) {{}}
        window.location.href = CONSOLE_URL;
      }}, 250);
    }}
  }}

  function onTimeout() {{
    clearInterval(pollTimer); pollTimer = null;
    if (loginPopup && !loginPopup.closed) {{ try {{ loginPopup.close(); }} catch(_) {{}} }}
    document.getElementById('step-working').style.display = 'none';
    document.getElementById('step-error').style.display   = 'block';
    document.getElementById('error-msg').textContent =
      'Timeout — BMC nicht erreichbar oder falsche Zugangsdaten.';
  }}

  // Auto-start: this page was opened by a user click so window.open is
  // typically allowed; if not the retry button is shown.
  startLogin();
  </script>
</body>
</html>"""
    return Response(page, content_type='text/html')


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
        ('5_man_ikvm',       'man_ikvm',       4000),   # Console Redirection page — calls GetIKVMStatus()
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

    # Step 5c: fetch url_name=ikvm AFTER the poll (this is what the browser does)
    try:
        jnlp_url = f"{base}/cgi/url_redirect.cgi?url_name=ikvm"
        pg = sess.get(jnlp_url, timeout=8,
                      headers={'Referer': f"{base}/cgi/url_redirect.cgi?url_name=man_ikvm"})
        steps.append({
            'step': '5c_ikvm_after_poll',
            'url': jnlp_url,
            'status': pg.status_code,
            'content_type': pg.headers.get('Content-Type', ''),
            'body_length': len(pg.content),
            'is_jnlp': '<jnlp' in pg.text.lower(),
            'body_preview': pg.text[:800],
        })
    except Exception as e:
        steps.append({'step': '5c_ikvm_after_poll', 'error': str(e)})

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
                # Extract any .jnlp or cgi references from the JS
                jnlp_refs = re.findall(r'["\']([^"\']*(?:jnlp|ikvm|launch|kvm)[^"\']*)["\']', r.text, re.I)
                # Find IKVM_SERVICE definition specifically
                ikvm_svc_m = re.search(r'IKVM_SERVICE\s*=\s*["\']([^"\']+)["\']', r.text)
                # Also show the 500 chars around IKVM_SERVICE definition
                ikvm_svc_ctx = ''
                m2 = re.search(r'IKVM_SERVICE', r.text)
                if m2:
                    ikvm_svc_ctx = r.text[max(0, m2.start()-100):min(len(r.text), m2.end()+400)]
                steps.append({
                    'step': f'8_js_{js_path.split("/")[-1]}',
                    'url': js_path,
                    'status': r.status_code,
                    'body_length': len(r.content),
                    'jnlp_kvm_refs': str(jnlp_refs[:20]),
                    'IKVM_SERVICE_value': ikvm_svc_m.group(1) if ikvm_svc_m else 'not found as string literal',
                    'IKVM_SERVICE_context': ikvm_svc_ctx,
                    'body_preview': r.text[:2000],
                })
        except Exception:
            pass

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
