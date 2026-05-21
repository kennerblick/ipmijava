'use strict';

const API = '/api';

const state = {
  groups: [],
  statuses: {},        // { [serverId]: { online, power, loading } }
  selectedGroup: null,
  pollTimer: null,
};

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, opts);
  if (r.status === 204) return null;
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const apiGet    = (p)    => apiFetch('GET',    p);
const apiPost   = (p, b) => apiFetch('POST',   p, b);
const apiPut    = (p, b) => apiFetch('PUT',    p, b);
const apiDelete = (p)    => apiFetch('DELETE', p);


// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function loadGroups() {
  try {
    state.groups = await apiGet('/groups');
  } catch (e) {
    state.groups = [];
    showToast('Fehler beim Laden der Konfiguration: ' + e.message, 'danger');
  }
  renderSidebar();
  renderGrid();
  scheduleStatusPoll();
}

function scheduleStatusPoll() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  pollAllServers();
  state.pollTimer = setInterval(pollAllServers, 30_000);
}

function pollAllServers() {
  state.groups.flatMap(g => g.servers).forEach(s => fetchStatus(s.id));
}

function refreshAll() {
  pollAllServers();
  showToast('Status-Abfrage gestartet…', 'secondary');
}


// ── Sidebar ──────────────────────────────────────────────────────────────────

function renderSidebar() {
  const el = document.getElementById('sidebar');
  if (state.groups.length === 0) {
    el.innerHTML = '<p class="text-muted small px-1 mt-2">Noch keine Gruppen.<br>Oben "+ Gruppe" klicken.</p>';
    return;
  }
  el.innerHTML = state.groups.map(g => {
    const active = state.selectedGroup === g.id ? ' active' : '';
    const count  = g.servers.length;
    return `
      <div class="sidebar-group${active}">
        <div class="sidebar-row" onclick="selectGroup('${g.id}')">
          <i class="bi bi-server text-secondary"></i>
          <span class="flex-grow-1 text-truncate">${esc(g.name)}</span>
          <span class="badge bg-secondary">${count}</span>
          <button class="sb-btn" onclick="event.stopPropagation();editGroup('${g.id}')" title="Umbenennen">
            <i class="bi bi-pencil"></i>
          </button>
          <button class="sb-btn text-danger" onclick="event.stopPropagation();deleteGroup('${g.id}')" title="Löschen">
            <i class="bi bi-trash"></i>
          </button>
        </div>
      </div>`;
  }).join('');
}

function selectGroup(id) {
  state.selectedGroup = (state.selectedGroup === id) ? null : id;
  renderSidebar();
  renderGrid();
}


// ── Server Grid ───────────────────────────────────────────────────────────────

function renderGrid() {
  const el = document.getElementById('server-grid');
  const groups = state.selectedGroup
    ? state.groups.filter(g => g.id === state.selectedGroup)
    : state.groups;

  if (groups.length === 0) {
    el.innerHTML = `
      <div class="text-center text-muted mt-5 pt-5">
        <i class="bi bi-cpu fs-1 d-block mb-3 opacity-25"></i>
        <p>Keine Gruppen vorhanden. Oben <strong>+ Gruppe</strong> klicken.</p>
      </div>`;
    return;
  }

  el.innerHTML = groups.map(g => `
    <div class="mb-5" id="group-section-${g.id}">
      <div class="d-flex align-items-center gap-2 mb-3">
        <h5 class="mb-0">${esc(g.name)}</h5>
        <button class="btn btn-sm btn-outline-primary" onclick="showAddServerModal('${g.id}')">
          <i class="bi bi-plus-lg"></i> Server
        </button>
      </div>
      <div class="row g-3" id="group-grid-${g.id}">
        ${g.servers.length === 0
          ? `<div class="col-12"><p class="text-muted small">Keine Server. "+ Server" klicken.</p></div>`
          : g.servers.map(s => serverCardHtml(s, state.statuses[s.id] || {}, g.id)).join('')}
      </div>
    </div>`).join('');
}

function serverCardHtml(server, status, groupId) {
  const { online, power, loading } = status;

  let badge;
  if (loading) {
    badge = `<span class="badge bg-secondary"><span class="spinner-border spinner-border-sm me-1" style="width:.7rem;height:.7rem;"></span>…</span>`;
  } else if (online === undefined) {
    badge = `<span class="badge bg-secondary">Unbekannt</span>`;
  } else if (!online) {
    badge = `<span class="badge bg-danger"><i class="bi bi-x-circle-fill me-1"></i>Offline</span>`;
  } else {
    badge = `<span class="badge bg-success"><i class="bi bi-check-circle-fill me-1"></i>Online</span>`;
  }

  const powerBadge = online && power === 'on'
    ? `<span class="badge bg-success ms-1">EIN</span>`
    : online && power === 'off'
      ? `<span class="badge bg-warning text-dark ms-1">AUS</span>`
      : '';

  const dis = (!online || loading) ? ' disabled' : '';

  return `
    <div class="col-xxl-3 col-xl-4 col-lg-6 col-md-6" id="card-wrap-${server.id}">
      <div class="card server-card h-100">
        <div class="card-body d-flex flex-column gap-2">
          <div class="d-flex justify-content-between align-items-start">
            <span class="fw-semibold text-truncate me-2" title="${esc(server.name)}">${esc(server.name)}</span>
            <div class="d-flex flex-shrink-0">${badge}${powerBadge}</div>
          </div>
          <div class="text-muted small">
            <i class="bi bi-hdd-network me-1"></i>${esc(server.ip)}
            ${server.description ? `<br><i class="bi bi-info-circle me-1"></i>${esc(server.description)}` : ''}
          </div>

          <!-- Power actions -->
          <div class="btn-group btn-group-sm mt-auto" role="group">
            <button class="btn btn-success${dis}" onclick="powerAction('${server.id}','on')" title="Power ON">
              <i class="bi bi-power"></i> ON
            </button>
            <button class="btn btn-warning${dis}" onclick="powerAction('${server.id}','soft')" title="Soft OFF (ACPI)">
              <i class="bi bi-moon-fill"></i> Soft
            </button>
            <button class="btn btn-danger${dis}" onclick="powerAction('${server.id}','forceoff')" title="Force OFF">
              <i class="bi bi-slash-circle"></i> Force
            </button>
            <button class="btn btn-info${dis}" onclick="powerAction('${server.id}','reset')" title="Reset">
              <i class="bi bi-arrow-clockwise"></i> Reset
            </button>
            <button class="btn btn-outline-danger${dis}" onclick="powerAction('${server.id}','cycle')" title="Power Cycle (Hard Reset)">
              <i class="bi bi-lightning-charge-fill"></i>
            </button>
          </div>

          <!-- Secondary actions -->
          <div class="d-flex gap-1">
            <button class="btn btn-sm btn-outline-info flex-grow-1" onclick="openConsole('${server.id}')" title="KVM Konsole">
              <i class="bi bi-display"></i> KVM
            </button>
            <button class="btn btn-sm btn-outline-secondary" onclick="fetchStatus('${server.id}')" title="Status aktualisieren">
              <i class="bi bi-arrow-clockwise"></i>
            </button>
            <button class="btn btn-sm btn-outline-secondary" onclick="editServer('${server.id}','${groupId}')" title="Bearbeiten">
              <i class="bi bi-pencil"></i>
            </button>
            <button class="btn btn-sm btn-outline-danger" onclick="deleteServer('${server.id}')" title="Löschen">
              <i class="bi bi-trash"></i>
            </button>
          </div>
        </div>
      </div>
    </div>`;
}

function updateCardInPlace(serverId) {
  const wrap = document.getElementById(`card-wrap-${serverId}`);
  if (!wrap) return;
  for (const g of state.groups) {
    const srv = g.servers.find(s => s.id === serverId);
    if (srv) {
      wrap.outerHTML = serverCardHtml(srv, state.statuses[serverId] || {}, g.id);
      return;
    }
  }
}


// ── Status ────────────────────────────────────────────────────────────────────

async function fetchStatus(serverId) {
  state.statuses[serverId] = { ...(state.statuses[serverId] || {}), loading: true };
  updateCardInPlace(serverId);
  try {
    const s = await apiGet(`/servers/${serverId}/status`);
    state.statuses[serverId] = s;
  } catch {
    state.statuses[serverId] = { online: false, power: 'unknown' };
  }
  updateCardInPlace(serverId);
}


// ── Power actions ─────────────────────────────────────────────────────────────

const POWER_LABELS = {
  on:       'Power ON',
  soft:     'Soft OFF (ACPI)',
  forceoff: 'Force OFF',
  reset:    'Reset',
  cycle:    'Power Cycle (Hard Reset)',
};

async function powerAction(serverId, action) {
  if (['forceoff', 'cycle'].includes(action)) {
    if (!confirm(`${POWER_LABELS[action]} wirklich ausführen?`)) return;
  }
  try {
    const r = await apiPost(`/servers/${serverId}/power`, { action });
    if (r.success) {
      showToast(`${POWER_LABELS[action]} ausgeführt.`, 'success');
      setTimeout(() => fetchStatus(serverId), 4000);
    } else {
      showToast(`Fehler: ${r.error || 'Unbekannte Fehlerantwort'}`, 'danger');
    }
  } catch (e) {
    showToast(`Fehler: ${e.message}`, 'danger');
  }
}


// ── KVM Console ───────────────────────────────────────────────────────────────

async function openConsole(serverId) {
  try {
    const data = await apiGet(`/servers/${serverId}/console-url`);
    const srv  = state.groups.flatMap(g => g.servers).find(s => s.id === serverId);

    document.getElementById('console-server-name').textContent = srv?.name ?? serverId;

    // Opens our relay page in a new tab — relay logs browser into BMC and opens man_ikvm
    const jnlpBtn = document.getElementById('console-ikvm-btn');
    jnlpBtn.href   = data.jnlp_proxy;
    jnlpBtn.target = '_blank';

    document.getElementById('console-bmc-btn').href = data.bmc_url;

    new bootstrap.Modal(document.getElementById('consoleModal')).show();
  } catch (e) {
    showToast(`Fehler: ${e.message}`, 'danger');
  }
}


// ── Group CRUD ────────────────────────────────────────────────────────────────

function showAddGroupModal() {
  document.getElementById('group-id-input').value   = '';
  document.getElementById('group-name-input').value = '';
  document.getElementById('group-modal-title').textContent = 'Neue Gruppe';
  new bootstrap.Modal(document.getElementById('groupModal')).show();
  setTimeout(() => document.getElementById('group-name-input').focus(), 300);
}

function editGroup(id) {
  const g = state.groups.find(x => x.id === id);
  if (!g) return;
  document.getElementById('group-id-input').value   = id;
  document.getElementById('group-name-input').value = g.name;
  document.getElementById('group-modal-title').textContent = 'Gruppe umbenennen';
  new bootstrap.Modal(document.getElementById('groupModal')).show();
  setTimeout(() => document.getElementById('group-name-input').focus(), 300);
}

async function saveGroup() {
  const name = document.getElementById('group-name-input').value.trim();
  const id   = document.getElementById('group-id-input').value;
  if (!name) { alert('Gruppenname erforderlich'); return; }
  try {
    if (id) { await apiPut(`/groups/${id}`, { name }); }
    else    { await apiPost('/groups', { name }); }
    bootstrap.Modal.getInstance(document.getElementById('groupModal')).hide();
    await loadGroups();
  } catch (e) {
    showToast(`Fehler: ${e.message}`, 'danger');
  }
}

async function deleteGroup(id) {
  const g = state.groups.find(x => x.id === id);
  if (!confirm(`Gruppe "${g?.name}" und alle enthaltenen Server wirklich löschen?`)) return;
  try {
    await apiDelete(`/groups/${id}`);
    if (state.selectedGroup === id) state.selectedGroup = null;
    await loadGroups();
  } catch (e) {
    showToast(`Fehler: ${e.message}`, 'danger');
  }
}


// ── Server CRUD ───────────────────────────────────────────────────────────────

function showAddServerModal(groupId) {
  document.getElementById('server-id-input').value   = '';
  document.getElementById('server-group-id').value   = groupId;
  document.getElementById('server-modal-title').textContent = 'Neuer Server';
  document.getElementById('server-name-input').value = '';
  document.getElementById('server-ip-input').value   = '';
  document.getElementById('server-user-input').value = 'ADMIN';
  document.getElementById('server-pass-input').value = '';
  document.getElementById('server-desc-input').value = '';
  new bootstrap.Modal(document.getElementById('serverModal')).show();
  setTimeout(() => document.getElementById('server-name-input').focus(), 300);
}

function editServer(serverId, groupId) {
  let srv = null;
  for (const g of state.groups) {
    srv = g.servers.find(s => s.id === serverId);
    if (srv) break;
  }
  if (!srv) return;
  document.getElementById('server-id-input').value   = serverId;
  document.getElementById('server-group-id').value   = groupId;
  document.getElementById('server-modal-title').textContent = 'Server bearbeiten';
  document.getElementById('server-name-input').value = srv.name;
  document.getElementById('server-ip-input').value   = srv.ip;
  document.getElementById('server-user-input').value = srv.username;
  document.getElementById('server-pass-input').value = srv.password;
  document.getElementById('server-desc-input').value = srv.description || '';
  new bootstrap.Modal(document.getElementById('serverModal')).show();
}

async function saveServer() {
  const id      = document.getElementById('server-id-input').value;
  const groupId = document.getElementById('server-group-id').value;
  const data = {
    name:        document.getElementById('server-name-input').value.trim(),
    ip:          document.getElementById('server-ip-input').value.trim(),
    username:    document.getElementById('server-user-input').value.trim(),
    password:    document.getElementById('server-pass-input').value,
    description: document.getElementById('server-desc-input').value.trim(),
  };
  if (!data.name || !data.ip) { alert('Name und IP erforderlich'); return; }
  try {
    if (id) { await apiPut(`/servers/${id}`, data); }
    else    { await apiPost(`/groups/${groupId}/servers`, data); }
    bootstrap.Modal.getInstance(document.getElementById('serverModal')).hide();
    await loadGroups();
  } catch (e) {
    showToast(`Fehler: ${e.message}`, 'danger');
  }
}

async function deleteServer(id) {
  if (!confirm('Server wirklich löschen?')) return;
  try {
    await apiDelete(`/servers/${id}`);
    await loadGroups();
  } catch (e) {
    showToast(`Fehler: ${e.message}`, 'danger');
  }
}


// ── Network Scan ──────────────────────────────────────────────────────────────

function showScanModal() {
  document.getElementById('scan-network-input').value = '';
  document.getElementById('scan-results').innerHTML   = '';
  const btn = document.getElementById('scan-btn');
  btn.disabled = false;
  btn.innerHTML = '<i class="bi bi-radar"></i> Scannen';
  new bootstrap.Modal(document.getElementById('scanModal')).show();
  setTimeout(() => document.getElementById('scan-network-input').focus(), 300);
}

async function startScan() {
  const network = document.getElementById('scan-network-input').value.trim();
  if (!network) { alert('Netzwerk eingeben, z.B. 192.168.1.0/24'); return; }

  const btn = document.getElementById('scan-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Scanne…';
  document.getElementById('scan-results').innerHTML =
    '<p class="text-muted"><span class="spinner-border spinner-border-sm me-2"></span>Suche nach IPMI/BMC-Geräten…</p>';

  try {
    const r = await apiFetch('POST', '/scan', { network });
    renderScanResults(r);
  } catch (e) {
    document.getElementById('scan-results').innerHTML =
      `<div class="alert alert-danger">Fehler: ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-radar"></i> Erneut scannen';
  }
}

function renderScanResults({ found, total, method }) {
  const el = document.getElementById('scan-results');
  const methodLabel = method === 'nmap' ? 'nmap' : 'Socket-Scan';

  if (!found || found.length === 0) {
    el.innerHTML = `
      <div class="alert alert-secondary">
        Keine IPMI/BMC-Geräte gefunden (${total} Hosts geprüft, ${methodLabel}).
      </div>`;
    return;
  }

  const rows = found.map(ip => `
    <div class="d-flex align-items-center justify-content-between py-2 px-3 border rounded mb-1 bg-body-secondary">
      <span><i class="bi bi-hdd-network text-success me-2"></i><code>${esc(ip)}</code></span>
      <button class="btn btn-sm btn-outline-primary" onclick="prefillFromScan('${esc(ip)}')">
        <i class="bi bi-plus-lg"></i> Hinzufügen
      </button>
    </div>`).join('');

  el.innerHTML = `
    <div class="text-success small mb-2">
      <i class="bi bi-check-circle-fill me-1"></i>
      ${found.length} Gerät(e) gefunden von ${total} Hosts (${methodLabel})
    </div>
    ${rows}`;
}

function prefillFromScan(ip) {
  bootstrap.Modal.getInstance(document.getElementById('scanModal')).hide();
  if (state.groups.length === 0) {
    alert('Zuerst eine Gruppe erstellen.');
    showAddGroupModal();
    return;
  }
  const groupId = state.selectedGroup ?? state.groups[0].id;
  showAddServerModal(groupId);
  document.getElementById('server-ip-input').value   = ip;
  document.getElementById('server-name-input').value = `BMC-${ip.split('.').pop()}`;
}


// ── Utilities ─────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function togglePassword(inputId, btn) {
  const inp = document.getElementById(inputId);
  const show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  btn.innerHTML = `<i class="bi bi-eye${show ? '-slash' : ''}"></i>`;
}

function showToast(msg, type = 'info') {
  const c = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast align-items-center text-bg-${type} border-0`;
  el.setAttribute('role', 'alert');
  el.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">${esc(msg)}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>`;
  c.appendChild(el);
  const t = new bootstrap.Toast(el, { delay: 4500 });
  t.show();
  el.addEventListener('hidden.bs.toast', () => el.remove());
}


// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', loadGroups);
