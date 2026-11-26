<script>
  import { onMount } from 'svelte';
  import { api, arrangeSources, DEFAULT_GAIN_DB, feedLabel, formatMHz, formatTime, formatDuration, nearestStep, antennaUsable, problemNote, STATE_LABELS, stickProblems } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { NOTICE_MS } from '../notice.svelte.js';
  import GainField from '../GainField.svelte';
  import NowPlaying from '../NowPlaying.svelte';
  import { CallPlayer } from '../player.svelte.js';

  let { user } = $props();
  let sources = $state([]);
  let targets = $state([]);
  let recorders = $state([]);
  let loaded = $state(false);
  let error = $state('');
  let listening = $state(null);
  let drafts = $state({});
  let notices = $state({});
  let recent = $state({});
  let measures = $state({});
  // This user's own order and groups, set on the My account page. Collapsing a group is done here.
  let layout = $state({ items: [] });
  const entries = $derived(arrangeSources(sources, layout));

  // Info and warnings clear themselves after a while. Errors stay until the next action.
  const noticeTimers = {};
  function notify(id, notice) {
    clearTimeout(noticeTimers[id]);
    notices[id] = notice;
    if (notice && notice.kind !== 'err') noticeTimers[id] = setTimeout(() => (notices[id] = null), NOTICE_MS);
  }

  const can = (permission) => user.permissions.includes(permission);
  const isAdmin = user.role === 'admin';
  const canAssign = isAdmin || user.permissions.includes('assign');
  const MODE_LABELS = { adsb: 'ADS-B', rtltcp: 'rtl_tcp', tuner: 'Tuner' };
  // What to paste into GQRX's Device string. The buffer settings help over a network.
  const gqrxString = (address) => `rtl_tcp=${address},buffers=16,psize=16384`;
  const wildcard = (address) => /^(0\.0\.0\.0|::|\[::\]):/.test(address);
  // Only administrators can put an SDR on rtl_tcp. The current one stays listed so the picker shows it.
  const assignable = (source) => targets.filter((t) => t.kind !== 'rtltcp' || isAdmin || t.id === source.base_target_id);
  // Only voice targets make calls. An idle SDR still offers the calls it made before.
  const records = (source) => !source.active_target || ['audio', 'multi'].includes(source.active_target.kind);
  // Multi-channel: NFM, FM and AM targets can share one SDR. Keep in step with pipelines.MULTI_MODES.
  const MULTI_MODES = ['nfm', 'fm', 'am'];
  const channelChoices = (source) => targets.filter((t) => t.kind === 'audio' && MULTI_MODES.includes(t.modulation)
    && !source.channels.some((c) => c.target_id === t.id));
  const channelKey = (source, channel) => `${source.id}:${channel.target_id}`;
  const rateLabel = (rate) => (rate >= 1e6 ? `${rate / 1e6} MS/s` : `${rate / 1e3} kS/s`);

  async function refresh() {
    try {
      [sources, targets, recorders] = await Promise.all([
        api('sources'), api('targets'), api('recorders'), ...Object.keys(callsOpen).map((id) => loadRecent(id)),
      ]);
      for (const source of sources) {
        drafts[source.id] ??= { target: '', editing: false, channel: '' };
        // Follow changes made elsewhere, unless this user is mid-selection.
        if (!drafts[source.id].editing) drafts[source.id].target = source.base_target_id ?? '';
        if (!measures[source.id]) {
          measures[source.id] = { target: source.active_target?.kind === 'audio' ? source.active_target.id : '', gain: '', squelch: '', result: null, busy: false };
          if (measures[source.id].target !== '') chooseTarget(source);
        }
      }
      error = '';
    } catch (failure) {
      error = failure.message;
    } finally {
      loaded = true;
    }
  }

  let refreshing = false;
  async function poll() {
    if (refreshing || document.hidden) return;  // don't stack requests on a slow link
    refreshing = true;
    try {
      await refresh();
    } finally {
      refreshing = false;
    }
  }

  async function loadLayout() {
    try {
      layout = await api('me/source-layout');
    } catch {
      // Not essential: the sources still show, in the usual order.
    }
  }

  async function toggleGroup(name) {
    const group = layout.items.find((item) => item.group === name);
    if (!group) return;
    group.collapsed = !group.collapsed;
    try {
      layout = await api('me/source-layout', { method: 'PUT', body: layout });
    } catch (failure) {
      error = `Couldn't remember that: ${failure.message}`;
    }
  }

  onMount(() => {
    refresh();
    loadLayout();
    const timer = setInterval(poll, 3000);
    return () => {
      clearInterval(timer);
      player.stop();
    };
  });

  async function act(source, path, options, success) {
    notify(source.id, null);
    try {
      const reply = await api(path, options);
      notify(source.id, reply?.warning ? { kind: 'warn', text: reply.warning } : { kind: 'msg', text: success });
      await refresh();
      return true;
    } catch (failure) {
      notify(source.id, { kind: 'err', text: failure.message });
      return false;
    }
  }

  // Targets that would fit alongside a multi-channel SDR's channels. Loaded when its assignment opens.
  let suggestions = $state({});
  async function loadSuggestions(source) {
    if (!source.multichannel) return;
    try {
      suggestions[source.id] = await api(`sources/${source.id}/channel-suggestions`);
    } catch {
      suggestions[source.id] = [];
    }
  }

  async function addSuggested(source, target) {
    if (await act(source, `sources/${source.id}/channels`, { method: 'POST', body: { target_id: target.id } }, `${target.name} added.`)) {
      await loadSuggestions(sources.find((s) => s.id === source.id) ?? source);
    }
  }

  async function setMultichannel(source, multichannel) {
    if (!multichannel && source.channels.length > 1
      && !confirm(`Back to single channel? ${source.name} keeps ${source.channels[0].name} and drops its other ${source.channels.length - 1} channel${source.channels.length === 2 ? '' : 's'}.`)) return;
    await act(source, `sources/${source.id}/channel-mode`, { method: 'PUT', body: { multichannel } },
      multichannel ? 'Multi-channel: add the channels to record.' : 'Single channel.');
    await loadSuggestions(sources.find((s) => s.id === source.id) ?? source);
  }

  async function addChannel(source) {
    const draft = drafts[source.id];
    if (!draft.channel) return;
    if (await act(source, `sources/${source.id}/channels`, { method: 'POST', body: { target_id: Number(draft.channel) } }, 'Channel added.')) {
      draft.channel = '';
      await loadSuggestions(sources.find((s) => s.id === source.id) ?? source);
    }
  }

  function removeChannel(source, channel) {
    if (!confirm(`Stop recording ${channel.name} on ${source.name}? A call in progress on its other channels is saved, and they carry on after a moment.`)) return;
    act(source, `sources/${source.id}/channels/${channel.target_id}`, { method: 'DELETE' }, 'Channel removed.')
      .then(() => loadSuggestions(sources.find((s) => s.id === source.id) ?? source));
  }

  async function assign(source) {
    const draft = drafts[source.id];
    const done = await act(source, `sources/${source.id}/assign`, {
      method: 'POST',
      body: { target_id: draft.target === '' ? null : Number(draft.target) },
    }, draft.target === '' ? 'Target cleared.' : 'Target assigned.');
    if (done) {
      draft.editing = false;
      await refresh();
    }
  }

  function restart(source) {
    if (!confirm(`Restart ${source.name}? Its tool stops and starts again, which takes a few seconds.`)) return;
    act(source, `sources/${source.id}/restart`, { method: 'POST' }, 'Restarted. It may take a few seconds to show Listening.');
  }

  async function setLocked(source, box) {
    const done = await act(source, `sources/${source.id}/lock`, { method: box.checked ? 'POST' : 'DELETE' }, box.checked ? 'Locked.' : 'Unlocked.');
    if (!done) box.checked = source.locked;
  }

  const audioTargets = () => targets.filter((t) => t.kind === 'audio');
  const profileFor = (source, targetId) => source.profiles.find((p) => p.target_id === Number(targetId));
  const targetName = (id) => targets.find((t) => t.id === id)?.name ?? `Target ${id}`;
  const gainLabel = (gain) => (gain === null || gain === undefined ? 'AGC' : `${gain} dB`);
  const optionalNumber = (value) => (value === '' || value === null ? null : Number(value));

  // Loads what's saved for the chosen target, since a squelch only suits its own gain.
  // A running channel of a multi-channel SDR is measured live, at the SDR's own gain, without pausing it.
  const liveChannel = (source, targetId) => source.multichannel && source.active_target?.kind === 'multi'
    && source.channels.some((c) => c.target_id === Number(targetId) && c.fits);

  function chooseTarget(source) {
    const m = measures[source.id];
    const saved = profileFor(source, m.target);
    m.gain = source.multichannel ? (source.gain_db ?? '') : saved ? (saved.gain_db ?? '') : (source.gain_db ?? DEFAULT_GAIN_DB);
    if (!saved && source.gain_db == null) m.gain = nearestStep(source.gain_steps, m.gain) ?? m.gain;  // the default may not be a step
    m.squelch = saved?.squelch ?? '';
    m.result = null;
  }

  async function measure(source) {
    const m = measures[source.id];
    const running = source.active_target && !liveChannel(source, m.target) ? ` It will stop listening on ${source.active_target.name} for about 10 seconds, and any call in progress ends there.` : '';
    if (running && !confirm(`Measure noise on ${source.name}?${running}`)) return;
    m.busy = true;
    m.result = null;
    notify(source.id, null);
    try {
      m.result = await api(`sources/${source.id}/measure`, { method: 'POST', body: { target_id: Number(m.target), gain_db: optionalNumber(m.gain) } });
      m.squelch = m.result.suggested;
    } catch (failure) {
      notify(source.id, { kind: 'err', text: failure.message });
    } finally {
      m.busy = false;
    }
  }

  async function saveProfile(source) {
    const m = measures[source.id];
    const body = { squelch: Number(m.squelch), gain_db: optionalNumber(m.gain), noise_level: m.result?.typical ?? null };
    if (await act(source, `sources/${source.id}/profiles/${m.target}`, { method: 'PUT', body }, 'Profile saved.')) m.result = null;
  }

  function removeProfile(source, targetId) {
    if (!confirm(`Remove the saved profile for ${targetName(targetId)} on ${source.name}?`)) return;
    act(source, `sources/${source.id}/profiles/${targetId}`, { method: 'DELETE' }, 'Profile removed.');
  }

  // An open call list is reloaded with each refresh, so new calls and transcripts appear.
  const callsOpen = {};
  // One player for the page: each open call list can feed it.
  const player = new CallPlayer();
  let autoplaying = $state({});

  async function loadRecent(id) {
    const reply = await api(`recordings?source_id=${id}&limit=5`);
    if (callsOpen[id]) recent[id] = reply.items;  // unless it was closed meanwhile
    if (autoplaying[id]) player.offer(reply.items);
  }

  function toggleAutoplay(source) {
    if (autoplaying[source.id]) delete autoplaying[source.id];
    else autoplaying[source.id] = true;
    const any = Object.keys(autoplaying).length > 0;
    if (any && !player.on) player.start();
    if (!any && player.on) player.stop();
  }

  async function toggleRecent(source) {
    if (callsOpen[source.id]) {
      delete callsOpen[source.id];
      if (autoplaying[source.id]) toggleAutoplay(source);
      delete recent[source.id];
      return;
    }
    callsOpen[source.id] = true;
    try {
      await loadRecent(source.id);
    } catch (failure) {
      delete callsOpen[source.id];
      notify(source.id, { kind: 'err', text: failure.message });
    }
  }

  const scheduleCount = (source) => recorders.filter((r) => r.source_id === source.id && r.schedule_days).length;
  const stateClass = (state) => ({ listening: 'st-ok', error: 'st-bad', offline: 'st-warn' }[state] ?? '');
</script>

<h1>Sources</h1>
<NowPlaying {player} />
{#if error}<div class="err" role="alert">{error}</div>{/if}

{#if loaded && !sources.length}
  <div class="msg">
    No sources are set up yet.
    {#if isAdmin}Add one under <a href="#/admin">Admin</a>.{:else}Ask an administrator to add one.{/if}
  </div>
{/if}

{#if sources.length}
  <div class="box">
    <table class="table">
      <thead><tr><th>Source</th><th>Tuned to</th><th>Status</th><th>Last activity</th><th>Actions</th></tr></thead>
      {#each entries as entry (entry.source ? `s${entry.source.id}` : `g${entry.group}`)}
        {#if entry.source}
          {@render row(entry.source)}
        {:else if entry.sources.length}
          <tbody class="group-head">
            <tr><td colspan="5"><div class="group-line">
              <button class="link-button" aria-expanded={!entry.collapsed} onclick={() => toggleGroup(entry.group)}>
                <span aria-hidden="true">{entry.collapsed ? '▸' : '▾'}</span> <strong>{entry.group}</strong>
              </button>
              <span class="small dim">{entry.sources.length} source{entry.sources.length === 1 ? '' : 's'}</span>
              {#if entry.collapsed && entry.sources.some((s) => s.in_call)}<span class="st-air small">ON AIR</span>{/if}
            </div></td></tr>
          </tbody>
          {#if !entry.collapsed}
            {#each entry.sources as source (source.id)}{@render row(source)}{/each}
          {/if}
        {/if}
      {/each}
    </table>
  </div>
{/if}

{#snippet row(source)}
        {@const draft = drafts[source.id]}
        <tbody>
          <tr>
            <td class="title-cell">
              {source.name}
              {#if source.description}<div class="small" style="font-weight:normal">{source.description}</div>{/if}
              <div class="small dim" style="font-weight:normal">{source.node_name}{source.tuner ? `, ${source.tuner}` : ''}{source.antenna_name ? `, ${source.antenna_name}` : ''}</div>
            </td>
            <td data-label="Tuned to">
              <div>
                {#if source.active_target?.kind === 'multi'}
                  {#each source.channels as ch (ch.target_id)}
                    <div class="channel-line">
                      <span class="mono nowrap">{formatMHz(ch.frequency_hz)}</span> {ch.name}
                      {#if ch.description}<span class="small dim">{ch.description}</span>{/if}
                      {#if ch.schedule}<span class="st-warn small">Schedule: {ch.schedule}</span>{/if}
                      <span class="small">({MODE_LABELS[ch.modulation] ?? ch.modulation.toUpperCase()}, squelch {ch.squelch == null ? 'off' : `${ch.squelch} dB`}{ch.level_db != null ? `, now ${ch.level_db} dB` : ''})</span>
                      {#if ch.in_call}<span class="st-air small">ON AIR</span>{/if}
                      {#if !ch.fits}<span class="st-warn small">Not recording: it no longer fits with the others</span>{/if}
                      {#if can('listen_live') && ch.fits}
                        <button class="link-button small" onclick={() => (listening = listening === channelKey(source, ch) ? null : channelKey(source, ch))}>{listening === channelKey(source, ch) ? 'Stop' : 'Listen'}</button>
                      {/if}
                    </div>
                  {/each}
                  {#if source.plan}<span class="small dim">Reading {rateLabel(source.plan.rate)} around {formatMHz(source.plan.center_hz)}, gain {gainLabel(source.gain_db)}, {source.channels.filter((c) => c.fits).length} of at most {source.max_channels} channels</span>{/if}
                  {#if source.load != null}
                    <br /><span class={`small ${source.load > 0.7 ? 'st-warn' : 'dim'}`} title="How long processing takes compared with the radio it covers. Over 100% it would fall behind and drop audio.">Processing load {Math.round(source.load * 100)}%{source.load > 0.7 ? ': close to its limit. Remove a channel, or check the machine under Admin > SDR sources.' : ''}</span>
                  {/if}
                {:else if source.active_target}
                  <span class="mono nowrap">{formatMHz(source.active_target.frequency_hz)}</span><br />
                  {source.active_target.name} ({MODE_LABELS[source.active_target.modulation] ?? source.active_target.modulation.toUpperCase()})
                  {#if source.active_target.description}<br /><span class="small dim">{source.active_target.description}</span>{/if}
                  {#if source.active_target.kind === 'audio'}
                    <br /><span class="small">Squelch {source.active_target.squelch == null ? 'off' : `${source.active_target.squelch} dB`}, gain {gainLabel(source.active_target.gain_db)}{source.active_target.from_profile ? ', saved profile' : ''}</span>
                  {/if}
                  {#if source.active_is_schedule}<br /><span class="st-warn small">Schedule: {source.active_recorder_name}</span>{/if}
                {:else}
                  <span class="dim">Nothing assigned</span>
                {/if}
              </div>
            </td>
            <td data-label="Status">
              <div>
                {#if source.in_call}<span class="st-air">ON AIR</span><br />{/if}
                <span class={stateClass(source.state)}>{STATE_LABELS[source.state] ?? source.state}</span>
                {#if source.locked}<br /><span class="st-warn">Locked</span>{/if}
                {#if source.error}<br /><span class="st-bad small wrap-any">{source.error}</span>{/if}
                {#if source.note}<br /><span class="st-warn small">{source.note}</span>{/if}
                {#if canAssign && source.feeds?.aggregators && Object.keys(source.feeds.aggregators).length}
                  <br /><span class="small">Feeding: {#each Object.entries(source.feeds.aggregators) as [key, up], i (key)}{i ? ', ' : ''}<span class={up ? 'st-ok' : 'st-warn'}>{feedLabel(key)} {up ? 'connected' : 'not connected'}</span>{/each}</span>
                {/if}
                {#if canAssign && source.feeds?.local_clients != null}<br /><span class="small">Local port: {source.feeds.local_clients} program{source.feeds.local_clients === 1 ? '' : 's'} reading</span>{/if}
                {#if canAssign && source.rtl_tcp?.address}
                  <br /><span class="small">rtl_tcp, <span class={source.rtl_tcp.client ? 'st-ok' : ''}>{source.rtl_tcp.client ? 'a program is connected' : 'waiting for a program to connect'}</span>. GQRX device string:</span>
                  <br /><span class="mono small copyable" title="Double-click to select">{gqrxString(source.rtl_tcp.address)}</span>
                  {#if wildcard(source.rtl_tcp.address)}<br /><span class="small st-warn">It listens on every address, so replace {source.rtl_tcp.address.replace(/:\d+$/, '')} with this machine's own.</span>{/if}
                {/if}
                {#if canAssign && source.feeds?.sending?.length}<br /><span class="small">Sending to {source.feeds.sending.map(feedLabel).join(', ')}</span>{/if}
              </div>
            </td>
            {#if records(source)}
              <td data-label="Last activity"><span class="small">{formatTime(source.last_call_at)}</span></td>
            {:else}
              <td class="wide-only"></td><!-- this target doesn't make calls -->
            {/if}
            <td class="actions">
              <div class="buttons">
                {#if can('listen_live') && source.active_target?.kind === 'audio'}
                  <button onclick={() => (listening = listening === source.id ? null : source.id)}>{listening === source.id ? 'Stop' : 'Listen'}</button>
                {/if}
                {#if can('listen_recordings') && records(source)}
                  <button onclick={() => toggleRecent(source)}>{recent[source.id] ? 'Hide calls' : 'Calls'}</button>
                {/if}
                {#if isAdmin && source.state === 'error' && source.node_online}
                  <button onclick={() => restart(source)} title="Stop and start this SDR's tool">Restart</button>
                {/if}
                {#if can('maps') && source.active_target?.kind === 'acars'}
                  <a class="button" href="#/messages">Messages</a>
                {/if}
                {#if (can('tuner') || can('listen_live')) && source.active_target?.kind === 'multi'}
                  <a class="button" href={`#/tuner?source=${source.id}`} target="_blank" rel="noopener" title="See its span and listen anywhere in it, while it records. It can't be moved from there.">View in tuner</a>
                {/if}
                {#if can('tuner') && source.active_target?.kind === 'tuner'}
                  <a class="button" href={`#/tuner?source=${source.id}`} target="_blank" rel="noopener">Open tuner</a>
                {/if}
              </div>
            </td>
          </tr>
          <tr>
            <td colspan="5">
              {#if notices[source.id]?.kind === 'err'}<div class="err" role="alert">{notices[source.id].text}</div>
              {:else if notices[source.id]}<Notice kind={notices[source.id].kind} ondismiss={() => notify(source.id, null)}>{notices[source.id].text}</Notice>{/if}
              {#if listening === source.id}
                <!-- svelte-ignore a11y_media_has_caption -->
                <audio src={`api/live/${source.id}`} controls autoplay></audio>
              {/if}
              {#each source.channels as ch (ch.target_id)}
                {#if listening === channelKey(source, ch)}
                  <div class="small">Live: {ch.name}</div>
                  <!-- svelte-ignore a11y_media_has_caption -->
                  <audio src={`api/live/${source.id}?target_id=${ch.target_id}`} controls autoplay></audio>
                {/if}
              {/each}
              {#if recent[source.id]}
                <label class="tick small"><input type="checkbox" checked={!!autoplaying[source.id]} onchange={() => toggleAutoplay(source)} /> Autoplay new calls from {source.name}</label>
                {#each recent[source.id] as call (call.id)}
                  <div style="margin:.4rem 0">
                    <div class="small">{formatTime(call.started_at)}, {formatDuration(call.duration)}, {call.target_name}</div>
                    <!-- svelte-ignore a11y_media_has_caption -->
                    <audio src={`api/recordings/${call.id}/audio`} controls preload="none"></audio>
                    {#if call.transcript}<div class="transcript small">{call.transcript}</div>{/if}
                  </div>
                {:else}
                  <p class="dim small">No calls recorded on this source yet.</p>
                {/each}
                <p class="small"><a href={`#/recordings?source_id=${source.id}`}>All calls from this source</a></p>
              {/if}
              {#if (can('assign') || isAdmin) && draft}
                <details ontoggle={(event) => event.currentTarget.open && loadSuggestions(source)}>
                  <summary class="small" style="padding:.3rem 0">Change assignment{scheduleCount(source) ? ` (${scheduleCount(source)} on the schedule)` : ''}</summary>
                  <div class="field wide"><span>Channels</span>
                    <div class="buttons" role="radiogroup" aria-label="Channels">
                      <label class="tick"><input type="radio" name={`mode-${source.id}`} checked={!source.multichannel} disabled={source.locked && !isAdmin} onchange={() => setMultichannel(source, false)} /> Single channel</label>
                      <label class="tick"><input type="radio" name={`mode-${source.id}`} checked={source.multichannel} disabled={source.locked && !isAdmin} onchange={() => setMultichannel(source, true)} /> Multi-channel</label>
                    </div>
                    <span class="hint">{source.multichannel
                      ? 'Several NFM, FM and AM targets within about 2 MHz.'
                      : 'One target, with rtl_fm.'}</span>
                  </div>
                  {#if source.multichannel}
                    {#each source.channels as ch (ch.target_id)}
                      <div class="form-buttons buttons">
                        <span class="small"><span class="mono">{formatMHz(ch.frequency_hz)}</span> {ch.name}</span>
                        <button disabled={source.locked && !isAdmin} onclick={() => removeChannel(source, ch)}>Remove</button>
                      </div>
                    {/each}
                    {#if suggestions[source.id]?.length}
                      <div class="field wide"><span>Also fits</span>
                        <div>
                          {#each suggestions[source.id] as target (target.id)}
                            <div class="buttons suggestion">
                              <button disabled={source.locked && !isAdmin} onclick={() => addSuggested(source, target)}>Add</button>
                              <span class="small"><span class="mono">{formatMHz(target.frequency_hz)}</span> {target.name} ({MODE_LABELS[target.modulation] ?? target.modulation.toUpperCase()})</span>
                              {#if target.warning}<span class="small st-warn">{target.warning}</span>{/if}
                            </div>
                          {/each}
                        </div>
                      </div>
                    {:else if source.channels.length && suggestions[source.id]}
                      <p class="small dim form-buttons">No other NFM, FM or AM target fits alongside these channels.</p>
                    {/if}
                    <label class="field wide"><span>Add a channel</span>
                      <select bind:value={draft.channel}>
                        <option value="">Choose a target</option>
                        {#each channelChoices(source) as target (target.id)}
                          <option value={target.id}>{target.name} ({formatMHz(target.frequency_hz)}, {MODE_LABELS[target.modulation] ?? target.modulation.toUpperCase()}){problemNote(source, target)}</option>
                        {/each}
                      </select>
                      <span class="hint">NFM, FM and AM targets.</span>
                    </label>
                    <div class="buttons form-buttons"><button class="main" disabled={!draft.channel || (source.locked && !isAdmin)} onclick={() => addChannel(source)}>Add channel</button></div>
                  {:else}
                  <label class="field wide"><span>Target</span>
                    <select bind:value={draft.target} onchange={() => (draft.editing = true)}>
                      <option value="">None</option>
                      {#each assignable(source) as target (target.id)}
                        <option value={target.id}>{target.name} ({formatMHz(target.frequency_hz)}){problemNote(source, target)}</option>
                      {/each}
                    </select>
                    {#if draft.target !== '' && stickProblems(source, targets.find((t) => t.id === Number(draft.target))).length}
                      <span class="hint st-warn">This SDR may not suit that target: {stickProblems(source, targets.find((t) => t.id === Number(draft.target))).join(', ')}. You can still assign it.</span>
                    {:else if draft.target !== '' && antennaUsable(source, targets.find((t) => t.id === Number(draft.target)))}
                      <span class="hint">Its antenna isn't ideal at that frequency.</span>
                    {/if}
                  </label>
                  <div class="buttons form-buttons"><button class="main" disabled={source.locked && !isAdmin} onclick={() => assign(source)}>Assign</button></div>
                  {/if}
                  {#if source.locked && !isAdmin}<p class="small st-warn">Locked by an administrator.</p>{/if}
                  {#if isAdmin}
                    <div class="form-buttons">
                      <label class="tick"><input type="checkbox" checked={source.locked} disabled={!source.base_recorder_id} onchange={(event) => setLocked(source, event.target)} /> Lock this assignment, so only administrators can change it</label>
                      {#if !source.base_recorder_id}<span class="small dim">Assign a target first.</span>{/if}
                    </div>
                  {/if}
                </details>
                {@const m = measures[source.id]}
                {#if !source.active_target || source.active_target.kind === 'audio' || source.profiles.length}
                <details>
                  <summary class="small" style="padding:.3rem 0">Squelch profiles{source.profiles.length ? ` (${source.profiles.length} saved)` : ''}</summary>
                  <p class="small dim form-buttons">A profile sets squelch and gain for one target on this SDR.</p>
                  {#each source.profiles as saved (saved.target_id)}
                    <div class="small" style="margin:.4rem 0">
                      <strong>{targetName(saved.target_id)}</strong>: squelch {saved.squelch == null ? 'off' : `${saved.squelch} dB`}, gain {gainLabel(saved.gain_db)}{saved.noise_level !== null ? `, noise ${saved.noise_level} dB on ${formatTime(saved.measured_at)}` : ''}
                      <div class="buttons" style="margin:.2rem 0"><button onclick={() => removeProfile(source, saved.target_id)}>Remove</button></div>
                    </div>
                  {/each}
                  <label class="field"><span>Target</span>
                    <select bind:value={m.target} onchange={() => chooseTarget(source)}>
                      <option value="">Choose a target</option>
                      {#each audioTargets() as target (target.id)}
                        <option value={target.id}>{target.name} ({formatMHz(target.frequency_hz)}, {target.modulation.toUpperCase()})</option>
                      {/each}
                    </select>
                  </label>
                  {#if source.multichannel}
                    <div class="field"><span>Gain</span>
                      <span class="mono">{gainLabel(source.gain_db)}</span>
                      <span class="hint">Multi-channel SDRs use one gain, set under Admin &gt; SDR sources.</span>
                    </div>
                  {:else}
                  <label class="field"><span>Gain (dB)</span>
                    <GainField steps={source.gain_steps} bind:value={m.gain} />
                    {#if nearestStep(source.gain_steps, m.gain) !== null}<span class="hint st-warn">Not one of this stick's gain steps. It will use {nearestStep(source.gain_steps, m.gain)} dB. Choose a step to save.</span>{/if}
                    <span class="hint">{source.gain_steps.length ? 'AGC is automatic gain.' : 'Empty is automatic gain (AGC).'}</span>
                  </label>
                  {/if}
                  <p class="small dim form-buttons">Measure while nothing is transmitting on this frequency.</p>
                  <div class="buttons form-buttons"><button disabled={m.target === '' || m.busy} onclick={() => measure(source)}>{m.busy ? 'Measuring, about 10 seconds...' : 'Measure noise'}</button></div>
                  {#if m.result}
                    <div class="msg" role="status">
                      Noise: lowest {m.result.low} dB, typical {m.result.typical} dB, highest {m.result.high} dB. Suggested squelch: <strong>{m.result.suggested} dB</strong>.
                      {#if m.result.live}<br /><span class="small">Read live from the running channel at {gainLabel(m.result.gain_db)}, without pausing the SDR.</span>{/if}
                      {#if m.result.moved}<br />The level moved a lot, so something may have transmitted. Measure again if this looks wrong.{/if}
                    </div>
                  {/if}
                  <label class="field"><span>Squelch (dB)</span>
                    <input class="mono" type="number" min="-130" max="0" step="0.1" bind:value={m.squelch} />
                    <span class="hint">Same scale as the tuner's meter.</span>
                  </label>
                  <div class="buttons form-buttons"><button class="main" disabled={m.target === '' || m.squelch === '' || m.squelch === null} onclick={() => saveProfile(source)}>Save profile</button></div>
                </details>
                {/if}
              {/if}
            </td>
          </tr>
        </tbody>
{/snippet}

<style>
  .channel-line { margin-bottom: .25rem; }
  .suggestion { margin: .15rem 0; flex-wrap: wrap; }
  .channel-line .link-button { min-height: 0; margin-left: .3rem; }
  .group-line { display: flex; flex-wrap: wrap; align-items: center; gap: 0 .6rem; }
  .group-line button { text-decoration: none; }
</style>
