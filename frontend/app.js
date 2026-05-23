'use strict';

const API = '/api';

const state = {
  groups: [],
  statuses: {},        // { [serverId]: { online, power, loading } }
  selectedGroup: null,
  pollTimer: null,
  lastScanIps: [],
  auth: { username: null, is_ipmi_user: false },
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
  if (r.status === 403) {
    showLoginModal();
    throw new Error('Nicht autorisiert — bitte anmelden');
  }
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

async function pollAllServers() {
  const servers = state.groups.flatMap(g => g.servers);
  if (!servers.length) return;

  // Mark all loading immediately so spinners appear before the request fires
  servers.forEach(s => {
    state.statuses[s.id] = { ...(state.statuses[s.id] || {}), loading: true };
    updateCardInPlace(s.id);
  });

  try {
    const result = await apiGet('/bulk-status');
    for (const [sid, status] of Object.entries(result)) {
      state.statuses[sid] = status;
      updateCardInPlace(sid);
    }
  } catch {
    // Fallback: mark all offline on error
    servers.forEach(s => {
      state.statuses[s.id] = { online: false, power: 'unknown' };
      updateCardInPlace(s.id);
    });
  }
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
  const ipmi = state.auth.is_ipmi_user;
  el.innerHTML = state.groups.map(g => {
    const active = state.selectedGroup === g.id ? ' active' : '';
    const count  = g.servers.length;
    const editDel = ipmi ? `
          <button class="sb-btn" onclick="event.stopPropagation();editGroup('${g.id}')" title="Umbenennen">
            <i class="bi bi-pencil"></i>
          </button>
          <button class="sb-btn text-danger" onclick="event.stopPropagation();deleteGroup('${g.id}')" title="Löschen">
            <i class="bi bi-trash"></i>
          </button>` : '';
    return `
      <div class="sidebar-group${active}">
        <div class="sidebar-row" onclick="selectGroup('${g.id}')">
          <i class="bi bi-server text-secondary"></i>
          <span class="flex-grow-1 text-truncate">${esc(g.name)}</span>
          <span class="badge bg-secondary">${count}</span>
          ${editDel}
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

  const canEdit = state.auth.is_ipmi_user;
  el.innerHTML = groups.map(g => `
    <div class="mb-5" id="group-section-${g.id}">
      <div class="d-flex align-items-center gap-2 mb-3">
        <h5 class="mb-0">${esc(g.name)}</h5>
        ${canEdit ? `<button class="btn btn-sm btn-outline-primary" onclick="showAddServerModal('${g.id}')">
          <i class="bi bi-plus-lg"></i> Server
        </button>` : ''}
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
  const caps = server.caps;           // undefined = not yet probed
  const probing = status.probing;

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

  const dis   = (!online || loading) ? ' disabled' : '';
  const ipmi  = state.auth.is_ipmi_user;

  // caps === undefined  →  not yet probed, show all
  // caps defined        →  show only supported features
  const showIpmi    = !caps || caps.ipmi;
  const showKvm     = !caps || caps.kvm_aten;
  const showConsole = !caps || caps.kvm_aten || caps.ikvm_java || caps.bmc_http;

  const powerGroup = ipmi && showIpmi ? `
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
          </div>` : '';

  const probeBtn = ipmi
    ? (probing
        ? `<button class="btn btn-sm btn-outline-warning disabled" title="Erkenne Funktionen…"><span class="spinner-border spinner-border-sm" style="width:.65rem;height:.65rem;"></span></button>`
        : `<button class="btn btn-sm btn-outline-secondary" onclick="probeServer('${server.id}')" title="Funktionen neu erkennen"><i class="bi bi-cpu"></i></button>`)
    : '';

  const actionBtns = ipmi ? `
            ${showKvm     ? `<button class="btn btn-sm btn-outline-info" onclick="openKvm('${server.id}')" title="HTML5 KVM (Browser-Konsole)"><i class="bi bi-display"></i></button>` : ''}
            ${showConsole ? `<button class="btn btn-sm btn-outline-secondary" onclick="openConsole('${server.id}')" title="Konsolen-Optionen"><i class="bi bi-window"></i></button>` : ''}
            <button class="btn btn-sm btn-outline-secondary" onclick="fetchStatus('${server.id}')" title="Status aktualisieren"><i class="bi bi-arrow-clockwise"></i></button>
            ${probeBtn}
            <button class="btn btn-sm btn-outline-secondary" onclick="editServer('${server.id}','${groupId}')" title="Bearbeiten"><i class="bi bi-pencil"></i></button>
            <button class="btn btn-sm btn-outline-danger" onclick="deleteServer('${server.id}')" title="Löschen"><i class="bi bi-trash"></i></button>` : `
            <button class="btn btn-sm btn-outline-secondary" onclick="fetchStatus('${server.id}')" title="Status aktualisieren"><i class="bi bi-arrow-clockwise"></i></button>
            <button class="btn btn-sm btn-outline-secondary disabled" title="Anmelden für Aktionen"><i class="bi bi-lock"></i></button>`;

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
          ${powerGroup}
          <!-- Secondary actions -->
          <div class="d-flex gap-1 flex-wrap mt-auto">
            ${actionBtns}
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
    const caps = srv?.caps;   // undefined = not yet probed → show all

    document.getElementById('console-server-name').textContent = srv?.name ?? serverId;

    // HTML5 KVM button — show only if kvm_aten supported (or caps unknown)
    const html5Btn = document.getElementById('console-html5-btn');
    html5Btn.classList.toggle('d-none', caps && !caps.kvm_aten);
    html5Btn.onclick = () => {
      bootstrap.Modal.getInstance(document.getElementById('consoleModal')).hide();
      openKvm(serverId);
    };

    // Java iKVM relay — show only if ikvm_java supported (or caps unknown)
    const jnlpBtn = document.getElementById('console-ikvm-btn');
    jnlpBtn.classList.toggle('d-none', caps && !caps.ikvm_java);
    jnlpBtn.href   = data.jnlp_proxy;
    jnlpBtn.target = '_blank';

    // Java fix — only useful with Java iKVM
    document.getElementById('console-java-fix-btn').classList.toggle('d-none', caps && !caps.ikvm_java);
    document.getElementById('console-java-fix-btn').href = data.java_fix;

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

function _setGroupSelectRow(visible, currentGroupId) {
  const row = document.getElementById('server-group-select-row');
  row.classList.toggle('d-none', !visible);
  if (!visible) return;
  const sel = document.getElementById('server-group-select');
  sel.innerHTML = state.groups.map(g =>
    `<option value="${esc(g.id)}"${g.id === currentGroupId ? ' selected' : ''}>${esc(g.name)}</option>`
  ).join('');
}

function showAddServerModal(groupId) {
  document.getElementById('server-id-input').value   = '';
  document.getElementById('server-group-id').value   = groupId;
  document.getElementById('server-modal-title').textContent = 'Neuer Server';
  document.getElementById('server-name-input').value = '';
  document.getElementById('server-ip-input').value   = '';
  document.getElementById('server-user-input').value = 'ADMIN';
  document.getElementById('server-pass-input').value = '';
  document.getElementById('server-desc-input').value = '';
  _setGroupSelectRow(false, groupId);
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
  _setGroupSelectRow(true, groupId);
  new bootstrap.Modal(document.getElementById('serverModal')).show();
}

async function saveServer() {
  const id             = document.getElementById('server-id-input').value;
  const origGroupId    = document.getElementById('server-group-id').value;
  const selectedGroup  = document.getElementById('server-group-select');
  const targetGroupId  = (id && !selectedGroup.closest('.d-none')) ? selectedGroup.value : origGroupId;
  const data = {
    name:        document.getElementById('server-name-input').value.trim(),
    ip:          document.getElementById('server-ip-input').value.trim(),
    username:    document.getElementById('server-user-input').value.trim(),
    password:    document.getElementById('server-pass-input').value,
    description: document.getElementById('server-desc-input').value.trim(),
  };
  if (!data.name || !data.ip) { alert('Name und IP erforderlich'); return; }
  if (id && targetGroupId !== origGroupId) data.group_id = targetGroupId;
  try {
    let saved;
    if (id) { saved = await apiPut(`/servers/${id}`, data); }
    else    { saved = await apiPost(`/groups/${origGroupId}/servers`, data); }
    bootstrap.Modal.getInstance(document.getElementById('serverModal')).hide();
    await loadGroups();
    // Trigger capability probe in background
    const probeId = id || saved.id;
    showToast('Erkenne Server-Funktionen…', 'secondary');
    state.statuses[probeId] = { ...(state.statuses[probeId] || {}), probing: true };
    updateCardInPlace(probeId);
    apiPost(`/servers/${probeId}/probe`).then(caps => {
      for (const g of state.groups) {
        const srv = g.servers.find(s => s.id === probeId);
        if (srv) { srv.caps = caps; break; }
      }
      state.statuses[probeId] = { ...(state.statuses[probeId] || {}), probing: false };
      updateCardInPlace(probeId);
      showToast(`Funktionen: ${_capsLabel(caps)}`, 'success');
    }).catch(() => {
      state.statuses[probeId] = { ...(state.statuses[probeId] || {}), probing: false };
      updateCardInPlace(probeId);
    });
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

async function probeServer(serverId) {
  state.statuses[serverId] = { ...(state.statuses[serverId] || {}), probing: true };
  updateCardInPlace(serverId);
  try {
    const caps = await apiPost(`/servers/${serverId}/probe`);
    // Update the caps on the in-memory server object so the card re-renders correctly
    for (const g of state.groups) {
      const srv = g.servers.find(s => s.id === serverId);
      if (srv) { srv.caps = caps; break; }
    }
    showToast(`Funktionen erkannt: ${_capsLabel(caps)}`, 'success');
  } catch (e) {
    showToast(`Probe-Fehler: ${e.message}`, 'danger');
  } finally {
    state.statuses[serverId] = { ...(state.statuses[serverId] || {}), probing: false };
    updateCardInPlace(serverId);
  }
}

function _capsLabel(caps) {
  const parts = [];
  if (caps.ipmi)      parts.push('IPMI');
  if (caps.kvm_aten)  parts.push('HTML5-KVM');
  if (caps.ikvm_java) parts.push('Java-iKVM');
  if (caps.bmc_http)  parts.push('BMC-Web');
  return parts.length ? parts.join(', ') : 'keine';
}


// ── Network Scan ──────────────────────────────────────────────────────────────

function showScanModal() {
  document.getElementById('scan-network-input').value = '';
  document.getElementById('scan-results').innerHTML   = '';
  document.getElementById('scan-import-all-btn').classList.add('d-none');
  state.lastScanIps = [];
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
  const importBtn = document.getElementById('scan-import-all-btn');
  const methodLabel = method === 'nmap' ? 'nmap' : 'Socket-Scan';

  state.lastScanIps = found || [];
  importBtn.classList.toggle('d-none', state.lastScanIps.length === 0);

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

async function importAllFromScan() {
  if (state.lastScanIps.length === 0) return;
  const btn = document.getElementById('scan-import-all-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Importiere…';
  try {
    const r = await apiPost('/scan/import', { ips: state.lastScanIps });
    bootstrap.Modal.getInstance(document.getElementById('scanModal')).hide();
    await loadGroups();
    showToast(
      `${r.created} Server importiert${r.skipped ? `, ${r.skipped} bereits vorhanden` : ''} → Gruppe "Ungruppiert"`,
      'success'
    );
  } catch (e) {
    showToast(`Fehler beim Import: ${e.message}`, 'danger');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-cloud-download"></i> Alle importieren';
  }
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


// ── KVM Console (Java iKVM via TigerVNC → noVNC) ─────────────────────────────

let _kvmPollTimer = null;
let _kvmServerId  = null;

async function openKvm(serverId) {
  _kvmServerId = serverId;

  const srv = state.groups.flatMap(g => g.servers).find(s => s.id === serverId);
  document.getElementById('kvm-server-name').textContent = srv?.name ?? serverId;

  const badge = document.getElementById('kvm-status-badge');
  badge.className = 'badge bg-secondary';
  badge.textContent = 'Starte…';

  const frame = document.getElementById('kvm-frame');
  frame.src = '';

  document.getElementById('kvm-newtab-btn').href = `${API}/servers/${serverId}/kvm-viewer`;

  const modal = new bootstrap.Modal(document.getElementById('kvmModal'));
  modal.show();

  // Start (or reuse) a KVM session
  try {
    await apiPost(`/servers/${serverId}/kvm-session`, {});
  } catch (e) {
    badge.className = 'badge bg-danger';
    badge.textContent = 'Fehler';
    showToast(`KVM Fehler: ${e.message}`, 'danger');
    return;
  }

  _kvmPollTimer = setInterval(() => _pollKvmStatus(serverId, frame, badge), 900);
}

async function _pollKvmStatus(serverId, frame, badge) {
  try {
    const s = await apiGet(`/servers/${serverId}/kvm-session`);

    if (s.status === 'running' && !frame.src.includes('kvm-viewer')) {
      clearInterval(_kvmPollTimer);
      badge.className = 'badge bg-success';
      badge.textContent = 'Verbunden';
      frame.src = `${API}/servers/${serverId}/kvm-viewer`;
    } else if (s.status === 'error') {
      clearInterval(_kvmPollTimer);
      badge.className = 'badge bg-danger';
      badge.textContent = 'Fehler';
      showToast(`KVM: ${s.error}`, 'danger');
    } else if (s.status === 'stopped' || s.status === 'none') {
      clearInterval(_kvmPollTimer);
      badge.className = 'badge bg-secondary';
      badge.textContent = 'Beendet';
    } else {
      badge.textContent = s.message || s.status;
    }
  } catch (_) {
    clearInterval(_kvmPollTimer);
  }
}

function closeKvm() {
  if (_kvmPollTimer) { clearInterval(_kvmPollTimer); _kvmPollTimer = null; }
  document.getElementById('kvm-frame').src = '';
  if (_kvmServerId) {
    // Stop the backend KVM session when modal closes
    apiDelete(`/servers/${_kvmServerId}/kvm-session`).catch(() => {});
    _kvmServerId = null;
  }
}


// ── Auth ──────────────────────────────────────────────────────────────────────

async function loadAuth() {
  try {
    const me = await fetch(API + '/me').then(r => r.json());
    state.auth = { username: me.username, is_ipmi_user: me.is_ipmi_user };
  } catch {
    state.auth = { username: null, is_ipmi_user: false };
  }
  _updateAuthUI();
}

function _updateAuthUI() {
  const nav   = document.getElementById('auth-nav');
  const scanB = document.getElementById('nav-scan-btn');
  const grpB  = document.getElementById('nav-gruppe-btn');
  const ipmi  = state.auth.is_ipmi_user;

  scanB.classList.toggle('d-none', !ipmi);
  grpB.classList.toggle('d-none',  !ipmi);

  if (state.auth.username) {
    const roleTag = ipmi
      ? `<span class="badge bg-success ms-1" title="Mitglied der IPMIUser-Gruppe">IPMIUser</span>`
      : `<span class="badge bg-secondary ms-1" title="Kein Mitglied der IPMIUser-Gruppe">Lesezugriff</span>`;
    nav.innerHTML = `
      <span class="text-muted small d-flex align-items-center gap-1">
        <i class="bi bi-person-check text-success"></i>${esc(state.auth.username)}${roleTag}
      </span>
      <button class="btn btn-sm btn-outline-secondary" onclick="doLogout()" title="Abmelden">
        <i class="bi bi-box-arrow-right"></i>
      </button>`;
  } else {
    nav.innerHTML = `
      <button class="btn btn-sm btn-outline-primary" onclick="showLoginModal()">
        <i class="bi bi-person me-1"></i>Anmelden
      </button>`;
  }

  // Re-render cards so action buttons appear/disappear
  renderSidebar();
  renderGrid();
}

function showLoginModal() {
  document.getElementById('login-user-input').value = '';
  document.getElementById('login-pass-input').value = '';
  document.getElementById('login-error').classList.add('d-none');
  document.getElementById('login-btn').disabled = false;
  document.getElementById('login-btn').innerHTML = '<i class="bi bi-box-arrow-in-right me-1"></i>Anmelden';
  const m = new bootstrap.Modal(document.getElementById('loginModal'));
  m.show();
  setTimeout(() => document.getElementById('login-user-input').focus(), 300);
}

async function doLogin() {
  const username = document.getElementById('login-user-input').value.trim();
  const password = document.getElementById('login-pass-input').value;
  const errEl    = document.getElementById('login-error');
  const btn      = document.getElementById('login-btn');

  errEl.classList.add('d-none');
  if (!username || !password) {
    errEl.textContent = 'Benutzername und Passwort erforderlich';
    errEl.classList.remove('d-none');
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Prüfe…';

  try {
    const me = await fetch(API + '/login', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ username, password }),
    }).then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      return d;
    });
    state.auth = { username: me.username, is_ipmi_user: me.is_ipmi_user };
    bootstrap.Modal.getInstance(document.getElementById('loginModal')).hide();
    _updateAuthUI();
    if (!me.is_ipmi_user) {
      showToast(`Angemeldet als ${me.username} — kein Mitglied der IPMIUser-Gruppe, nur Lesezugriff`, 'warning');
    } else {
      showToast(`Angemeldet als ${me.username}`, 'success');
    }
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('d-none');
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-box-arrow-in-right me-1"></i>Anmelden';
  }
}

async function doLogout() {
  try {
    await fetch(API + '/logout', { method: 'POST' });
  } catch { /* ignore */ }
  state.auth = { username: null, is_ipmi_user: false };
  _updateAuthUI();
  showToast('Abgemeldet', 'secondary');
}


// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  await loadAuth();
  await loadGroups();
});
