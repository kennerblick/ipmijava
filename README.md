# ipmijava — Web-based IPMI Manager for Supermicro Servers

A self-hosted Docker web application for managing multiple Supermicro IPMI/BMC interfaces grouped by category.

## Features

| Feature | Details |
|---|---|
| Server groups | Organize servers in named groups |
| Online status | BMC reachability check (TCP 443/80/623) + power state via ipmitool |
| Power actions | Power ON · Soft OFF (ACPI) · Force OFF · Reset · Power Cycle |
| KVM Console | Opens Java iKVM (JNLP) or HTML5 KVM in new tab |
| Network scan | Discovers IPMI/BMC devices in a subnet (nmap preferred, socket fallback) |
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
```

## KVM Console

- **Java iKVM** — Requires Java + Java Web Start on the client. Opens `https://<ip>/cgi/url_redirect.cgi?url_name=ikvm`
- **HTML5 KVM** — Supported by newer Supermicro BMC firmware. Opens `https://<ip>/kvm.html`
- **BMC Web UI** — Direct link to `https://<ip>`

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
