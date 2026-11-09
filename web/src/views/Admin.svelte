<script>
  import { onMount, tick } from 'svelte';
  import { ANTENNA_UNITS, DEFAULT_GAIN_DB, formatSize, OTHER_USES, RECORDED_USES, api, formatRange, formatRanges, formatTime, nearestStep } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { clearAfter, NOTICE_MS } from '../notice.svelte.js';
  import GainField from '../GainField.svelte';

  const TABS = [['users', 'Users'], ['sources', 'SDR sources'], ['antennas', 'Antennas'], ['nodes', 'Nodes'], ['bans', 'Bans'], ['audit', 'Audit log'], ['settings', 'Settings'], ['uploads', 'Uploads and feeds'], ['email', 'Email'], ['warnings', 'Warnings']];
  const MUTE_CHOICES = [['1', '1 hour'], ['4', '4 hours'], ['8', '8 hours'], ['24', '1 day'], ['72', '3 days'], ['168', '1 week'], ['', 'Until I unmute it']];
  const PERMISSIONS = [
    ['listen_live', 'Listen live'], ['listen_recordings', 'Play recordings'], ['download', 'Download'],
    ['assign', 'Assign and schedule'], ['manage_targets', 'Manage targets'], ['protect', 'Protect recordings'],
    ['transcribe', 'Request transcripts'], ['maps', 'Maps'],
  ];

  let tab = $state('users');
  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));

  let users = $state([]);
  let selected = $state(null);
  let userSessions = $state([]);
  let oneTimeSecret = $state(null);

  let sources = $state([]);
  let available = $state([]);
  let nodes = $state([]);
  const blankSource = () => ({
    id: null, key: '', name: '', description: '', antenna_id: '', gain: DEFAULT_GAIN_DB, ppm: 0,
    tuner: '', steps: '', capMin: '', capMax: '', uses: [], rtlTcpPort: '', lat: '', lon: '', alt: '', quiet: false,
  });
  const TUNERS = ['R820T', 'R820T2', 'R828D', 'FC0012', 'FC0013', 'FC2580', 'E4000'];
  let detecting = $state(false);
  let sourceForm = $state(blankSource());

  let antennas = $state([]);
  const ANTENNA_TYPES = ['Discone', 'Dipole', 'Ground plane', 'Collinear', 'J-pole', 'Slim Jim', 'Yagi', 'Log periodic', 'Whip', 'Telescopic', 'Loop', 'Magnetic mount'];
  const blankAntenna = () => ({ id: null, name: '', kind: '', length: '', length_unit: 'in', notes: '', ranges: [], newLow: '', newHigh: '' });
  let antennaForm = $state(blankAntenna());
  let rangeProblem = $state('');

  let nodeName = $state('');
  let enrolment = $state(null);

  let bans = $state([]);
  let ban = $state({ kind: 'ip', value: '', reason: '' });

  let warnings = $state({ current: [], cleared: [] });
  let muteFor = $state({});

  let audit = $state([]);
  let auditSearch = $state('');

  let settings = $state(null);
  let keys = $state({ rdio_api_key: '', clearcut_api_key: '' });
  let systems = $state([]);
  const blankSystem = () => ({ id: null, name: '', system_id: '', api_key: '' });
  let systemForm = $state(blankSystem());
  let systemReplies = $state({});

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
    // Requests waiting for a decision come first.
    users: async () => { users = (await api('admin/users')).sort((a, b) => (b.status === 'pending') - (a.status === 'pending')); },
    sources: async () => { [sources, available, antennas] = await Promise.all([api('sources'), api('sources/available'), api('antennas')]); },
    antennas: async () => { antennas = await api('antennas'); },
    nodes: async () => { nodes = await api('nodes'); },
    bans: async () => { bans = await api('admin/bans'); },
    audit: async () => { audit = await api(`admin/audit?limit=200${auditSearch ? `&search=${encodeURIComponent(auditSearch)}` : ''}`); },
    settings: async () => { settings = await api('admin/settings'); systems = await api('admin/broadcastify'); },
    uploads: async () => { settings = await api('admin/settings'); systems = await api('admin/broadcastify'); },
    warnings: async () => { warnings = await api('admin/warnings'); },
    email: async () => { settings = await api('admin/settings'); smtpPassword = ''; },
  };

  function open(next) {
    tab = next;
    message = '';
    selected = null;
    oneTimeSecret = null;
    enrolment = null;
    if (location.hash !== `#/admin?tab=${next}`) history.replaceState(null, '', `#/admin?tab=${next}`);
    run(loaders[next]);
  }

  // The address names the section, so #/admin?tab=warnings (from the warnings banner) opens it.
  function openFromAddress() {
    if (!location.hash.startsWith('#/admin')) return;
    const wanted = new URLSearchParams(location.hash.split('?')[1] ?? '').get('tab');
    open(TABS.some(([id]) => id === wanted) ? wanted : 'users');
  }

  onMount(() => {
    openFromAddress();
    addEventListener('hashchange', openFromAddress);
    return () => removeEventListener('hashchange', openFromAddress);
  });

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
    const body = {
      node_id: Number(nodeId), serial: serial.join('|'), name: sourceForm.name, description: sourceForm.description,
      antenna_id: sourceForm.antenna_id === '' ? null : Number(sourceForm.antenna_id),
      gain_db: sourceForm.gain === '' || sourceForm.gain === null ? null : Number(sourceForm.gain), ppm: Number(sourceForm.ppm),
      tuner: sourceForm.tuner.trim() || null, gain_steps: parseSteps(sourceForm.steps),
      cap_min_hz: filled(sourceForm.capMin) ? toHz(sourceForm.capMin) : null,
      cap_max_hz: filled(sourceForm.capMax) ? toHz(sourceForm.capMax) : null, uses: sourceForm.uses,
      rtl_tcp_port: filled(sourceForm.rtlTcpPort) ? Number(sourceForm.rtlTcpPort) : null,
      lat: filled(sourceForm.lat) ? Number(sourceForm.lat) : null, lon: filled(sourceForm.lon) ? Number(sourceForm.lon) : null,
      alt_m: filled(sourceForm.alt) ? Number(sourceForm.alt) : null, quiet_offline: sourceForm.quiet,
    };
    if (body.gain_steps.some(Number.isNaN)) {
      error = 'Gain steps must be numbers, separated by spaces or commas.';
      return;
    }
    const done = await run(() => (sourceForm.id
      ? api(`sources/${sourceForm.id}`, { method: 'PUT', body })
      : api('sources', { method: 'POST', body })), 'Source saved.');
    if (done) {
      if (done.warning) message = `Source saved. ${done.warning}`;
      sourceForm = blankSource();
      await loaders.sources();
    }
  }

  function editSource(source) {
    sourceForm = {
      id: source.id, key: `${source.node_id}|${source.serial}`, name: source.name, description: source.description,
      antenna_id: source.antenna_id ?? '', gain: source.gain_db ?? '', ppm: source.ppm, feederId: source.feeder_uuid ?? '',
      tuner: source.tuner, steps: source.gain_steps.join(' '),
      capMin: source.cap_min_hz == null ? '' : plain(source.cap_min_hz), capMax: source.cap_max_hz == null ? '' : plain(source.cap_max_hz),
      uses: [...source.uses], rtlTcpPort: source.rtl_tcp_port ?? '',
      lat: source.lat ?? '', lon: source.lon ?? '', alt: source.alt_m ?? '', quiet: !!source.quiet_offline,
    };
    document.getElementById('source-form')?.scrollIntoView();
  }

  const parseSteps = (text) => text.split(/[\s,]+/).filter(Boolean).map(Number);
  const formSteps = $derived(parseSteps(sourceForm.steps).filter((n) => !Number.isNaN(n)));
  const formNearest = $derived(nearestStep(formSteps, sourceForm.gain));

  // Reads the tuner from the stick itself. Fills the form; nothing is saved until Save.
  async function detect() {
    const [nodeId, ...serial] = sourceForm.key.split('|');
    const existing = sources.find((s) => s.node_id === Number(nodeId) && s.serial === serial.join('|'));
    if (existing?.active_target && !confirm(`Detect the tuner on ${existing.name}? It stops listening on ${existing.active_target.name} for a few seconds.`)) return;
    detecting = true;
    const found = await run(() => api('sources/detect', { method: 'POST', body: { node_id: Number(nodeId), serial: serial.join('|') } }));
    detecting = false;
    if (!found) return;
    sourceForm.tuner = found.tuner;
    sourceForm.steps = found.gain_steps.join(' ');
    const snapped = nearestStep(found.gain_steps, sourceForm.gain);
    const moved = snapped === null ? '' : ` Gain moved from ${sourceForm.gain} to ${snapped} dB, the nearest step.`;
    if (snapped !== null) sourceForm.gain = snapped;
    if (found.freq_min_hz != null) sourceForm.capMin = plain(found.freq_min_hz);
    if (found.freq_max_hz != null) sourceForm.capMax = plain(found.freq_max_hz);
    message = `Found ${found.tuner} with ${found.gain_steps.length} gain steps${found.freq_min_hz == null ? '. Its range isn\'t known, so enter it if you want it checked' : ''}. Save to keep it.${moved}`;
  }

  // --- antennas
  const toHz = (mhz) => Math.round(Number(mhz) * 1e6);
  const filled = (value) => value !== '' && value !== null && value !== undefined;
  const plain = (hz) => +(hz / 1e6).toFixed(4);

  function badRange(low, high) {
    if (!filled(low) || !filled(high)) return 'Enter both ends of the range.';
    if (Number(low) < 0 || Number(high) < 0) return 'Frequencies can\'t be negative.';
    if (Number(low) > Number(high)) return 'The low end is above the high end.';
    return '';
  }

  function addRange() {
    rangeProblem = badRange(antennaForm.newLow, antennaForm.newHigh);
    if (rangeProblem) return false;
    antennaForm.ranges.push({ low: antennaForm.newLow, high: antennaForm.newHigh });
    antennaForm.newLow = '';
    antennaForm.newHigh = '';
    document.getElementById('new-range-low')?.focus();
    return true;
  }

  function addOnEnter(event) {
    if (event.key !== 'Enter') return;
    event.preventDefault();  // Enter here means add this range, not save the antenna
    addRange();
  }

  async function saveAntenna(event) {
    event.preventDefault();
    rangeProblem = '';
    // A range typed into the add row but not added yet still counts.
    if ((filled(antennaForm.newLow) || filled(antennaForm.newHigh)) && !addRange()) return;
    const broken = antennaForm.ranges.map((r) => badRange(r.low, r.high)).find(Boolean);
    if (broken) {
      rangeProblem = broken;
      return;
    }
    const body = {
      name: antennaForm.name, kind: antennaForm.kind, notes: antennaForm.notes,
      length: filled(antennaForm.length) ? Number(antennaForm.length) : null, length_unit: antennaForm.length_unit,
      ranges: antennaForm.ranges.map((r) => ({ low_hz: toHz(r.low), high_hz: toHz(r.high) })),
    };
    const done = await run(() => (antennaForm.id
      ? api(`antennas/${antennaForm.id}`, { method: 'PUT', body })
      : api('antennas', { method: 'POST', body })), 'Antenna saved.');
    if (done) {
      antennaForm = blankAntenna();
      await loaders.antennas();
    }
  }

  function editAntenna(antenna) {
    antennaForm = {
      ...blankAntenna(), id: antenna.id, name: antenna.name, kind: antenna.kind, notes: antenna.notes,
      length: antenna.length ?? '', length_unit: antenna.length_unit,
      ranges: antenna.ranges.map(([low, high]) => ({ low: plain(low), high: plain(high) })),
    };
    rangeProblem = '';
    document.getElementById('antenna-form')?.scrollIntoView();
  }

  function removeAntenna(antenna) {
    const used = antenna.sources.length ? ` ${antenna.sources.join(', ')} will have no antenna set.` : '';
    if (!confirm(`Remove antenna ${antenna.name}?${used}`)) return;
    run(async () => {
      await api(`antennas/${antenna.id}`, { method: 'DELETE' });
      if (antennaForm.id === antenna.id) antennaForm = blankAntenna();
      await loaders.antennas();
    }, 'Antenna removed.');
  }

  const unitLabel = (unit) => ANTENNA_UNITS.find(([id]) => id === unit)?.[1] ?? unit;
  const formatLength = (antenna) => (antenna.length == null ? 'Not set' : `${antenna.length} ${unitLabel(antenna.length_unit)}`);

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

  const restartSource = (source) => confirm(`Restart ${source.name}? Its tool stops and starts again, which takes a few seconds. A call in progress is saved.`) && run(async () => {
    await api(`sources/${source.id}/restart`, { method: 'POST' });
    await loaders.sources();
  }, `${source.name} restarted.`);

  const restartService = (node) => confirm(`Restart SentrySDR on ${node.name}? Every SDR on it stops for about 10 seconds. Calls in progress are saved.${node.is_local ? ' This page reconnects when it\'s back.' : ''}`) && run(async () => {
    await api(`nodes/${node.id}/restart`, { method: 'POST' });
  }, `${node.name} is restarting. It should be back in about 10 seconds.`);

  const revokeNode = (node) => confirm(`Revoke ${node.name}? It will disconnect, and need a new token to come back.`) && run(async () => {
    await api(`nodes/${node.id}`, { method: 'DELETE' });
    await loaders.nodes();
  }, `${node.name} revoked.`);

  // --- nodes: no warning while offline
  const setNodeQuiet = (node, quiet) => run(async () => {
    await api(`nodes/${node.id}/quiet`, { method: 'POST', body: { quiet } });
    await loaders.nodes();
    dispatchEvent(new Event('warnings-changed'));
  }, quiet ? `No warnings when ${node.name} is offline.` : `${node.name} warns when offline again.`);

  // --- warnings: muted for this admin only
  const muteWarning = (warning) => run(async () => {
    const hours = muteFor[warning.key] ?? '8';
    await api('admin/warnings/mute', { method: 'POST', body: { key: warning.key, hours: hours === '' ? null : Number(hours) } });
    await loaders.warnings();
    dispatchEvent(new Event('warnings-changed'));
  }, 'Warning muted for you.');

  const unmuteWarning = (key) => run(async () => {
    await api('admin/warnings/unmute', { method: 'POST', body: { key } });
    await loaders.warnings();
    dispatchEvent(new Event('warnings-changed'));
  }, 'Warning unmuted.');

  const muteText = (until) => (until ? `Muted for you until ${formatTime(until)}` : 'Muted for you until you unmute it');

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
      broadcastify_server: settings.broadcastify_server,
      message_retention_days: Number(settings.message_retention_days),
      recording_retention_gb: Number(settings.recording_retention_gb || 0),
      airframes_station_id: settings.airframes_station_id.trim(), airframes_feed: settings.airframes_feed.trim(),
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

  // --- email
  let smtpPassword = $state('');
  let testing = $state(false);
  const SMTP_PORTS = { starttls: 587, tls: 465, none: 25 };

  async function saveEmail(event) {
    event.preventDefault();
    const body = {
      email_alerts_enabled: settings.email_alerts_enabled === 'true' || settings.email_alerts_enabled === true,
      email_daily_cap: Number(settings.email_daily_cap), smtp_host: settings.smtp_host.trim(), smtp_port: Number(settings.smtp_port),
      smtp_security: settings.smtp_security, smtp_username: settings.smtp_username.trim(), smtp_from: settings.smtp_from.trim(),
    };
    if (smtpPassword) body.smtp_password = smtpPassword;
    const reply = await run(() => api('admin/settings', { method: 'PATCH', body }), 'Email settings saved.');
    if (reply) {
      settings = reply;
      smtpPassword = '';
    }
  }

  async function testEmail() {
    testing = true;
    const reply = await run(() => api('admin/email/test', { method: 'POST' }));
    if (reply) message = reply.detail;
    testing = false;
  }

  // --- backup
  let backupPassword = $state('');
  let backingUp = $state(false);

  async function downloadBackup(event) {
    event.preventDefault();
    backingUp = true;
    error = '';
    try {
      const response = await fetch('api/admin/backup', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-SentrySDR': '1' },
        body: JSON.stringify({ password: backupPassword }),
      });
      if (!response.ok) {
        error = (await response.json().catch(() => ({}))).detail ?? `Backup failed (${response.status})`;
        scrollTo(0, 0);
        return;
      }
      const name = /filename="?([^";]+)"?/.exec(response.headers.get('content-disposition') ?? '')?.[1] ?? 'sentrysdr-backup.tar.gz';
      const link = document.createElement('a');
      link.href = URL.createObjectURL(await response.blob());
      link.download = name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      backupPassword = '';
      message = 'Backup downloaded.';
    } finally {
      backingUp = false;
    }
  }

  // --- Broadcastify systems
  async function saveSystem(event) {
    event.preventDefault();
    const body = { name: systemForm.name, system_id: Number(systemForm.system_id), api_key: systemForm.api_key || null };
    const path = systemForm.id ? `admin/broadcastify/${systemForm.id}` : 'admin/broadcastify';
    if (await run(() => api(path, { method: systemForm.id ? 'PUT' : 'POST', body }), 'System saved.')) {
      systemForm = blankSystem();
      systems = await api('admin/broadcastify');
    }
  }

  async function removeSystem(system) {
    if (!confirm(`Remove Broadcastify system ${system.name}? Its saved key is deleted.`)) return;
    if (await run(() => api(`admin/broadcastify/${system.id}`, { method: 'DELETE' }), 'System removed.')) {
      systems = await api('admin/broadcastify');
    }
  }

  async function testSystem(system) {
    systemReplies[system.id] = 'Asking Broadcastify...';
    try {
      const text = `Broadcastify replied: ${(await api(`admin/broadcastify/${system.id}/test`, { method: 'POST' })).reply}`;
      systemReplies[system.id] = text;
      setTimeout(() => { if (systemReplies[system.id] === text) delete systemReplies[system.id]; }, NOTICE_MS);
    } catch (failure) {
      systemReplies[system.id] = failure.message;
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
{#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}

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
      <thead><tr><th>Name</th><th>Node</th><th>Serial</th><th>Stick</th><th>Antenna</th><th>Gain</th><th>Actions</th></tr></thead>
      {#each sources as source (source.id)}
        <tbody>
          <tr>
            <td class="title-cell">{source.name}{#if source.description}<div class="small dim" style="font-weight:normal">{source.description}</div>{/if}</td>
            <td data-label="Node"><span>{source.node_name}</span></td>
            <td data-label="Serial"><span class="mono small">{source.serial}</span></td>
            <td data-label="Stick">
              <span class="small">
                {source.tuner || 'Tuner not set'}{#if source.cap_min_hz != null || source.cap_max_hz != null}, <span class="mono">{formatRange(source.cap_min_hz, source.cap_max_hz)}</span>{/if}
                {#if source.uses.length}<br />{[...RECORDED_USES, ...OTHER_USES].filter(([id]) => source.uses.includes(id)).map(([, label]) => label).join(', ')}{/if}
              </span>
            </td>
            <td data-label="Antenna"><span class="small">{source.antenna_name || 'Not set'}{#if source.antenna_id}<br /><span class="mono">{formatRanges(source.antenna_ranges)}</span>{/if}</span></td>
            <td data-label="Gain"><span class="small">{source.gain_db == null ? 'Auto (AGC)' : `${source.gain_db} dB`}</span></td>
            <td>
              <div class="buttons" style="margin:0">
                <button onclick={() => editSource(source)}>Edit</button>
                <button onclick={() => restartSource(source)} disabled={!source.node_online} title="Stop and start this SDR's tool, for a stick that was unplugged">Restart</button>
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
      {#if sourceForm.feederId}
        <div class="field"><span>Feeder ID</span>
          <span class="mono small copyable">{sourceForm.feederId}</span>
          <span class="hint">Sent with ADS-B shared to aggregators, so they can tell this SDR's feed apart. Some feeder stats pages ask for it. Keep it private: anyone with it could feed data as this receiver.</span>
        </div>
      {/if}
      <label class="field"><span>Description</span><input bind:value={sourceForm.description} maxlength="500" /></label>
      <label class="field"><span>Antenna</span>
        <select bind:value={sourceForm.antenna_id}>
          <option value="">None</option>
          {#each antennas as antenna (antenna.id)}<option value={antenna.id}>{antenna.name}{antenna.kind && antenna.kind !== antenna.name ? ` (${antenna.kind})` : ''}</option>{/each}
        </select>
        <span class="hint">Add antennas under the Antennas section. One antenna can feed several SDRs.</span>
      </label>
      <label class="field"><span>Gain (dB)</span>
        <GainField steps={formSteps} bind:value={sourceForm.gain} />
        {#if formNearest !== null}<span class="hint st-warn">Not one of this stick's gain steps. It will use {formNearest} dB. Choose a step to save.</span>{/if}
        {#if formSteps.length}
          <span class="hint">Only this stick's own steps can be used. AGC is automatic gain.</span>
        {:else}
          <span class="hint">Starts at {DEFAULT_GAIN_DB} dB. Leave empty for automatic gain (AGC). 0 means a real 0 dB, which may miss most calls. Detect the stick below to choose from its own steps.</span>
        {/if}
      </label>
      <label class="field"><span>Frequency correction (ppm)</span><input type="number" min="-500" max="500" bind:value={sourceForm.ppm} /></label>
      <div class="field"><span>Location</span>
        <div class="pair">
          <input class="mono" type="number" inputmode="decimal" step="any" min="-90" max="90" aria-label="Latitude" placeholder="Latitude" bind:value={sourceForm.lat} />
          <input class="mono" type="number" inputmode="decimal" step="any" min="-180" max="180" aria-label="Longitude" placeholder="Longitude" bind:value={sourceForm.lon} />
        </div>
      </div>
      <label class="field"><span>Antenna height (m)</span>
        <input class="mono" type="number" inputmode="decimal" step="any" min="-500" max="10000" bind:value={sourceForm.alt} />
        <span class="hint">Optional. Where the antenna is, in decimal degrees (43.1566, -77.6088), for the map's receiver marker and ADS-B coverage. Anyone who can see maps sees it. Moving it starts the coverage again.</span>
      </label>
      <label class="field"><span>rtl_tcp port</span>
        <input class="mono" type="number" inputmode="numeric" min="1024" max="65535" placeholder="Picked for you" bind:value={sourceForm.rtlTcpPort} />
        <span class="hint">Used when this SDR is on an rtl_tcp target. Each SDR on one machine needs its own. The address is set in that machine's configuration file.</span>
      </label>
      <div class="field"><span>Warnings</span>
        <label class="tick"><input type="checkbox" bind:checked={sourceForm.quiet} /> Don't warn if its stick is unplugged</label>
        <span class="hint">For everyone. Other faults still warn. The warning still shows under Admin, Warnings.</span>
      </div>
    </fieldset>
    <fieldset>
      <legend>What this stick can do</legend>
      <p class="small dim form-buttons">The range and uses only warn: a target this stick doesn't suit can still be assigned. Gain steps limit the gain boxes to the stick's own steps. Leave anything blank to skip it.</p>
      <div class="buttons form-buttons">
        <button type="button" disabled={!sourceForm.key || detecting} onclick={detect}>{detecting ? 'Detecting...' : 'Detect from the stick'}</button>
      </div>
      <p class="small dim form-buttons">Reads the tuner and gain steps, and fills in that tuner's usual range. Pauses the stick for a few seconds. Works on slaves too.</p>
      <label class="field"><span>Tuner</span>
        <input bind:value={sourceForm.tuner} maxlength="40" list="tuner-names" placeholder="R820T" />
        <datalist id="tuner-names">{#each TUNERS as name (name)}<option value={name}></option>{/each}</datalist>
      </label>
      <div class="field"><span>Frequency range (MHz)</span>
        <div class="pair">
          <input class="mono" type="number" inputmode="decimal" step="any" min="0" aria-label="Lowest frequency" placeholder="From" bind:value={sourceForm.capMin} />
          <input class="mono" type="number" inputmode="decimal" step="any" min="0" aria-label="Highest frequency" placeholder="To" bind:value={sourceForm.capMax} />
        </div>
      </div>
      <label class="field"><span>Gain steps (dB)</span>
        <input class="mono" bind:value={sourceForm.steps} placeholder="0.0 0.9 1.4 ..." />
        <span class="hint">Separated by spaces or commas. These become the only gains you can choose for this stick.</span>
      </label>
      <div class="field top"><span>Uses</span>
        <div>
          <div class="small dim">Checked against targets</div>
          <div class="flags">{#each RECORDED_USES as [id, label] (id)}<label class="tick"><input type="checkbox" value={id} bind:group={sourceForm.uses} /> {label}</label>{/each}</div>
          <div class="small dim" style="margin-top:.3rem">Labels only</div>
          <div class="flags">{#each OTHER_USES as [id, label] (id)}<label class="tick"><input type="checkbox" value={id} bind:group={sourceForm.uses} /> {label}</label>{/each}</div>
        </div>
      </div>
      <div class="buttons form-buttons">
        <button class="main">Save</button>
        {#if sourceForm.id}<button type="button" onclick={() => (sourceForm = blankSource())}>Cancel</button>{/if}
      </div>
    </fieldset>
  </form>
{/if}

{#if tab === 'antennas'}
  <div class="box">
    <table class="table">
      <thead><tr><th>Name</th><th>Type</th><th>Length</th><th>Effective ranges</th><th>Used by</th><th>Actions</th></tr></thead>
      {#each antennas as antenna (antenna.id)}
        <tbody>
          <tr>
            <td class="title-cell">{antenna.name}{#if antenna.notes}<div class="small dim" style="font-weight:normal">{antenna.notes}</div>{/if}</td>
            <td data-label="Type"><span>{antenna.kind || 'Not set'}</span></td>
            <td data-label="Length"><span class="small">{formatLength(antenna)}</span></td>
            <td data-label="Ranges"><span class="mono small">{formatRanges(antenna.ranges)}</span></td>
            <td data-label="Used by"><span class="small">{antenna.sources.join(', ') || 'No SDRs'}</span></td>
            <td>
              <div class="buttons" style="margin:0">
                <button onclick={() => editAntenna(antenna)}>Edit</button>
                <button class="bad" onclick={() => removeAntenna(antenna)}>Remove</button>
              </div>
            </td>
          </tr>
        </tbody>
      {:else}
        <tbody><tr><td class="empty" colspan="6">No antennas yet.</td></tr></tbody>
      {/each}
    </table>
  </div>

  <form id="antenna-form" onsubmit={saveAntenna}>
    <fieldset>
      <legend>{antennaForm.id ? 'Edit antenna' : 'Add an antenna'}</legend>
      <label class="field"><span>Name</span><input bind:value={antennaForm.name} required maxlength="120" placeholder="Roof discone" /></label>
      <label class="field"><span>Type</span>
        <input bind:value={antennaForm.kind} maxlength="60" list="antenna-types" />
        <datalist id="antenna-types">{#each ANTENNA_TYPES as kind (kind)}<option value={kind}></option>{/each}</datalist>
      </label>
      <div class="field"><span>Length</span>
        <div class="pair">
          <input type="number" inputmode="decimal" step="any" min="0" aria-label="Length" bind:value={antennaForm.length} />
          <select aria-label="Unit" bind:value={antennaForm.length_unit}>
            {#each ANTENNA_UNITS as [id, label] (id)}<option value={id}>{label}</option>{/each}
          </select>
        </div>
      </div>
      <div class="field top"><span>Effective ranges (MHz)</span>
        <table class="ranges">
          <thead><tr><th>From</th><th>To</th><th></th></tr></thead>
          <tbody>
            {#each antennaForm.ranges as range, index (range)}
              <tr>
                <td><input class="mono" type="number" inputmode="decimal" step="any" min="0" aria-label={`Range ${index + 1} from`} bind:value={range.low} required /></td>
                <td><input class="mono" type="number" inputmode="decimal" step="any" min="0" aria-label={`Range ${index + 1} to`} bind:value={range.high} required /></td>
                <td><button type="button" aria-label={`Remove range ${index + 1}`} onclick={() => antennaForm.ranges.splice(index, 1)}>Remove</button></td>
              </tr>
            {/each}
            <tr class:adding={antennaForm.ranges.length > 0}>
              <td><input id="new-range-low" class="mono" type="number" inputmode="decimal" step="any" min="0" aria-label="New range from" placeholder="144" bind:value={antennaForm.newLow} onkeydown={addOnEnter} /></td>
              <td><input class="mono" type="number" inputmode="decimal" step="any" min="0" aria-label="New range to" placeholder="148" bind:value={antennaForm.newHigh} onkeydown={addOnEnter} /></td>
              <td><button type="button" onclick={addRange}>+ Add</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      {#if rangeProblem}<p class="small st-bad form-buttons" role="alert">{rangeProblem}</p>{/if}
      <p class="small dim form-buttons">Add one range per band the antenna works on. A target outside all of them gets a warning, but can still be assigned. With no ranges, nothing is checked.</p>
      <label class="field"><span>Notes</span><input bind:value={antennaForm.notes} maxlength="500" placeholder="Mounting, feed line, splitter..." /></label>
      <div class="buttons form-buttons">
        <button class="main">{antennaForm.id ? 'Save antenna' : 'Add antenna'}</button>
        {#if antennaForm.id}<button type="button" onclick={() => { antennaForm = blankAntenna(); rangeProblem = ''; }}>Cancel</button>{/if}
      </div>
      <p class="small dim form-buttons">To use an antenna, choose it on each SDR under SDR sources.</p>
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
      <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Version</th><th>SDRs</th><th>Last address</th><th>Actions</th></tr></thead>
      {#each nodes as node (node.id)}
        <tbody>
          <tr>
            <td class="title-cell">{node.name}</td>
            <td data-label="Type"><span>{node.is_local ? 'This server' : 'Slave'}</span></td>
            <td data-label="Status">
              <span>
                <span class={statusClass(node.status)}>{node.status}</span>{#if node.status === 'active'}, <span class={node.online ? 'st-ok' : 'st-warn'}>{node.online ? 'online' : 'offline'}</span>{/if}
                {#if !node.is_local && node.status !== 'revoked'}
                  <label class="tick small"><input type="checkbox" checked={node.quiet_offline} onchange={(event) => setNodeQuiet(node, event.target.checked)} /> Don't warn if offline</label>
                {/if}
              </span>
            </td>
            <td data-label="Version">
              <span class="small">
                {node.version ?? 'Unknown'}{#if node.build}{' '}<span class="mono dim">{node.build}</span>{/if}
                {#if !node.matches_master && node.status === 'active' && !node.is_local}<br /><span class="st-warn">Differs from this server. Upgrade it.</span>{/if}
              </span>
            </td>
            <td data-label="SDRs"><span class="mono small">{node.serials.join(', ') || 'None'}</span></td>
            <td data-label="Last address"><span class="mono small">{node.last_ip ?? 'None'}</span></td>
            <td>
              <div class="buttons" style="margin:0">
                {#if node.status === 'active'}<button onclick={() => restartService(node)} disabled={!node.online && !node.is_local}>Restart service</button>{/if}
                {#if !node.is_local && node.status !== 'revoked'}<button class="bad" onclick={() => revokeNode(node)}>Revoke</button>{/if}
              </div>
            </td>
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

{#if (tab === 'settings' || tab === 'uploads') && settings}
  <form onsubmit={saveSettings}>
    {#if tab === 'settings'}
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
      <label class="field"><span>Recordings size limit (GB)</span>
        <input type="number" inputmode="decimal" min="0" step="any" bind:value={settings.recording_retention_gb} />
        <span class="hint">0 means no limit. Over it, the oldest unprotected recordings are deleted first. Using {formatSize(settings.recordings_bytes)}{settings.disk ? `, with ${formatSize(settings.disk.free)} free on that disk` : ''}.</span>
      </label>
      <label class="field"><span>ACARS messages (days)</span>
        <input type="number" inputmode="numeric" min="0" bind:value={settings.message_retention_days} />
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
    {:else}
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
      <legend>Broadcastify Calls server</legend>
      <label class="field"><span>Upload address</span><input class="mono" type="url" bind:value={settings.broadcastify_server} /></label>
    </fieldset>
    <fieldset>
      <legend>airframes.io feed</legend>
      <p class="small">ACARS targets with feeding turned on send each message here. No account is needed.</p>
      <label class="field"><span>Station ID</span>
        <input class="mono" bind:value={settings.airframes_station_id} maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9\-]*" placeholder="KE-KSEA-ACARS" />
        <span class="hint">Your initials, the nearest airport's ICAO code, then ACARS. Leave empty to send nothing.</span>
      </label>
      <label class="field"><span>Feed address</span><input class="mono" bind:value={settings.airframes_feed} maxlength="260" placeholder="feed.acars.io:5550" /></label>
    </fieldset>
    {/if}
    {#if tab === 'settings'}
    <fieldset>
      <legend>Maps</legend>
      <label class="field"><span>Map tile URL</span><input class="mono" bind:value={settings.map_tile_url} /></label>
    </fieldset>
    {/if}
    <div class="buttons"><button class="main">{tab === 'uploads' ? 'Save uploads and feeds' : 'Save settings'}</button></div>
  </form>
  {#if tab === 'settings'}
    <form onsubmit={downloadBackup}>
      <fieldset>
        <legend>Backup</legend>
        <p class="small">The database and <span class="mono">secret.key</span>, as one file: every account, setting, SDR, target, schedule and recording's details, and the key that decrypts the saved API keys. Recordings themselves aren't included. Keep the file private.</p>
        <label class="field"><span>Your password</span><input type="password" bind:value={backupPassword} required autocomplete="current-password" /></label>
        <div class="buttons form-buttons"><button disabled={backingUp}>{backingUp ? 'Preparing...' : 'Download backup'}</button></div>
      </fieldset>
    </form>
  {/if}
  {#if tab === 'uploads'}
  <form onsubmit={saveSystem}>
    <fieldset>
      <legend>Broadcastify Calls systems</legend>
      <p class="small">One per node. Each target then chooses a system and a frequency slot. Saved keys are never shown again.</p>
      {#each systems as system (system.id)}
        <div class="small" style="margin:.5rem 0">
          <strong>{system.name}</strong>, node {system.system_id}
          <div class="buttons" style="margin:.2rem 0">
            <button type="button" onclick={() => testSystem(system)}>Check credentials</button>
            <button type="button" onclick={() => (systemForm = { ...blankSystem(), id: system.id, name: system.name, system_id: system.system_id })}>Edit</button>
            <button type="button" onclick={() => removeSystem(system)}>Remove</button>
          </div>
          {#if systemReplies[system.id]}<Notice ondismiss={() => delete systemReplies[system.id]}>{systemReplies[system.id]}</Notice>{/if}
        </div>
      {/each}
      <label class="field"><span>Name</span><input bind:value={systemForm.name} maxlength="120" required /></label>
      <label class="field"><span>Broadcastify system ID</span><input type="number" inputmode="numeric" min="1" bind:value={systemForm.system_id} required /><span class="hint">The node's ID from Broadcastify, not a RadioReference ID.</span></label>
      <label class="field"><span>API key{systemForm.id ? ' (leave blank to keep the saved key)' : ''}</span><input type="password" bind:value={systemForm.api_key} autocomplete="off" required={!systemForm.id} /></label>
      <div class="buttons form-buttons"><button>{systemForm.id ? 'Save system' : 'Add system'}</button>
        {#if systemForm.id}<button type="button" onclick={() => (systemForm = blankSystem())}>Cancel</button>{/if}
      </div>
    </fieldset>
  </form>
  {/if}
{/if}

{#if tab === 'warnings'}
  <p class="small dim">Everything that needs attention now, including what's hidden. Muting is for you only: other admins and operators still see it. To stop a warning for everyone, set <strong>Don't warn if offline</strong> on the slave under Nodes, or on the SDR under SDR sources.</p>
  <div class="box">
    {#each warnings.current as warning (warning.key)}
      <div class="call">
        <div><span class={warning.level === 'bad' ? 'st-bad' : 'st-warn'}>{warning.level === 'bad' ? 'Problem' : 'Warning'}</span>: <a href={warning.page}>{warning.text}</a></div>
        {#if warning.quiet}
          <div class="small dim">Not shown to anyone. {warning.quiet}</div>
        {:else if warning.muted}
          <div class="small dim">{muteText(warning.muted_until)}.</div>
          <div class="buttons" style="margin:.3rem 0 0"><button onclick={() => unmuteWarning(warning.key)}>Unmute</button></div>
        {:else}
          <div class="pair" style="margin-top:.3rem">
            <select aria-label="Mute for how long" value={muteFor[warning.key] ?? '8'} onchange={(event) => (muteFor[warning.key] = event.target.value)}>
              {#each MUTE_CHOICES as [hours, label] (hours)}<option value={hours}>{label}</option>{/each}
            </select>
            <button onclick={() => muteWarning(warning)}>Mute</button>
          </div>
        {/if}
      </div>
    {:else}
      <p class="pad dim">Nothing needs attention.</p>
    {/each}
  </div>
  {#if warnings.cleared.length}
    <h2>Muted, but cleared for now</h2>
    <p class="small dim">These have stopped for now. The mute still applies if they come back.</p>
    <div class="box">
      {#each warnings.cleared as mute (mute.key)}
        <div class="call">
          <div>{mute.text}</div>
          <div class="small dim">{muteText(mute.muted_until)}.</div>
          <div class="buttons" style="margin:.3rem 0 0"><button onclick={() => unmuteWarning(mute.key)}>Unmute</button></div>
        </div>
      {/each}
    </div>
  {/if}
{/if}

{#if tab === 'email' && settings}
  <form onsubmit={saveEmail}>
    <fieldset>
      <legend>Alert emails</legend>
      <p class="small">Each user chooses their own alerts on the Alerts page, and gets them only at an address they've confirmed. Mail goes through the server below; SentrySDR doesn't run its own.</p>
      <label class="tick"><input type="checkbox" checked={settings.email_alerts_enabled === 'true' || settings.email_alerts_enabled === true} onchange={(event) => (settings.email_alerts_enabled = event.target.checked)} /> Send alert emails</label>
      <label class="field"><span>Most emails per person per day</span>
        <input type="number" inputmode="numeric" min="1" max="1000" bind:value={settings.email_daily_cap} />
        <span class="hint">Stops a noisy keyword filling an inbox, or getting this server's mail marked as spam. Confirmation emails don't count.</span>
      </label>
    </fieldset>
    <fieldset>
      <legend>Mail server (SMTP)</legend>
      <label class="field"><span>Server</span><input class="mono" bind:value={settings.smtp_host} maxlength="253" placeholder="smtp.example.com" autocomplete="off" /></label>
      <label class="field"><span>Encryption</span>
        <select bind:value={settings.smtp_security} onchange={() => (settings.smtp_port = SMTP_PORTS[settings.smtp_security])}>
          <option value="starttls">STARTTLS (port 587)</option>
          <option value="tls">TLS (port 465)</option>
          <option value="none">None (this machine)</option>
        </select>
      </label>
      <label class="field"><span>Port</span><input class="mono" type="number" inputmode="numeric" min="1" max="65535" bind:value={settings.smtp_port} /></label>
      <label class="field"><span>Username</span><input bind:value={settings.smtp_username} maxlength="254" autocomplete="off" /></label>
      <label class="field"><span>Password {settings.smtp_password_set ? '(saved)' : '(not set)'}</span>
        <input type="password" bind:value={smtpPassword} autocomplete="new-password" />
        <span class="hint">Stored encrypted, and never shown again. Leave empty to keep the saved one. For Gmail or Outlook, use an app password.</span>
      </label>
      <label class="field"><span>From</span>
        <input bind:value={settings.smtp_from} maxlength="254" placeholder="SentrySDR <alerts@example.com>" />
        <span class="hint">An address the mail server is allowed to send as, or the email may be refused or land in spam.</span>
      </label>
      <div class="buttons form-buttons">
        <button class="main">Save email settings</button>
        <button type="button" disabled={testing || !settings.smtp_host} onclick={testEmail}>{testing ? 'Sending...' : 'Send me a test email'}</button>
      </div>
      <p class="small dim form-buttons">The test uses the saved settings, so save first. It goes to your own address.</p>
    </fieldset>
  </form>
{/if}
