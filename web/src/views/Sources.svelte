<script>
  import { onMount } from 'svelte';
  import { api, DEFAULT_GAIN_DB, feedLabel, formatMHz, formatTime, formatDuration, nearestStep, problemNote, STATE_LABELS, stickProblems } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { NOTICE_MS } from '../notice.svelte.js';
  import GainField from '../GainField.svelte';

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
  const MODE_LABELS = { adsb: 'ADS-B', rtltcp: 'rtl_tcp' };
  // What to paste into GQRX's Device string. The buffer settings help over a network.
  const gqrxString = (address) => `rtl_tcp=${address},buffers=16,psize=16384`;
  const wildcard = (address) => /^(0\.0\.0\.0|::|\[::\]):/.test(address);
  // Only administrators can put an SDR on rtl_tcp. The current one stays listed so the picker shows it.
  const assignable = (source) => targets.filter((t) => t.kind !== 'rtltcp' || isAdmin || t.id === source.base_target_id);
  // Only voice targets make calls. An idle SDR still offers the calls it made before.
  const records = (source) => !source.active_target || source.active_target.kind === 'audio';

  async function refresh() {
    try {
      [sources, targets, recorders] = await Promise.all([
        api('sources'), api('targets'), api('recorders'), ...Object.keys(callsOpen).map((id) => loadRecent(id)),
      ]);
      for (const source of sources) {
        drafts[source.id] ??= { target: '', editing: false };
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

  onMount(() => {
    refresh();
    const timer = setInterval(poll, 3000);
    return () => clearInterval(timer);
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
  function chooseTarget(source) {
    const m = measures[source.id];
    const saved = profileFor(source, m.target);
    m.gain = saved ? (saved.gain_db ?? '') : (source.gain_db ?? DEFAULT_GAIN_DB);
    if (!saved && source.gain_db == null) m.gain = nearestStep(source.gain_steps, m.gain) ?? m.gain;  // the default may not be a step
    m.squelch = saved ? saved.squelch : '';
    m.result = null;
  }

  async function measure(source) {
    const m = measures[source.id];
    const running = source.active_target ? ` It will stop listening on ${source.active_target.name} for about 10 seconds, and any call in progress ends there.` : '';
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
  async function loadRecent(id) {
    const reply = await api(`recordings?source_id=${id}&limit=5`);
    if (callsOpen[id]) recent[id] = reply.items;  // unless it was closed meanwhile
  }

  async function toggleRecent(source) {
    if (callsOpen[source.id]) {
      delete callsOpen[source.id];
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
      {#each sources as source (source.id)}
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
                {#if source.active_target}
                  <span class="mono nowrap">{formatMHz(source.active_target.frequency_hz)}</span><br />
                  {source.active_target.name} ({MODE_LABELS[source.active_target.modulation] ?? source.active_target.modulation.toUpperCase()})
                  {#if source.active_target.kind === 'audio'}
                    <br /><span class="small">Squelch {source.active_target.squelch || 'off'}, gain {gainLabel(source.active_target.gain_db)}{source.active_target.from_profile ? ', saved profile' : ''}</span>
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
                {#if source.error}<br /><span class="st-bad small">{source.error}</span>{/if}
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
            <td data-label="Last activity"><span class="small">{formatTime(source.last_call_at)}</span></td>
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
              {#if recent[source.id]}
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
                <details>
                  <summary class="small" style="padding:.3rem 0">Change assignment{scheduleCount(source) ? ` (${scheduleCount(source)} on the schedule)` : ''}</summary>
                  <label class="field wide"><span>Target</span>
                    <select bind:value={draft.target} onchange={() => (draft.editing = true)}>
                      <option value="">None</option>
                      {#each assignable(source) as target (target.id)}
                        <option value={target.id}>{target.name} ({formatMHz(target.frequency_hz)}){problemNote(source, target)}</option>
                      {/each}
                    </select>
                    {#if draft.target !== '' && stickProblems(source, targets.find((t) => t.id === Number(draft.target))).length}
                      <span class="hint st-warn">This SDR may not suit that target: {stickProblems(source, targets.find((t) => t.id === Number(draft.target))).join(', ')}. You can still assign it.</span>
                    {/if}
                  </label>
                  <div class="buttons form-buttons"><button class="main" disabled={source.locked && !isAdmin} onclick={() => assign(source)}>Assign</button></div>
                  {#if source.locked && !isAdmin}<p class="small st-warn">This assignment is locked. Only an administrator can change it.</p>{/if}
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
                  <p class="small dim form-buttons">Noise differs by stick, frequency and gain. A saved profile sets the squelch and gain for one target on this SDR.</p>
                  {#each source.profiles as saved (saved.target_id)}
                    <div class="small" style="margin:.4rem 0">
                      <strong>{targetName(saved.target_id)}</strong>: squelch {saved.squelch}, gain {gainLabel(saved.gain_db)}{saved.noise_level !== null ? `, noise ${Math.round(saved.noise_level)} on ${formatTime(saved.measured_at)}` : ''}
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
                  <label class="field"><span>Gain (dB)</span>
                    <GainField steps={source.gain_steps} bind:value={m.gain} />
                    {#if nearestStep(source.gain_steps, m.gain) !== null}<span class="hint st-warn">Not one of this stick's gain steps. It will use {nearestStep(source.gain_steps, m.gain)} dB. Choose a step to save.</span>{/if}
                    <span class="hint">{source.gain_steps.length ? 'AGC is automatic gain.' : 'Leave empty for automatic gain (AGC). 0 means a real 0 dB, which may miss most calls.'} A fixed gain usually gives a steadier noise level.</span>
                  </label>
                  <p class="small dim form-buttons">Measure while nothing is transmitting on this frequency.</p>
                  <div class="buttons form-buttons"><button disabled={m.target === '' || m.busy} onclick={() => measure(source)}>{m.busy ? 'Measuring, about 10 seconds...' : 'Measure noise'}</button></div>
                  {#if m.result}
                    <div class="msg" role="status">
                      Noise: lowest {m.result.low}, typical {m.result.typical}, highest {m.result.high}. Suggested squelch: <strong>{m.result.suggested}</strong>.
                      {#if m.result.moved}<br />The level moved a lot, so something may have transmitted. Measure again if this looks wrong.{/if}
                    </div>
                  {/if}
                  <label class="field"><span>Squelch</span>
                    <input class="mono" type="number" inputmode="numeric" min="0" max="100000" bind:value={m.squelch} />
                  </label>
                  <div class="buttons form-buttons"><button class="main" disabled={m.target === '' || m.squelch === '' || m.squelch === null} onclick={() => saveProfile(source)}>Save profile</button></div>
                </details>
                {/if}
              {/if}
            </td>
          </tr>
        </tbody>
      {/each}
    </table>
  </div>
{/if}
