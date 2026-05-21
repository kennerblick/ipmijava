import json
import os
import socket
import subprocess
import uuid
import ipaddress
import concurrent.futures
from flask import Flask, jsonify, request, send_from_directory

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
        'ikvm_url':  f"https://{server['ip']}/cgi/url_redirect.cgi?url_name=ikvm",
        'html5_url': f"https://{server['ip']}/kvm.html",
        'bmc_url':   f"https://{server['ip']}",
    })


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
