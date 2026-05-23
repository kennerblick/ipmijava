# ipmijava — Web-based IPMI Manager for Supermicro Servers

A self-hosted Docker web application for managing multiple Supermicro IPMI/BMC interfaces grouped by category.

## Features

| Feature | Details |
|---|---|
| Server groups | Organize servers in named groups |
| Online status | BMC reachability (parallel TCP 443/80/623) + power state via ipmitool |
| Power actions | Power ON · Soft OFF (ACPI) · Force OFF · Reset · Power Cycle |
| HTML5 KVM console | Embedded in-browser KVM via WebSocket proxy (ATEN firmware) |
| Java iKVM | JNLP download for ATEN (browser relay) and AMI MegaRAC / GoAhead firmware |
| Capability probe | Auto-detects which features each BMC supports; shows only relevant buttons |
| Network scan | Discovers IPMI/BMC devices in a subnet (nmap preferred, socket fallback) |
| Scan import | Import all discovered IPs at once into an "Ungruppiert" group |
| Bulk status poll | One parallel backend request for all servers — stays fast at 100+ entries |
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

### HTML5 KVM (ATEN firmware)

Supported on Supermicro X10/X11/X12 boards with ATEN BMC firmware that includes the HTML5 KVM bootstrap page. Each concurrent session uses one port from 6080–6089.

ATEN BMC firmware speaks **RFB 055.008** — a proprietary VNC protocol extension incompatible with standard noVNC. This application proxies the full stack transparently:

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

**Authentication flow:**
1. `POST /cgi/login.cgi` — logs in, receives `SID` session cookie
2. `GET man_ikvm_html5_bootstrap` — extracts the `entry_value` token
3. The asyncio proxy connects `wss://BMC:443/` with `Cookie: SID=...` and `Origin: https://BMC`
4. ATEN's `rfb.js` uses `entry_value` as VNC username + password for security type 16

**Requirements:**
- Ports 6080–6089 must be reachable from the browser (mapped in `docker-compose.yml`)
- BMC must be reachable from the container at HTTPS port 443
- Tested: Supermicro X10/X11, ATEN firmware with HTML5 KVM (`man_ikvm_html5_bootstrap`)

### Java iKVM (ATEN + AMI MegaRAC / GoAhead)

Two firmware families are supported:

| Firmware | Login | JNLP path | Flow |
|---|---|---|---|
| ATEN (X10/X11/X12) | `/cgi/login.cgi` (form POST) | `/cgi/url_redirect.cgi?url_name=ikvm` | Browser relay page: popup logs in, opens `man_ikvm` |
| ATEN older (X9, B16NA) | `/cgi/login.cgi` | `url_name=ikvm&url_type=jwsk` | Same browser relay |
| AMI MegaRAC / GoAhead (X8/X9) | `/rpc/WEBSES/create.asp` (WEBVAR_USERNAME/PASSWORD) | `/Java/jviewer.jnlp` | Server-side login + direct JNLP download |

For **GoAhead** firmware (e.g. Supermicro X8/X9 with AMI MegaRAC): clicking "Java iKVM starten" triggers a server-side login and streams the JNLP directly to the browser as `application/x-java-jnlp-file`. Open the downloaded file with Java Web Start (`javaws`).

GoAhead's JNLP endpoint has a known bug: it reports `Content-Length: 3757` but closes the connection after ~2013 bytes. The app reads until EOF and ignores the mismatch.

### Java Security Fix (.ps1)

The "Java Security einrichten" button downloads a PowerShell script that configures the local Java installation for the minimum security required to launch iKVM JNLP files:

- `deployment.security.level=LOW`
- `deployment.security.mixcode=DISABLE` (allows JViewer native libs)
- CRL/OCSP validation disabled
- SHA1/MD5/RC4/DES/RSA and DSA certificate restrictions removed from `java.security`
- BMC added to Java exception site list

Run once per client workstation; only needs Admin rights to patch the JRE's global `java.security` file (per-user override works without Admin).

### Capability probe

When a server is saved, the app probes which features the BMC supports and stores the result in `servers.json`. Only the supported buttons are shown:

| Flag | Meaning |
|---|---|
| `bmc_http` | BMC web interface reachable |
| `ipmi` | ipmitool IPMI-over-LAN works |
| `kvm_aten` | ATEN HTML5 KVM (WebSocket sends RFB data) |
| `ikvm_java` | Valid JNLP found (ATEN or AMI JViewer) |

Use the rescan (☠) button in the sidebar or the reload icon on a server card to re-probe after firmware upgrades.

## Performance with many servers

The status of all servers is polled in one `/api/bulk-status` request. The backend runs all TCP reachability checks and ipmitool calls concurrently in a thread pool (up to 40 workers). This keeps the full refresh under ~5 seconds even with 100 entries on a LAN, compared to 60–100 seconds with individual requests.

Individual port checks (443/80/623) run in parallel per server, so an unreachable BMC adds ~1.5 s instead of ~6 s.

## Network Scan & Import

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
