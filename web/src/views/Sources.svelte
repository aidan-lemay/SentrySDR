<script>
  import { onMount } from 'svelte';
  import { api, formatMHz, formatTime, formatDuration, STATE_LABELS } from '../lib.js';

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

  const can = (permission) => user.permissions.includes(permission);
  const isAdmin = user.role === 'admin';

  async function refresh() {
    try {
      [sources, targets, recorders] = await Promise.all([api('sources'), api('targets'), api('recorders')]);
      for (const source of sources) {
        drafts[source.id] ??= { target: '', password: '', lock: '', editing: false };
        // Follow changes made elsewhere, unless this user is mid-selection.
        if (!drafts[source.id].editing) drafts[source.id].target = source.base_target_id ?? '';
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
              {/if}
            </td>
          </tr>
        </tbody>
      {/each}
    </table>
  </div>
{/if}
