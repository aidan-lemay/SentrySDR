<script>
  import { onMount, tick } from 'svelte';
  import { api, formatMHz, formatTime } from '../lib.js';

  const TABS = [['users', 'Users'], ['sources', 'SDR sources'], ['nodes', 'Nodes'], ['bans', 'Bans'], ['audit', 'Audit log'], ['settings', 'Settings']];
  const PERMISSIONS = [
    ['listen_live', 'Listen live'], ['listen_recordings', 'Play recordings'], ['download', 'Download'],
    ['assign', 'Assign and schedule'], ['manage_targets', 'Manage targets'], ['protect', 'Protect recordings'],
    ['transcribe', 'Request transcripts'], ['maps', 'Maps'],
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
  const pending = $derived(users.filter((user) => user.status === 'pending').length);

  async function run(action, success) {
    error = '';
    message = '';
    try {
      const reply = await action();
      if (success) {
        message = success;
        scrollTo(0, 0);
      }
      return reply ?? true;
    } catch (failure) {
      error = failure.message;
      scrollTo(0, 0);
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
      if (selected?.id === user.id) selected = { ...structuredClone($state.snapshot(users.find((u) => u.id === user.id))), history: selected.history };
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
    await tick();
    document.getElementById('user-detail')?.scrollIntoView();
  }

  async function resetPassword(user) {
    const reply = await run(() => api(`admin/users/${user.id}/reset-password`, { method: 'POST' }));
    if (reply) {
      oneTimeSecret = reply.temporary_password;
      document.getElementById('user-detail')?.scrollIntoView();
    }
  }

  const banUser = (user, kind, value) => confirm(`Ban ${value}?`) && run(async () => {
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
    document.getElementById('source-form')?.scrollIntoView();
  }

  const removeSource = (source) => confirm(`Remove source ${source.name}? Its recordings are kept.`) && run(async () => {
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

  const revokeNode = (node) => confirm(`Revoke ${node.name}? It will disconnect, and need a new token to come back.`) && run(async () => {
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

  const statusClass = (status) => ({ active: 'st-ok', pending: 'st-warn', denied: 'st-bad', disabled: 'st-bad', revoked: 'st-bad' }[status] ?? '');
</script>

<h1>Admin</h1>

<label class="section-pick">
  <span class="small">Section</span>
  <select value={tab} onchange={(event) => open(event.target.value)}>
    {#each TABS as [id, label] (id)}<option value={id}>{label}</option>{/each}
  </select>
</label>
<div class="tabs" role="tablist">
  {#each TABS as [id, label] (id)}
    <button role="tab" aria-selected={tab === id} onclick={() => open(id)}>{label}</button>
  {/each}
</div>

{#if error}<div class="err" role="alert">{error}</div>{/if}
{#if message}<div class="msg" role="status">{message}</div>{/if}

{#if tab === 'users'}
  {#if pending}<div class="warn">{pending} account request{pending === 1 ? '' : 's'} waiting for a decision.</div>{/if}
  <div class="box">
    <table class="table">
      <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last address</th><th>Last sign-in</th><th>Actions</th></tr></thead>
      {#each users as user (user.id)}
        <tbody>
          <tr>
            <td class="title-cell">{user.name}</td>
            <td data-label="Email"><span>{user.email}</span></td>
            <td data-label="Role"><span>{user.role}</span></td>
            <td data-label="Status"><span class={statusClass(user.status)}>{user.status}</span></td>
            <td data-label="Last address"><span class="mono small">{user.last_ip ?? 'None'}</span></td>
            <td data-label="Last sign-in"><span class="small">{formatTime(user.last_login_at)}</span></td>
            <td>
              <div class="buttons" style="margin:0">
                {#if user.status === 'pending'}
                  <button class="main" onclick={() => updateUser(user, { status: 'active' }, `${user.email} approved.`)}>Approve</button>
                  <button class="bad" onclick={() => confirm(`Deny the request from ${user.email}?`) && updateUser(user, { status: 'denied' }, `${user.email} denied.`)}>Deny</button>
                {/if}
                <button onclick={() => selectUser(user)}>Manage</button>
              </div>
            </td>
          </tr>
        </tbody>
      {/each}
    </table>
  </div>

  {#if selected}
    {@const original = users.find((u) => u.id === selected.id)}
    <div id="user-detail">
      <div class="buttons"><strong class="wrap-any">{selected.email}</strong> <button onclick={() => (selected = null)}>Close</button></div>
      {#if oneTimeSecret}
        <div class="warn">
          Temporary password, shown once. Send it securely. They'll have to change it when they sign in.
          <div class="secret">{oneTimeSecret}</div>
        </div>
      {/if}
      <form onsubmit={(event) => { event.preventDefault(); updateUser(original, { name: selected.name, email: selected.email, role: selected.role }, 'User saved.'); }}>
        <fieldset>
          <legend>Details</legend>
          <label class="field"><span>Name</span><input bind:value={selected.name} required /></label>
          <label class="field"><span>Email</span><input type="email" bind:value={selected.email} required /></label>
          <label class="field"><span>Role</span>
            <select bind:value={selected.role}>
              <option value="listener">Listener</option>
              <option value="operator">Operator</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <div class="buttons form-buttons"><button class="main">Save</button></div>
        </fieldset>
      </form>

      {#if selected.role !== 'admin'}
        <fieldset>
          <legend>Permissions</legend>
          <p class="small">These change what the role allows for this user.</p>
          {#each PERMISSIONS as [name, label] (name)}
            <label class="field"><span>{label}</span>
              <select value={overrideState(name)} onchange={(event) => setOverride(original, name, event.target.value)}>
                <option value="default">Role default ({selected.permissions.includes(name) && overrideState(name) === 'default' ? 'allowed' : overrideState(name) === 'default' ? 'not allowed' : 'overridden'})</option>
                <option value="allow">Allow</option>
                <option value="deny">Deny</option>
              </select>
            </label>
          {/each}
        </fieldset>
      {/if}

      <fieldset>
        <legend>Access</legend>
        <p class="small">Requested from <span class="mono">{selected.signup_ip ?? 'unknown'}</span>. Created {formatTime(selected.created_at)}.</p>
        <div class="buttons">
          <button onclick={() => confirm(`Reset the password for ${selected.email}? They will be signed out.`) && resetPassword(selected)}>Reset password</button>
          {#if selected.totp_enabled}<button onclick={() => run(() => api(`admin/users/${selected.id}/reset-totp`, { method: 'POST' }), 'Two-factor reset.')}>Reset two-factor</button>{/if}
          {#if selected.status === 'active'}
            <button class="bad" onclick={() => confirm(`Disable ${selected.email}? They will be signed out.`) && updateUser(original, { status: 'disabled' }, 'Account disabled.')}>Disable</button>
          {:else}
            <button onclick={() => updateUser(original, { status: 'active' }, 'Account enabled.')}>Enable</button>
          {/if}
          <button class="bad" onclick={() => banUser(selected, 'email', selected.email)}>Ban email</button>
          {#if selected.last_ip}<button class="bad" onclick={() => banUser(selected, 'ip', selected.last_ip)}>Ban {selected.last_ip}</button>{/if}
        </div>
      </fieldset>

      <div class="box">
        <h2>Sessions</h2>
        <table class="table">
          <thead><tr><th>Started</th><th>From</th><th>Browser</th></tr></thead>
          <tbody>
            {#each userSessions as row}
              <tr>
                <td data-label="Started"><span class="small">{formatTime(row.created_at)}</span></td>
                <td data-label="From"><span class="mono small">{row.ip}</span></td>
                <td data-label="Browser"><span class="small">{row.user_agent}</span></td>
              </tr>
            {:else}
              <tr><td class="empty" colspan="3">No active sessions.</td></tr>
            {/each}
          </tbody>
        </table>
      </div>

      <div class="box">
        <h2>Audit log for this user</h2>
        <table class="table">
          <thead><tr><th>When</th><th>Action</th><th>Subject</th><th>From</th></tr></thead>
          <tbody>
            {#each selected.history ?? [] as row (row.id)}
              <tr>
                <td data-label="When"><span class="small">{formatTime(row.ts)}</span></td>
                <td data-label="Action"><span class="mono small">{row.action}</span></td>
                <td data-label="Subject"><span class="small">{row.target ?? ''}</span></td>
                <td data-label="From"><span class="mono small">{row.ip}</span></td>
              </tr>
            {:else}
              <tr><td class="empty" colspan="4">Nothing logged.</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
{/if}

{#if tab === 'sources'}
  <div class="box">
    <table class="table">
      <thead><tr><th>Name</th><th>Node</th><th>Serial</th><th>Antenna</th><th>Range</th><th>Gain</th><th>Actions</th></tr></thead>
      {#each sources as source (source.id)}
        <tbody>
          <tr>
            <td class="title-cell">{source.name}</td>
            <td data-label="Node"><span>{source.node_name}</span></td>
            <td data-label="Serial"><span class="mono small">{source.serial}</span></td>
            <td data-label="Antenna"><span>{source.antenna || 'Not set'}</span></td>
            <td data-label="Range"><span class="mono small">{source.freq_min_hz ? `${formatMHz(source.freq_min_hz)} to ${formatMHz(source.freq_max_hz)}` : 'Not set'}</span></td>
            <td data-label="Gain"><span class="small">{source.gain_db == null ? 'Auto (AGC)' : `${source.gain_db} dB`}</span></td>
            <td>
              <div class="buttons" style="margin:0">
                <button onclick={() => editSource(source)}>Edit</button>
                <button class="bad" onclick={() => removeSource(source)}>Remove</button>
              </div>
            </td>
          </tr>
        </tbody>
      {:else}
        <tbody><tr><td class="empty" colspan="7">No sources set up yet.</td></tr></tbody>
      {/each}
    </table>
  </div>

  <form id="source-form" onsubmit={saveSource}>
    <fieldset>
      <legend>{sourceForm.id ? 'Edit source' : 'Add a source'}</legend>
      {#if !sourceForm.id && !unused.length}
        <div class="warn small">No unused SDRs. A node only offers the serials listed under <span class="mono">sdr.serials</span> in its configuration file.</div>
      {/if}
      <label class="field"><span>SDR</span>
        <select bind:value={sourceForm.key} required disabled={!!sourceForm.id}>
          <option value="" disabled>Choose...</option>
          {#if sourceForm.id}<option value={sourceForm.key}>{sourceForm.key.split('|').slice(1).join('|')}</option>{/if}
          {#each unused as item (`${item.node_id}|${item.serial}`)}
            <option value={`${item.node_id}|${item.serial}`}>{item.serial} on {item.node_name}{item.connected ? '' : ' (not connected)'}</option>
          {/each}
        </select>
      </label>
      <label class="field"><span>Name</span><input bind:value={sourceForm.name} required maxlength="120" /></label>
      <label class="field"><span>Description</span><input bind:value={sourceForm.description} maxlength="500" /></label>
      <label class="field"><span>Antenna type</span><input bind:value={sourceForm.antenna} maxlength="120" /></label>
      <label class="field"><span>Antenna low end (MHz)</span><input type="number" inputmode="decimal" step="0.001" min="0" bind:value={sourceForm.min} /></label>
      <label class="field"><span>Antenna high end (MHz)</span><input type="number" inputmode="decimal" step="0.001" min="0" bind:value={sourceForm.max} /></label>
      <label class="field"><span>Gain (dB)</span>
        <input type="number" inputmode="decimal" step="0.1" min="0" max="60" bind:value={sourceForm.gain} />
        <span class="hint">Leave blank for automatic gain.</span>
      </label>
      <label class="field"><span>Frequency correction (ppm)</span><input type="number" min="-500" max="500" bind:value={sourceForm.ppm} /></label>
      <div class="buttons form-buttons">
        <button class="main">Save</button>
        {#if sourceForm.id}<button type="button" onclick={() => (sourceForm = blankSource())}>Cancel</button>{/if}
      </div>
    </fieldset>
  </form>
{/if}

{#if tab === 'nodes'}
  {#if enrolment}
    <div class="warn">
      Token for <strong>{enrolment.name}</strong>, shown once and good for 24 hours. Put this in that slave's configuration file:
      <div class="secret">[master]<br />enrollment_token = "{enrolment.enrollment_token}"</div>
    </div>
  {/if}
  <div class="box">
    <table class="table">
      <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>SDRs</th><th>Last address</th><th>Actions</th></tr></thead>
      {#each nodes as node (node.id)}
        <tbody>
          <tr>
            <td class="title-cell">{node.name}</td>
            <td data-label="Type"><span>{node.is_local ? 'This server' : 'Slave'}</span></td>
            <td data-label="Status">
              <span>
                <span class={statusClass(node.status)}>{node.status}</span>{#if node.status === 'active'}, <span class={node.online ? 'st-ok' : 'st-warn'}>{node.online ? 'online' : 'offline'}</span>{/if}
              </span>
            </td>
            <td data-label="SDRs"><span class="mono small">{node.serials.join(', ') || 'None'}</span></td>
            <td data-label="Last address"><span class="mono small">{node.last_ip ?? 'None'}</span></td>
            <td>{#if !node.is_local && node.status !== 'revoked'}<button class="bad" onclick={() => revokeNode(node)}>Revoke</button>{/if}</td>
          </tr>
        </tbody>
      {/each}
    </table>
  </div>
  <form onsubmit={addNode}>
    <fieldset>
      <legend>Add a slave</legend>
      <label class="field"><span>Name</span><input bind:value={nodeName} required maxlength="120" /></label>
      <div class="buttons form-buttons"><button class="main">Create token</button></div>
    </fieldset>
  </form>
{/if}

{#if tab === 'bans'}
  <form onsubmit={addBan}>
    <fieldset>
      <legend>Add a ban</legend>
      <label class="field"><span>Type</span>
        <select bind:value={ban.kind}><option value="ip">IP address or range</option><option value="email">Email</option></select>
      </label>
      <label class="field"><span>{ban.kind === 'ip' ? 'Address or range' : 'Email'}</span><input bind:value={ban.value} required /></label>
      <label class="field"><span>Reason</span><input bind:value={ban.reason} maxlength="255" /></label>
      <div class="buttons form-buttons"><button class="main">Add ban</button></div>
    </fieldset>
  </form>
  <div class="box">
    <table class="table">
      <thead><tr><th>Banned</th><th>Reason</th><th>By</th><th>When</th><th>Actions</th></tr></thead>
      {#each bans as row (row.id)}
        <tbody>
          <tr>
            <td class="title-cell"><span class="mono small">{row.value}</span></td>
            <td data-label="Reason"><span>{row.reason || 'None given'}</span></td>
            <td data-label="By"><span class="small">{row.created_by}</span></td>
            <td data-label="When"><span class="small">{formatTime(row.created_at)}</span></td>
            <td><button onclick={() => removeBan(row)}>Remove</button></td>
          </tr>
        </tbody>
      {:else}
        <tbody><tr><td class="empty" colspan="5">No bans.</td></tr></tbody>
      {/each}
    </table>
  </div>
{/if}

{#if tab === 'audit'}
  <form onsubmit={(event) => { event.preventDefault(); run(loaders.audit); }}>
    <fieldset>
      <legend>Search</legend>
      <label class="field"><span>Action, email, address or subject</span><input type="search" bind:value={auditSearch} /></label>
      <div class="buttons form-buttons"><button class="main">Search</button></div>
    </fieldset>
  </form>
  <div class="box">
    <table class="table">
      <thead><tr><th>When</th><th>User</th><th>From</th><th>Action</th><th>Subject</th></tr></thead>
      {#each audit as row (row.id)}
        <tbody>
          <tr>
            <td data-label="When"><span class="small">{formatTime(row.ts)}</span></td>
            <td data-label="User"><span class="small">{row.email ?? 'None'}</span></td>
            <td data-label="From"><span class="mono small">{row.ip ?? 'None'}</span></td>
            <td data-label="Action"><span class="mono small">{row.action}</span></td>
            <td data-label="Subject"><span class="small">{row.target ?? ''}</span></td>
          </tr>
        </tbody>
      {:else}
        <tbody><tr><td class="empty" colspan="5">Nothing found.</td></tr></tbody>
      {/each}
    </table>
  </div>
{/if}

{#if tab === 'settings' && settings}
  <form onsubmit={saveSettings}>
    <fieldset>
      <legend>How long to keep things</legend>
      <label class="field"><span>Recordings (days)</span>
        <input type="number" inputmode="numeric" min="0" bind:value={settings.recording_retention_days} />
        <span class="hint">0 keeps them forever. Protected recordings are never removed automatically.</span>
      </label>
      <label class="field"><span>Audit logs (days)</span>
        <input type="number" inputmode="numeric" min="0" bind:value={settings.audit_retention_days} />
        <span class="hint">0 keeps them forever.</span>
      </label>
      <p class="small wrap-any">Recordings are in <span class="mono">{settings.recordings_dir}</span> and logs in <span class="mono">{settings.log_dir}</span>. Change these in the configuration file.</p>
    </fieldset>
    <fieldset>
      <legend>Sign-ups</legend>
      <label class="tick">
        <input type="checkbox" checked={settings.registration_open === 'true' || settings.registration_open === true}
          onchange={(event) => (settings.registration_open = event.target.checked)} />
        Let people request accounts
      </label>
    </fieldset>
    <fieldset>
      <legend>Uploads</legend>
      <p class="small">Saved keys are never shown again. Leave a key blank to keep the current one.</p>
      <label class="field"><span>RDIO server</span><input type="url" bind:value={settings.rdio_server} /></label>
      <label class="field"><span>RDIO API key {settings.rdio_api_key_set ? '(saved)' : '(not set)'}</span><input type="password" bind:value={keys.rdio_api_key} autocomplete="off" /></label>
      <label class="field"><span>ClearCut server</span><input type="url" bind:value={settings.clearcut_server} /></label>
      <label class="field"><span>ClearCut API key {settings.clearcut_api_key_set ? '(saved)' : '(not set)'}</span><input type="password" bind:value={keys.clearcut_api_key} autocomplete="off" /></label>
      <label class="field"><span>ClearCut link for users</span><input type="url" bind:value={settings.clearcut_page_url} /></label>
    </fieldset>
    <fieldset>
      <legend>Maps</legend>
      <label class="field"><span>Map tile URL</span><input class="mono" bind:value={settings.map_tile_url} /></label>
    </fieldset>
    <div class="buttons"><button class="main">Save settings</button></div>
  </form>
{/if}
