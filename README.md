# ipmijava — Web-based IPMI Manager for Supermicro Servers

A self-hosted Docker web application for managing multiple Supermicro IPMI/BMC interfaces grouped by category.

## Features

| Feature | Details |
|---|---|
| Server groups | Organize servers in named groups |
| Online status | BMC reachability check (TCP 443/80/623) + power state via ipmitool |
| Power actions | Power ON · Soft OFF (ACPI) · Force OFF · Reset · Power Cycle |
| HTML5 KVM console | Embedded in-browser KVM via WebSocket proxy (ATEN firmware supported) |
| Network scan | Discovers IPMI/BMC devices in a subnet (nmap preferred, socket fallback) |
| Scan import | Import all discovered IPs at once into an "Ungruppiert" group |
| Persistent config | JSON file on host — survives container restarts |

## Quick Start

```bash
# 1. Clone
git clone https://github.com/kennerblick/ipmijava
cd ipmijava

# 2. Optional: copy example config as starting point
cp config/servers.json.example config/servers.json

# 3. Build & start
docker compose up -d --build

# 4. Open browser
http://<host>:9193
```

## Configuration

The server list is stored in `./config/servers.json` (mounted into the container at `/config/servers.json`).
The file is created automatically when you add the first server via the UI.

A commented example is at [`config/servers.json.example`](config/servers.json.example).

> **Note:** Passwords are stored in plaintext in the JSON file. Restrict file permissions accordingly (`chmod 600 config/servers.json`).

## Network access for scanning

The compose file uses `network_mode: host` so nmap can scan the same network segments the host can reach. On non-Linux hosts (Docker Desktop on macOS/Windows) this has no effect — scanning then uses a slower socket fallback and is limited to networks the bridge can reach.

To use bridge networking instead, replace `network_mode: host` in `docker-compose.yml` with:
```yaml
ports:
  - "9193:9193"
  - "6080-6089:6080-6089"
```

## KVM Console

The application supports embedded in-browser KVM access for Supermicro BMC (ATEN firmware). Each concurrent KVM session uses one port from the range 6080–6089.

### How it works

ATEN BMC firmware speaks **RFB 055.008** — a proprietary VNC protocol extension that is incompatible with standard noVNC clients. The only working client is the ATEN-patched noVNC bundled with the BMC itself. This application proxies that entire stack transparently:

```
Browser
  │  HTTP GET /kvm/<sid>/view
  │    ← Flask fetches BMC bootstrap page, rewrites asset URLs,
  │      injects INCLUDE_URI so ATEN noVNC loads via proxy
  │
  │  HTTP GET /kvm/<sid>/proxy/<asset>
  │    ← Flask proxies every JS/CSS asset from the BMC,
  │      patching nav_ui.js to redirect the WebSocket to localhost:608x
  │
  │  WebSocket ws://host:608x/
  │    ← asyncio proxy forwards to wss://BMC:443/ with BMC session
  │      cookie and correct Origin header
  │
  └─► BMC   RFB 055.008 + security type 16 auth (entry_value token)
```

### Authentication flow

1. `POST /cgi/login.cgi` — logs in, receives `SID` session cookie
2. `GET man_ikvm_html5_bootstrap` — extracts the `entry_value` token (a per-session base64 auth token embedded as a hidden input)
3. The asyncio proxy connects `wss://BMC:443/` with `Cookie: SID=...` and `Origin: https://BMC`
4. ATEN's `rfb.js` uses `entry_value` as both VNC username and password for security type 16

### nav_ui.js patching

ATEN's `nav_ui.js` hardcodes `port=window.location.port` and selects `encrypt=true` when protocol is HTTPS. Because the browser accesses the app over HTTP (or a different port), this is patched on-the-fly:

```
port=window.location.port;if(window.location.protocol...https...){encrypt=true}...
  →  port=6080;encrypt=false;
```

### Requirements

- Ports 6080–6089 must be reachable from the browser (mapped in `docker-compose.yml`)
- The BMC must be reachable from the Docker container at its HTTPS port (443)
- Tested with: Supermicro X10 / X11 boards, ATEN BMC firmware with HTML5 KVM support

## Network Scan & Import

The scan modal discovers BMC/IPMI devices on a subnet:

1. Enter a CIDR range (e.g. `192.168.1.0/24`) and click **Scannen**
2. Found devices appear as a list
3. **Hinzufügen** — opens the server edit modal for a single IP (prefilled)
4. **Alle importieren** — imports all found IPs at once into the group "Ungruppiert" (auto-created if needed), skipping IPs already in the config

After importing, open any server in edit mode to assign it to the correct group.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CONFIG_PATH` | `/config/servers.json` | Path to config file inside container |

## Power Actions

| Button | ipmitool command | Description |
|---|---|---|
| **ON** | `power on` | Power on the server |
| **Soft** | `power soft` | ACPI graceful shutdown |
| **Force** | `power off` | Immediate power cut |
| **Reset** | `power reset` | Hard reset signal |
| **⚡ (Cycle)** | `power cycle` | Power cycle (off + on) |
