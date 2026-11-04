<script>
  import { onMount } from 'svelte';
  import { api, DEFAULT_GAIN_DB, formatMHz, formatTime, formatDuration, STATE_LABELS } from '../lib.js';

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

  const can = (permission) => user.permissions.includes(permission);
  const isAdmin = user.role === 'admin';

  async function refresh() {
    try {
      [sources, targets, recorders] = await Promise.all([api('sources'), api('targets'), api('recorders')]);
      for (const source of sources) {
        drafts[source.id] ??= { target: '', password: '', lock: '', editing: false };
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

  onMount(() => {
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  });

  async function act(source, path, options, success) {
    notices[source.id] = null;
    try {
      const reply = await api(path, options);
      notices[source.id] = reply?.warning ? { kind: 'warn', text: reply.warning } : { kind: 'msg', text: success };
      await refresh();
      return true;
    } catch (failure) {
      const text = failure.detail === 'locked' ? 'This source is locked. Enter its lock password.' : failure.message;
      notices[source.id] = { kind: 'err', text };
      return false;
    }
  }

  async function assign(source) {
    const draft = drafts[source.id];
    const done = await act(source, `sources/${source.id}/assign`, {
      method: 'POST',
      body: { target_id: draft.target === '' ? null : Number(draft.target), lock_password: draft.password || null },
    }, draft.target === '' ? 'Target cleared.' : 'Target assigned.');
    if (done) {
      draft.password = '';
      draft.editing = false;
      await refresh();
    }
  }

  async function lock(source) {
    const draft = drafts[source.id];
    if (await act(source, `sources/${source.id}/lock`, { method: 'POST', body: { password: draft.lock } }, 'Locked.')) {
      draft.lock = '';
    }
  }

  const unlock = (source) => act(source, `sources/${source.id}/lock`, { method: 'DELETE' }, 'Unlocked.');

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
    m.squelch = saved ? saved.squelch : '';
    m.result = null;
  }

  async function measure(source) {
    const m = measures[source.id];
    const running = source.active_target ? ` It will stop listening on ${source.active_target.name} for about 10 seconds, and any call in progress ends there.` : '';
    if (running && !confirm(`Measure noise on ${source.name}?${running}`)) return;
    m.busy = true;
    m.result = null;
    notices[source.id] = null;
    try {
      m.result = await api(`sources/${source.id}/measure`, { method: 'POST', body: { target_id: Number(m.target), gain_db: optionalNumber(m.gain) } });
      m.squelch = m.result.suggested;
    } catch (failure) {
      notices[source.id] = { kind: 'err', text: failure.message };
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

  async function toggleRecent(source) {
    if (recent[source.id]) {
      delete recent[source.id];
      return;
    }
    try {
      const reply = await api(`recordings?source_id=${source.id}&limit=5`);
      recent[source.id] = reply.items;
    } catch (failure) {
      notices[source.id] = { kind: 'err', text: failure.message };
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
              <div class="small dim" style="font-weight:normal">{source.node_name}{source.antenna ? `, ${source.antenna}` : ''}</div>
            </td>
            <td data-label="Tuned to">
              <div>
                {#if source.active_target}
                  <span class="mono nowrap">{formatMHz(source.active_target.frequency_hz)}</span><br />
                  {source.active_target.name} ({source.active_target.modulation.toUpperCase()})
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
              </div>
            </td>
            <td data-label="Last activity"><span class="small">{formatTime(source.last_call_at)}</span></td>
            <td>
              <div class="buttons" style="margin:0">
                {#if can('listen_live') && source.active_target?.kind === 'audio'}
                  <button onclick={() => (listening = listening === source.id ? null : source.id)}>{listening === source.id ? 'Stop' : 'Listen'}</button>
                {/if}
                {#if can('listen_recordings')}
                  <button onclick={() => toggleRecent(source)}>{recent[source.id] ? 'Hide calls' : 'Calls'}</button>
                {/if}
              </div>
            </td>
          </tr>
          <tr>
            <td colspan="5">
              {#if notices[source.id]}<div class={notices[source.id].kind} role="status">{notices[source.id].text}</div>{/if}
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
                  <label class="field"><span>Target</span>
                    <select bind:value={draft.target} onchange={() => (draft.editing = true)}>
                      <option value="">None</option>
                      {#each targets as target (target.id)}
                        <option value={target.id}>{target.name} ({formatMHz(target.frequency_hz)})</option>
                      {/each}
                    </select>
                  </label>
                  {#if source.locked && !isAdmin}
                    <label class="field"><span>Lock password</span><input type="password" bind:value={draft.password} autocomplete="off" /></label>
                  {/if}
                  <div class="buttons form-buttons"><button class="main" onclick={() => assign(source)}>{source.locked && isAdmin ? 'Override lock and assign' : 'Assign'}</button></div>
                  {#if isAdmin && source.base_recorder_id}
                    {#if source.locked}
                      <div class="buttons form-buttons"><button onclick={() => unlock(source)}>Remove lock</button></div>
                    {:else}
                      <label class="field"><span>Set a lock password</span><input type="password" bind:value={draft.lock} minlength="4" autocomplete="new-password" /></label>
                      <div class="buttons form-buttons"><button disabled={draft.lock.length < 4} onclick={() => lock(source)}>Lock</button></div>
                    {/if}
                  {/if}
                </details>
                {@const m = measures[source.id]}
                <details>
                  <summary class="small" style="padding:.3rem 0">Squelch profiles{source.profiles.length ? ` (${source.profiles.length} saved)` : ''}</summary>
                  <p class="small dim">Noise differs by stick, frequency and gain. A saved profile sets the squelch and gain for one target on this SDR.</p>
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
                    <input class="mono" type="number" inputmode="decimal" min="0" max="60" step="0.1" placeholder="AGC" bind:value={m.gain} />
                    <span class="hint">Leave empty for automatic gain (AGC). A fixed gain usually gives a steadier noise level. 0 means a real 0 dB, which may miss most calls.</span>
                  </label>
                  <p class="small dim">Measure while nothing is transmitting on this frequency.</p>
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
            </td>
          </tr>
        </tbody>
      {/each}
    </table>
  </div>
{/if}
