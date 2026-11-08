<script>
  import { onMount } from 'svelte';
  import { ADSB_AGGREGATORS, AIS_SERVICES, api, feedLabel, formatMHz, MODULATIONS } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { clearAfter } from '../notice.svelte.js';

  const blank = () => ({
    id: null, name: '', kind: 'audio', mhz: '', modulation: 'nfm', squelch: 50, volume_db: 0,
    clearcut_enabled: false, clearcut_system: '', clearcut_id: '', rdio_enabled: false, rdio_system: '', rdio_talkgroup: '',
    broadcastify_enabled: false, broadcastify_system_ref: '', broadcastify_slot: '',
    channels: [], newChannel: '', airframes_enabled: false, feeds: blankFeeds(),
  });
  function blankFeeds(saved = {}) {
    const udp = Object.fromEntries(AIS_SERVICES.map(([id]) => {
      const found = (saved.udp ?? []).find((d) => d.service === id);
      return [id, { on: Boolean(found), host: found?.host ?? '', port: found?.port ?? '' }];
    }));
    return {
      aggregators: [...(saved.aggregators ?? [])], beastOn: saved.beast_port != null, beastPort: saved.beast_port ?? 30005,
      community: Boolean(saved.community), udp,
    };
  }
  function feedsBody(kind, feeds) {
    if (kind === 'adsb') return { aggregators: feeds.aggregators, beast_port: feeds.beastOn ? Number(feeds.beastPort) : null };
    if (kind === 'ais') {
      return {
        community: feeds.community,
        udp: AIS_SERVICES.filter(([id]) => feeds.udp[id].on).map(([id]) => ({ service: id, host: feeds.udp[id].host.trim(), port: Number(feeds.udp[id].port) })),
      };
    }
    return null;
  }
  function feedSummary(target) {
    const f = target.feeds ?? {};
    return [...(f.aggregators ?? []), ...(f.udp ?? []).map((d) => d.service), ...(f.community ? ['community'] : [])].map(feedLabel)
      .concat(f.beast_port ? [`local port ${f.beast_port}`] : []);
  }
  // The usual channels. North America's fifth, 129.125, doesn't fit on the same stick.
  const PRESETS = {
    na: ['North America', [130.025, 130.45, 131.125, 131.55]],
    eu: ['Europe', [131.525, 131.725, 131.825]],
  };
  const MAX_SPAN_MHZ = 2;
  const MODE_NAMES = { adsb: 'ADS-B', ais: 'AIS', acars: 'ACARS' };
  // These frequencies are the standard ones each decoder listens on.
  const FIXED = { adsb: { modulation: 'adsb', mhz: 1090 }, ais: { modulation: 'ais', mhz: 162 } };

  let targets = $state([]);
  let systems = $state([]);
  let form = $state(blank());
  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));

  async function refresh() {
    try {
      [targets, systems] = await Promise.all([api('targets'), api('broadcastify-systems').catch(() => [])]);
    } catch (failure) {
      error = failure.message;
    }
  }

  onMount(refresh);

  const optionalInt = (value) => (value === '' || value === null ? null : Number(value));

  // --- ACARS channels
  let channelProblem = $state('');
  const sortedChannels = $derived([...form.channels].map(Number).sort((a, b) => a - b));
  const span = $derived(sortedChannels.length ? sortedChannels.at(-1) - sortedChannels[0] : 0);

  function addChannel() {
    const mhz = Number(form.newChannel);
    channelProblem = '';
    if (form.newChannel === '' || Number.isNaN(mhz)) channelProblem = 'Enter a frequency in MHz.';
    else if (mhz < 118 || mhz > 137) channelProblem = 'ACARS channels are between 118 and 137 MHz.';
    else if (form.channels.some((c) => Math.abs(c - mhz) < 0.0005)) channelProblem = 'That channel is already in the list.';
    else if (form.channels.length >= 8) channelProblem = 'One target can have at most 8 channels.';
    if (channelProblem) return false;
    form.channels.push(mhz);
    form.newChannel = '';
    return true;
  }

  function addOnEnter(event) {
    if (event.key !== 'Enter') return;
    event.preventDefault();  // Enter here adds the channel instead of saving
    addChannel();
  }

  const usePreset = (key) => { form.channels = [...PRESETS[key][1]]; channelProblem = ''; };

  async function save(event) {
    event.preventDefault();
    error = '';
    message = '';
    const fixed = FIXED[form.kind];
    if (form.kind === 'acars') {
      if (form.newChannel !== '' && !addChannel()) return;
      if (!form.channels.length) {
        channelProblem = 'Add at least one channel, or choose a preset.';
        return;
      }
    }
    const channelsHz = form.kind === 'acars' ? sortedChannels.map((mhz) => Math.round(mhz * 1e6)) : [];
    const body = {
      name: form.name, kind: form.kind,
      frequency_hz: form.kind === 'acars' ? channelsHz[0] : Math.round(Number(fixed ? fixed.mhz : form.mhz) * 1e6),
      modulation: form.kind === 'acars' ? 'acars' : fixed ? fixed.modulation : form.modulation,
      acars_channels: channelsHz, airframes_enabled: form.kind === 'acars' && form.airframes_enabled,
      feeds: feedsBody(form.kind, form.feeds),
      squelch: Number(form.squelch), volume_db: Number(form.volume_db || 0),
      clearcut_enabled: form.clearcut_enabled, clearcut_system: form.clearcut_system || null, clearcut_id: optionalInt(form.clearcut_id),
      rdio_enabled: form.rdio_enabled, rdio_system: optionalInt(form.rdio_system), rdio_talkgroup: optionalInt(form.rdio_talkgroup),
      broadcastify_enabled: Boolean(form.broadcastify_enabled), broadcastify_system_ref: optionalInt(form.broadcastify_system_ref), broadcastify_slot: optionalInt(form.broadcastify_slot),
    };
    try {
      const saved = form.id ? await api(`targets/${form.id}`, { method: 'PUT', body }) : await api('targets', { method: 'POST', body });
      message = saved.profiles_cleared
        ? `Target saved. Its frequency or mode changed, so ${saved.profiles_cleared} saved SDR profile(s) were removed. Measure again on the Sources page.`
        : saved.warning ?? 'Target saved.';
      form = blank();
      await refresh();
    } catch (failure) {
      error = failure.message;
    }
  }

  function edit(target) {
    form = {
      ...blank(), ...target, mhz: target.frequency_hz / 1e6,
      clearcut_system: target.clearcut_system ?? '', clearcut_id: target.clearcut_id ?? '',
      rdio_system: target.rdio_system ?? '', rdio_talkgroup: target.rdio_talkgroup ?? '', volume_db: target.volume_db ?? 0,
      broadcastify_enabled: Boolean(target.broadcastify_enabled), broadcastify_system_ref: target.broadcastify_system_ref ?? '', broadcastify_slot: target.broadcastify_slot ?? '',
      channels: (target.acars_channels ?? []).map((hz) => +(hz / 1e6).toFixed(4)), newChannel: '', airframes_enabled: Boolean(target.airframes_enabled),
      feeds: blankFeeds(target.feeds ?? {}),
    };
    channelProblem = '';
    document.getElementById('target-form')?.scrollIntoView();
  }

  async function remove(target) {
    if (!confirm(`Delete target ${target.name}? Any recorders using it are removed too. Its recordings are kept.`)) return;
    error = '';
    try {
      await api(`targets/${target.id}`, { method: 'DELETE' });
      await refresh();
    } catch (failure) {
      error = failure.message;
    }
  }
</script>

<h1>Targets</h1>
{#if error}<div class="err" role="alert">{error}</div>{/if}
{#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}

<div class="box">
  <table class="table">
    <thead><tr><th>Name</th><th>Frequency</th><th>Mode</th><th>Squelch</th><th>Sends to</th><th>Actions</th></tr></thead>
    <tbody>
      {#each targets as target (target.id)}
        <tr>
          <td class="title-cell">{target.name}</td>
          <td data-label="Frequency"><span class="mono">{target.kind === 'acars' ? (target.acars_channels ?? []).map((hz) => +(hz / 1e6).toFixed(4)).join(', ') + ' MHz' : formatMHz(target.frequency_hz)}</span></td>
          <td data-label="Mode"><span>{MODE_NAMES[target.modulation] ?? target.modulation.toUpperCase()}</span></td>
          {#if target.kind === 'audio'}<td data-label="Squelch"><span class="mono">{target.squelch || 'Off'}</span></td>{:else}<td class="wide-only"></td>{/if}
          <td data-label="Sends to"><span>{[target.clearcut_enabled && 'ClearCut', target.rdio_enabled && 'RDIO', target.broadcastify_enabled && 'Broadcastify', target.airframes_enabled && 'airframes.io', ...feedSummary(target)].filter(Boolean).join(', ') || 'None'}</span></td>
          <td>
            <div class="buttons" style="margin:0">
              <button onclick={() => edit(target)}>Edit</button>
              <button class="bad" onclick={() => remove(target)}>Delete</button>
            </div>
          </td>
        </tr>
      {:else}
        <tr><td class="empty" colspan="6">No targets yet. Add one below.</td></tr>
      {/each}
    </tbody>
  </table>
</div>

<form id="target-form" onsubmit={save}>
  <fieldset>
    <legend>{form.id ? 'Edit target' : 'Add a target'}</legend>
    <label class="field"><span>Name / alpha tag</span><input bind:value={form.name} required maxlength="120" /></label>
    <label class="field"><span>Type</span>
      <select bind:value={form.kind}>
        <option value="audio">Voice calls</option>
        <option value="adsb">ADS-B aircraft (1090 MHz)</option>
        <option value="ais">AIS ships (162 MHz)</option>
        <option value="acars">ACARS aircraft messages</option>
      </select>
    </label>
    {#if form.kind === 'acars'}
      <div class="field top"><span>Channels (MHz)</span>
        <div>
          <div class="buttons" style="margin-top:0">
            {#each Object.entries(PRESETS) as [key, [label]] (key)}<button type="button" onclick={() => usePreset(key)}>{label}</button>{/each}
          </div>
          <table class="ranges">
            <tbody>
              {#each form.channels as channel, index (index)}
                <tr>
                  <td><input class="mono" type="number" inputmode="decimal" step="0.001" min="118" max="137" aria-label={`Channel ${index + 1}`} bind:value={form.channels[index]} required /></td>
                  <td><button type="button" aria-label={`Remove channel ${index + 1}`} onclick={() => form.channels.splice(index, 1)}>Remove</button></td>
                </tr>
              {/each}
              <tr class:adding={form.channels.length > 0}>
                <td><input id="new-channel" class="mono" type="number" inputmode="decimal" step="0.001" min="118" max="137" aria-label="New channel" placeholder="131.550" bind:value={form.newChannel} onkeydown={addOnEnter} /></td>
                <td><button type="button" onclick={addChannel}>+ Add</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      {#if channelProblem}<p class="small st-bad form-buttons" role="alert">{channelProblem}</p>{/if}
      {#if span > MAX_SPAN_MHZ}
        <p class="small st-bad form-buttons" role="alert">These span {span.toFixed(3)} MHz, but one stick covers at most {MAX_SPAN_MHZ} MHz. Remove the outermost channel, or put it on another target and stick.</p>
      {:else}
        <p class="small dim form-buttons">One stick decodes every channel here at once, so they must fit within {MAX_SPAN_MHZ} MHz{sortedChannels.length > 1 ? ` (these span ${span.toFixed(3)} MHz)` : ''}.</p>
      {/if}
    {/if}
    {#if form.kind === 'audio'}
      <label class="field"><span>Frequency (MHz)</span><input class="mono" type="number" inputmode="decimal" step="0.0001" min="0.5" max="2200" bind:value={form.mhz} required /></label>
      <label class="field"><span>Modulation</span>
        <select bind:value={form.modulation}>
          {#each MODULATIONS as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
        </select>
      </label>
      <label class="field"><span>Squelch level</span>
        <input class="mono" type="number" inputmode="numeric" min="0" max="100000" bind:value={form.squelch} />
        <span class="hint">Used on any SDR without a saved profile for this target. Must sit above the noise, so measure each SDR on the Sources page. 0 records all the time.</span>
      </label>
      <label class="field"><span>Volume (dB)</span>
        <input class="mono" type="number" inputmode="decimal" min="-20" max="30" step="1" bind:value={form.volume_db} />
        <span class="hint">Applies to recordings, uploads and live audio, on any SDR. 0 leaves the audio unchanged. Too much boost clips loud voices.</span>
      </label>
    {/if}
  </fieldset>
  {#if form.kind === 'adsb'}
  <fieldset>
    <legend>Sharing</legend>
      <div class="field top"><span>Aggregators</span>
        <div class="flags">
          {#each ADSB_AGGREGATORS as [id, label] (id)}<label class="tick"><input type="checkbox" value={id} bind:group={form.feeds.aggregators} /> {label}</label>{/each}
        </div>
      </div>
      <p class="small dim form-buttons">readsb connects out to each one. Nothing is opened to the network. Each SDR is identified by its own random ID.</p>
      <div class="field"><span>FlightRadar24, FlightAware</span><label class="tick"><input type="checkbox" bind:checked={form.feeds.beastOn} /> Offer data to fr24feed or piaware</label></div>
      {#if form.feeds.beastOn}
        <label class="field"><span>Local port</span>
          <input class="mono" type="number" inputmode="numeric" min="1025" max="65535" bind:value={form.feeds.beastPort} required />
          <span class="hint">Beast data on 127.0.0.1 only, on the machine with the SDR. Point fr24feed or piaware at 127.0.0.1 and this port. See the README for setup.</span>
        </label>
      {/if}
  </fieldset>
  {/if}
  {#if form.kind === 'ais'}
  <fieldset>
    <legend>Sharing</legend>
      <div class="field"><span>aiscatcher.org</span><label class="tick"><input type="checkbox" bind:checked={form.feeds.community} /> Share with the aiscatcher.org community feed</label></div>
      {#each AIS_SERVICES as [id, label] (id)}
        <div class="field"><span>{label}</span><label class="tick"><input type="checkbox" bind:checked={form.feeds.udp[id].on} /> Send to {label}</label></div>
        {#if form.feeds.udp[id].on}
          <div class="field"><span>{label} address</span>
            <div class="pair">
              <input class="mono" aria-label={`${label} host`} placeholder="Host or IP" bind:value={form.feeds.udp[id].host} required maxlength="253" />
              <input class="mono" type="number" inputmode="numeric" min="1" max="65535" aria-label={`${label} port`} placeholder="Port" bind:value={form.feeds.udp[id].port} required style="max-width:7rem" />
            </div>
            <span class="hint">The UDP address {label} gives you when you register a station.</span>
          </div>
        {/if}
      {/each}
  </fieldset>
  {/if}
  {#if form.kind === 'acars'}
  <fieldset>
    <legend>Sharing</legend>
      <div class="field"><span>airframes.io</span><label class="tick"><input type="checkbox" bind:checked={form.airframes_enabled} /> Feed messages to airframes.io</label></div>
      {#if form.airframes_enabled}<p class="small dim form-buttons">Sent with the station ID set under Admin &gt; Uploads and feeds. Nothing is sent until one is set.</p>{/if}
  </fieldset>
  {/if}
  {#if form.kind === 'audio'}
  <fieldset>
    <legend>Uploads</legend>
      <div class="field"><span>ClearCut</span><label class="tick"><input type="checkbox" bind:checked={form.clearcut_enabled} /> Upload calls to ClearCut</label></div>
      {#if form.clearcut_enabled}
        <label class="field"><span>ClearCut system ID</span><input bind:value={form.clearcut_system} required maxlength="64" pattern="[A-Za-z0-9_.\-]+" /></label>
        <label class="field"><span>ClearCut talkgroup</span><input type="number" inputmode="numeric" bind:value={form.clearcut_id} required /></label>
      {/if}
      <div class="field"><span>RDIO</span><label class="tick"><input type="checkbox" bind:checked={form.rdio_enabled} /> Upload calls to RDIO</label></div>
      {#if form.rdio_enabled}
        <label class="field"><span>RDIO system</span><input type="number" inputmode="numeric" bind:value={form.rdio_system} required /></label>
        <label class="field"><span>RDIO talkgroup</span><input type="number" inputmode="numeric" bind:value={form.rdio_talkgroup} required /></label>
      {/if}
      <div class="field"><span>Broadcastify</span><label class="tick"><input type="checkbox" bind:checked={form.broadcastify_enabled} /> Upload calls to Broadcastify Calls</label></div>
      {#if form.broadcastify_enabled}
        <label class="field"><span>Broadcastify system</span>
          <select bind:value={form.broadcastify_system_ref} required>
            <option value="">Choose a system</option>
            {#each systems as system (system.id)}<option value={system.id}>{system.name} (node {system.system_id})</option>{/each}
          </select>
          {#if !systems.length}<span class="hint">An administrator adds systems under Admin &gt; Uploads and feeds.</span>{/if}
        </label>
        <label class="field"><span>Frequency slot</span>
          <input type="number" inputmode="numeric" min="1" bind:value={form.broadcastify_slot} required />
          <span class="hint">The Slot beside this frequency on Broadcastify's Manage Calls Systems page, under Node Frequencies.</span>
        </label>
      {/if}
  </fieldset>
  {/if}
    <div class="buttons form-buttons">
      <button class="main">Save</button>
      {#if form.id}<button type="button" onclick={() => (form = blank())}>Cancel</button>{/if}
    </div>
</form>
