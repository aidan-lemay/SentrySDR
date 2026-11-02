<script>
  import { onMount } from 'svelte';
  import { api, formatMHz, formatTime } from '../lib.js';

  const TABS = [['users', 'Users'], ['sources', 'SDR sources'], ['nodes', 'Nodes'], ['bans', 'Bans'], ['audit', 'Audit log'], ['settings', 'Settings']];
  const PERMISSIONS = [
    ['listen_live', 'Listen live'], ['listen_recordings', 'Play recordings'], ['download', 'Download'],
    ['assign', 'Assign & schedule'], ['manage_targets', 'Manage targets'], ['protect', 'Protect recordings'], ['maps', 'Maps'],
  ];

  let tab = $state('users');
  let error = $state('');
  let message = $state('');

  let users = $state([]);
  let selected = $state(null);
  let userSessions = $state([]);
  let oneTimeSecret = $state(null);

  let sources = $state([]);
  let available = $state([]);
  let nodes = $state([]);
  const blankSource = () => ({ id: null, key: '', name: '', description: '', antenna: '', min: '', max: '', gain: '', ppm: 0 });
  let sourceForm = $state(blankSource());

  let nodeName = $state('');
  let enrolment = $state(null);

  let bans = $state([]);
  let ban = $state({ kind: 'ip', value: '', reason: '' });

  let audit = $state([]);
  let auditSearch = $state('');

  let settings = $state(null);
  let keys = $state({ rdio_api_key: '', clearcut_api_key: '' });

  const unused = $derived(available.filter((item) => !item.defined));

  async function run(action, success) {
    error = '';
    message = '';
    try {
      const reply = await action();
      if (success) message = success;
      return reply ?? true;
    } catch (failure) {
      error = failure.message;
      return null;
    }
  }

  const loaders = {
    users: async () => { users = await api('admin/users'); },
    sources: async () => { [sources, available] = await Promise.all([api('sources'), api('sources/available')]); },
    nodes: async () => { nodes = await api('nodes'); },
    bans: async () => { bans = await api('admin/bans'); },
    audit: async () => { audit = await api(`admin/audit?limit=200${auditSearch ? `&search=${encodeURIComponent(auditSearch)}` : ''}`); },
    settings: async () => { settings = await api('admin/settings'); },
  };

  function open(next) {
    tab = next;
    message = '';
    selected = null;
    oneTimeSecret = null;
    enrolment = null;
    run(loaders[next]);
  }

  onMount(() => open('users'));

  // --- users
  async function updateUser(user, body, success) {
    if (await run(() => api(`admin/users/${user.id}`, { method: 'PATCH', body }), success)) {
      await loaders.users();
      if (selected?.id === user.id) selected = structuredClone($state.snapshot(users.find((u) => u.id === user.id)));
    }
  }

  async function selectUser(user) {
    selected = structuredClone($state.snapshot(user));
    oneTimeSecret = null;
    const [sessions, history] = await Promise.all([
      api(`admin/users/${user.id}/sessions`).catch(() => []),
      api(`admin/audit?user_id=${user.id}&limit=50`).catch(() => []),
    ]);
    userSessions = sessions;
    selected.history = history;
  }

  async function resetPassword(user) {
    const reply = await run(() => api(`admin/users/${user.id}/reset-password`, { method: 'POST' }));
    if (reply) oneTimeSecret = reply.temporary_password;
  }

  const banUser = (user, kind, value) => run(async () => {
    await api('admin/bans', { method: 'POST', body: { kind, value, reason: `Banned from ${user.email}` } });
    await loaders.users();
  }, `Banned ${value}.`);

  function overrideState(name) {
    return selected.perm_overrides?.[name] === undefined ? 'default' : (selected.perm_overrides[name] ? 'allow' : 'deny');
  }

  function setOverride(user, name, value) {
    const next = { ...user.perm_overrides };
    if (value === 'default') delete next[name];
    else next[name] = value === 'allow';
    updateUser(user, { perm_overrides: next }, 'Permissions updated.');
  }

  // --- sources
  async function saveSource(event) {
    event.preventDefault();
    const [nodeId, ...serial] = sourceForm.key.split('|');
    const toHz = (mhz) => (mhz === '' ? null : Math.round(Number(mhz) * 1e6));
    const body = {
      node_id: Number(nodeId), serial: serial.join('|'), name: sourceForm.name, description: sourceForm.description,
      antenna: sourceForm.antenna, freq_min_hz: toHz(sourceForm.min), freq_max_hz: toHz(sourceForm.max),
      gain_db: sourceForm.gain === '' ? null : Number(sourceForm.gain), ppm: Number(sourceForm.ppm),
    };
    const done = await run(() => (sourceForm.id
      ? api(`sources/${sourceForm.id}`, { method: 'PUT', body })
      : api('sources', { method: 'POST', body })), 'Source saved.');
    if (done) {
      sourceForm = blankSource();
      await loaders.sources();
    }
  }

  function editSource(source) {
    sourceForm = {
      id: source.id, key: `${source.node_id}|${source.serial}`, name: source.name, description: source.description,
      antenna: source.antenna, min: source.freq_min_hz ? source.freq_min_hz / 1e6 : '', max: source.freq_max_hz ? source.freq_max_hz / 1e6 : '',
      gain: source.gain_db ?? '', ppm: source.ppm,
    };
  }

  const removeSource = (source) => run(async () => {
    await api(`sources/${source.id}`, { method: 'DELETE' });
    await loaders.sources();
  }, 'Source removed. Its recordings are kept.');

  // --- nodes
  async function addNode(event) {
    event.preventDefault();
    const reply = await run(() => api('nodes', { method: 'POST', body: { name: nodeName } }));
    if (reply) {
      enrolment = reply;
      nodeName = '';
      await loaders.nodes();
    }
  }

  const revokeNode = (node) => run(async () => {
    await api(`nodes/${node.id}`, { method: 'DELETE' });
    await loaders.nodes();
  }, `${node.name} revoked.`);

  // --- bans
  async function addBan(event) {
    event.preventDefault();
    const done = await run(() => api('admin/bans', { method: 'POST', body: ban }), 'Ban added.');
    if (done) {
      ban = { kind: ban.kind, value: '', reason: '' };
      await loaders.bans();
    }
  }

  const removeBan = (row) => run(async () => {
    await api(`admin/bans/${row.id}`, { method: 'DELETE' });
    await loaders.bans();
  }, 'Ban removed.');

  // --- settings
  async function saveSettings(event) {
    event.preventDefault();
    const body = {
      audit_retention_days: Number(settings.audit_retention_days),
      recording_retention_days: Number(settings.recording_retention_days),
      registration_open: settings.registration_open === 'true' || settings.registration_open === true,
      rdio_server: settings.rdio_server, clearcut_server: settings.clearcut_server,
      clearcut_page_url: settings.clearcut_page_url, map_tile_url: settings.map_tile_url,
    };
    for (const name of ['rdio_api_key', 'clearcut_api_key']) {
      if (keys[name]) body[name] = keys[name];
    }
    const reply = await run(() => api('admin/settings', { method: 'PATCH', body }), 'Settings saved.');
    if (reply) {
      settings = reply;
      keys = { rdio_api_key: '', clearcut_api_key: '' };
    }
  }

  const statusClass = (status) => ({ active: 'ok', pending: 'warn', denied: 'bad', disabled: 'bad', revoked: 'bad' }[status] ?? '');
</script>

<div class="page-head">
  <h1>Admin</h1>
  <div class="row" role="tablist">
    {#each TABS as [id, label] (id)}
      <button class="tab" class:active={tab === id} role="tab" aria-selected={tab === id} onclick={() => open(id)}>{label}</button>
    {/each}
  </div>
</div>

{#if error}<div class="notice error" role="alert">{error}</div>{/if}
{#if message}<div class="notice" role="status">{message}</div>{/if}

{#if tab === 'users'}
  <section class="panel table-wrap">
    <table>
      <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last seen from</th><th>Last sign-in</th><th></th></tr></thead>
      <tbody>
        {#each users as user (user.id)}
          <tr>
            <td>{user.name}</td>
            <td>{user.email}</td>
            <td>{user.role}</td>
            <td><span class="pill {statusClass(user.status)}">{user.status}</span></td>
            <td class="mono small">{user.last_ip ?? '—'}</td>
            <td class="small">{formatTime(user.last_login_at)}</td>
            <td>
              <div class="row">
                {#if user.status === 'pending'}
                  <button class="primary" onclick={() => updateUser(user, { status: 'active' }, `${user.email} approved.`)}>Approve</button>
                  <button class="danger" onclick={() => updateUser(user, { status: 'denied' }, `${user.email} denied.`)}>Deny</button>
                {/if}
                <button onclick={() => selectUser(user)}>Manage</button>
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </section>

  {#if selected}
    {@const original = users.find((u) => u.id === selected.id)}
    <section class="panel stack">
      <div class="row spread"><h2>{selected.email}</h2><button onclick={() => (selected = null)}>Close</button></div>
      {#if oneTimeSecret}
        <div class="notice warn">
          Temporary password, shown only once. Send it securely; they'll be made to change it.
          <div class="secret" style="margin-top:.35rem">{oneTimeSecret}</div>
        </div>
      {/if}
      <form class="fields" onsubmit={(event) => { event.preventDefault(); updateUser(original, { name: selected.name, email: selected.email, role: selected.role }, 'User saved.'); }}>
        <label>Name <input bind:value={selected.name} required /></label>
        <label>Email <input type="email" bind:value={selected.email} required /></label>
        <label>Role
          <select bind:value={selected.role}>
            <option value="listener">Listener</option>
            <option value="operator">Operator</option>
            <option value="admin">Admin</option>
          </select>
        </label>
        <button class="primary">Save</button>
      </form>

      {#if selected.role !== 'admin'}
        <h3>Permissions (overrides on top of the role)</h3>
        <div class="fields">
          {#each PERMISSIONS as [name, label] (name)}
            <label>{label}
              <select value={overrideState(name)} onchange={(event) => setOverride(original, name, event.target.value)}>
                <option value="default">Role default</option>
                <option value="allow">Allow</option>
                <option value="deny">Deny</option>
              </select>
            </label>
          {/each}
        </div>
      {/if}

      <div class="row">
        <button onclick={() => resetPassword(selected)}>Reset password</button>
        {#if selected.totp_enabled}<button onclick={() => run(() => api(`admin/users/${selected.id}/reset-totp`, { method: 'POST' }), 'Two-factor reset.')}>Reset 2FA</button>{/if}
        {#if selected.status === 'active'}
          <button class="danger" onclick={() => updateUser(original, { status: 'disabled' }, 'Account disabled.')}>Disable</button>
        {:else}
          <button onclick={() => updateUser(original, { status: 'active' }, 'Account enabled.')}>Enable</button>
        {/if}
        <button class="danger" onclick={() => banUser(selected, 'email', selected.email)}>Ban email</button>
        {#if selected.last_ip}<button class="danger" onclick={() => banUser(selected, 'ip', selected.last_ip)}>Ban IP {selected.last_ip}</button>{/if}
      </div>

      <div class="small muted">Requested from <span class="mono">{selected.signup_ip ?? '—'}</span> · created {formatTime(selected.created_at)}</div>

      <h3>Sessions</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Started</th><th>From</th><th>Browser</th></tr></thead>
          <tbody>
            {#each userSessions as row}
              <tr><td class="small">{formatTime(row.created_at)}</td><td class="mono small">{row.ip}</td><td class="small">{row.user_agent}</td></tr>
            {:else}
              <tr><td colspan="3" class="muted">No active sessions.</td></tr>
            {/each}
          </tbody>
        </table>
      </div>

      <h3>Audit log</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>When</th><th>Action</th><th>Subject</th><th>From</th></tr></thead>
          <tbody>
            {#each selected.history ?? [] as row (row.id)}
              <tr><td class="small">{formatTime(row.ts)}</td><td class="mono small">{row.action}</td><td class="small">{row.target ?? ''}</td><td class="mono small">{row.ip}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}
{/if}

{#if tab === 'sources'}
  <section class="panel table-wrap">
    <table>
      <thead><tr><th>Name</th><th>Node</th><th>Serial</th><th>Antenna</th><th>Range</th><th>Gain</th><th></th></tr></thead>
      <tbody>
        {#each sources as source (source.id)}
          <tr>
            <td>{source.name}</td>
            <td>{source.node_name}</td>
            <td class="mono small">{source.serial}</td>
            <td>{source.antenna}</td>
            <td class="mono small">{source.freq_min_hz ? `${formatMHz(source.freq_min_hz)} – ${formatMHz(source.freq_max_hz)}` : '—'}</td>
            <td class="small">{source.gain_db == null ? 'Auto (AGC)' : `${source.gain_db} dB`}</td>
            <td><div class="row"><button onclick={() => editSource(source)}>Edit</button><button class="danger" onclick={() => removeSource(source)}>Remove</button></div></td>
          </tr>
        {:else}
          <tr><td colspan="7" class="muted">No sources defined yet.</td></tr>
        {/each}
      </tbody>
    </table>
  </section>

  <form class="panel stack" onsubmit={saveSource}>
    <h2>{sourceForm.id ? 'Edit source' : 'Define a source'}</h2>
    {#if !sourceForm.id && !unused.length}
      <div class="notice warn small">No unused SDRs. Each node only offers the serials listed under <span class="mono">sdr.serials</span> in its configuration file.</div>
    {/if}
    <div class="fields">
      <label>SDR
        <select bind:value={sourceForm.key} required disabled={!!sourceForm.id}>
          <option value="" disabled>Choose an SDR</option>
          {#if sourceForm.id}<option value={sourceForm.key}>{sourceForm.key.split('|').slice(1).join('|')}</option>{/if}
          {#each unused as item (`${item.node_id}|${item.serial}`)}
            <option value={`${item.node_id}|${item.serial}`}>{item.serial} on {item.node_name}{item.connected ? '' : ' (not connected)'}</option>
          {/each}
        </select>
      </label>
      <label>Name <input bind:value={sourceForm.name} required maxlength="120" placeholder="Source 1" /></label>
      <label>Antenna type <input bind:value={sourceForm.antenna} maxlength="120" placeholder="380–460 MHz yagi" /></label>
      <label>Antenna min (MHz) <input type="number" step="0.001" min="0" bind:value={sourceForm.min} /></label>
      <label>Antenna max (MHz) <input type="number" step="0.001" min="0" bind:value={sourceForm.max} /></label>
      <label>Gain in dB (blank = AGC) <input type="number" step="0.1" min="0" max="60" bind:value={sourceForm.gain} /></label>
      <label>Frequency correction (ppm) <input type="number" min="-500" max="500" bind:value={sourceForm.ppm} /></label>
    </div>
    <label>Description <input bind:value={sourceForm.description} maxlength="500" /></label>
    <div class="row">
      <button class="primary">Save source</button>
      {#if sourceForm.id}<button type="button" onclick={() => (sourceForm = blankSource())}>Cancel</button>{/if}
    </div>
  </form>
{/if}

{#if tab === 'nodes'}
  <section class="panel table-wrap">
    <table>
      <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>SDRs</th><th>Last address</th><th></th></tr></thead>
      <tbody>
        {#each nodes as node (node.id)}
          <tr>
            <td>{node.name}</td>
            <td>{node.is_local ? 'This server' : 'Slave'}</td>
            <td>
              <span class="pill {statusClass(node.status)}">{node.status}</span>
              {#if node.status === 'active'}<span class="pill {node.online ? 'ok' : 'warn'}">{node.online ? 'Online' : 'Offline'}</span>{/if}
            </td>
            <td class="mono small">{node.serials.join(', ') || '—'}</td>
            <td class="mono small">{node.last_ip ?? '—'}</td>
            <td>{#if !node.is_local && node.status !== 'revoked'}<button class="danger" onclick={() => revokeNode(node)}>Revoke</button>{/if}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </section>

  {#if enrolment}
    <div class="notice warn">
      Enrolment token for <strong>{enrolment.name}</strong>, shown only once and valid for 24 hours. Put it in that slave's configuration:
      <div class="secret" style="margin-top:.35rem">[master]<br />enrollment_token = "{enrolment.enrollment_token}"</div>
    </div>
  {/if}
  <form class="panel fields" onsubmit={addNode}>
    <label>Add a slave node <input bind:value={nodeName} required maxlength="120" placeholder="Field Pi" /></label>
    <button class="primary">Create enrolment token</button>
  </form>
{/if}

{#if tab === 'bans'}
  <form class="panel fields" onsubmit={addBan}>
    <label>Type
      <select bind:value={ban.kind}><option value="ip">IP address or range</option><option value="email">Email</option></select>
    </label>
    <label>Value <input bind:value={ban.value} required placeholder={ban.kind === 'ip' ? '203.0.113.0/24' : 'name@example.com'} /></label>
    <label>Reason <input bind:value={ban.reason} maxlength="255" /></label>
    <button class="primary">Add ban</button>
  </form>
  <section class="panel table-wrap">
    <table>
      <thead><tr><th>Type</th><th>Value</th><th>Reason</th><th>By</th><th>When</th><th></th></tr></thead>
      <tbody>
        {#each bans as row (row.id)}
          <tr><td>{row.kind}</td><td class="mono small">{row.value}</td><td>{row.reason}</td><td class="small">{row.created_by}</td><td class="small">{formatTime(row.created_at)}</td>
            <td><button onclick={() => removeBan(row)}>Remove</button></td></tr>
        {:else}
          <tr><td colspan="6" class="muted">No bans.</td></tr>
        {/each}
      </tbody>
    </table>
  </section>
{/if}

{#if tab === 'audit'}
  <form class="panel fields" onsubmit={(event) => { event.preventDefault(); run(loaders.audit); }}>
    <label>Search action, email, address or subject <input type="search" bind:value={auditSearch} /></label>
    <button class="primary">Search</button>
  </form>
  <section class="panel table-wrap">
    <table>
      <thead><tr><th>When</th><th>User</th><th>From</th><th>Action</th><th>Subject</th></tr></thead>
      <tbody>
        {#each audit as row (row.id)}
          <tr><td class="small">{formatTime(row.ts)}</td><td class="small">{row.email ?? '—'}</td><td class="mono small">{row.ip ?? '—'}</td><td class="mono small">{row.action}</td><td class="small">{row.target ?? ''}</td></tr>
        {:else}
          <tr><td colspan="5" class="muted">Nothing found.</td></tr>
        {/each}
      </tbody>
    </table>
  </section>
{/if}

{#if tab === 'settings' && settings}
  <form class="stack" onsubmit={saveSettings}>
    <section class="panel stack">
      <h2>Retention</h2>
      <p class="muted small" style="margin:0">Use 0 to keep forever. Protected recordings are never removed automatically.</p>
      <div class="fields">
        <label>Keep recordings (days) <input type="number" min="0" bind:value={settings.recording_retention_days} /></label>
        <label>Keep audit logs (days) <input type="number" min="0" bind:value={settings.audit_retention_days} /></label>
      </div>
      <p class="muted small" style="margin:0">Recordings: <span class="mono">{settings.recordings_dir}</span> · Logs: <span class="mono">{settings.log_dir}</span>. These folders are set in the configuration file.</p>
    </section>
    <section class="panel stack">
      <h2>Sign-ups</h2>
      <label class="check">
        <input type="checkbox" checked={settings.registration_open === 'true' || settings.registration_open === true}
          onchange={(event) => (settings.registration_open = event.target.checked)} />
        Allow people to request accounts
      </label>
    </section>
    <section class="panel stack">
      <h2>Uploads</h2>
      <p class="muted small" style="margin:0">Stored keys are never shown again. Leave a key blank to keep the current one.</p>
      <div class="fields">
        <label>RDIO server URL <input type="url" bind:value={settings.rdio_server} placeholder="https://rdio.example.com" /></label>
        <label>RDIO API key {settings.rdio_api_key_set ? '(set)' : '(not set)'} <input type="password" bind:value={keys.rdio_api_key} autocomplete="off" /></label>
        <label>ClearCut server URL <input type="url" bind:value={settings.clearcut_server} placeholder="https://clearcut.example.com" /></label>
        <label>ClearCut API key {settings.clearcut_api_key_set ? '(set)' : '(not set)'} <input type="password" bind:value={keys.clearcut_api_key} autocomplete="off" /></label>
        <label>ClearCut page link for users <input type="url" bind:value={settings.clearcut_page_url} /></label>
      </div>
    </section>
    <section class="panel stack">
      <h2>Maps</h2>
      <label>Map tile URL <input bind:value={settings.map_tile_url} class="mono" /></label>
    </section>
    <div><button class="primary">Save settings</button></div>
  </form>
{/if}
