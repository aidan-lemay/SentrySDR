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
      notices[source.id] = reply?.warning ? { kind: 'warn', text: reply.warning } : { kind: '', text: success };
      await refresh();
      return true;
    } catch (failure) {
      const text = failure.detail === 'locked' ? 'This source is locked. Enter its lock password.' : failure.message;
      notices[source.id] = { kind: 'error', text };
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
      notices[source.id] = { kind: 'error', text: failure.message };
    }
  }

  const schedulesFor = (source) => recorders.filter((r) => r.source_id === source.id && r.schedule_days);
  const stateClass = (state) => ({ listening: 'ok', error: 'bad', offline: 'warn' }[state] ?? '');
</script>

<div class="page-head">
  <h1>Sources</h1>
  <span class="muted small">{sources.filter((s) => s.in_call).length} on the air · {sources.length} total</span>
</div>

{#if error}<div class="notice error" role="alert">{error}</div>{/if}

{#if loaded && !sources.length}
  <div class="panel empty">
    No sources are defined yet.
    {#if isAdmin}Add one under <a href="#/admin">Admin</a>.{:else}Ask an administrator to add one.{/if}
  </div>
{/if}

<div class="grid">
  {#each sources as source (source.id)}
    {@const draft = drafts[source.id]}
    <section class="panel stack">
      <div class="row spread">
        <div>
          <h2>{source.name}</h2>
          <div class="muted small">{source.node_name} · {source.antenna || 'Antenna not set'}</div>
        </div>
        <div class="row">
          {#if source.in_call}<span class="pill live">On air</span>{/if}
          {#if source.locked}<span class="pill warn">Locked</span>{/if}
          <span class="pill {stateClass(source.state)}">{STATE_LABELS[source.state] ?? source.state}</span>
        </div>
      </div>

      {#if source.active_target}
        <div>
          <div class="freq">{formatMHz(source.active_target.frequency_hz)}</div>
          <div>
            {source.active_target.name}
            <span class="muted small">· {source.active_target.modulation.toUpperCase()}</span>
            {#if source.active_is_schedule}<span class="pill warn">Schedule: {source.active_recorder_name}</span>{/if}
          </div>
        </div>
      {:else}
        <div class="muted">Unassigned</div>
      {/if}

      {#if source.error}<div class="notice error small">{source.error}</div>{/if}
      {#if source.last_call_at}<div class="muted small">Last activity {formatTime(source.last_call_at)}</div>{/if}
      {#if schedulesFor(source).length}
        <div class="muted small">{schedulesFor(source).length} schedule{schedulesFor(source).length > 1 ? 's' : ''} set — <a href="#/schedule">view</a></div>
      {/if}

      {#if notices[source.id]}
        <div class="notice small {notices[source.id].kind}" role="status">{notices[source.id].text}</div>
      {/if}

      <div class="row">
        {#if can('listen_live') && source.active_target?.kind === 'audio'}
          <button onclick={() => (listening = listening === source.id ? null : source.id)}>
            {listening === source.id ? 'Stop listening' : 'Listen live'}
          </button>
        {/if}
        {#if can('listen_recordings')}
          <button onclick={() => toggleRecent(source)}>{recent[source.id] ? 'Hide calls' : 'Recent calls'}</button>
        {/if}
      </div>
      {#if listening === source.id}
        <!-- svelte-ignore a11y_media_has_caption -->
        <audio src={`api/live/${source.id}`} controls autoplay></audio>
      {/if}
      {#if recent[source.id]}
        <div>
          {#each recent[source.id] as call (call.id)}
            <div class="call">
              <div class="small">{formatTime(call.started_at)} · {formatDuration(call.duration)} · {call.target_name}</div>
              <!-- svelte-ignore a11y_media_has_caption -->
              <audio src={`api/recordings/${call.id}/audio`} controls preload="none"></audio>
            </div>
          {:else}
            <div class="muted small">No calls recorded on this source yet.</div>
          {/each}
          <a class="small" href={`#/recordings?source_id=${source.id}`}>All calls from this source</a>
        </div>
      {/if}

      {#if can('assign') && draft}
        <div class="fields">
          <label>Target
            <select bind:value={draft.target} onchange={() => (draft.editing = true)}>
              <option value="">None (unassigned)</option>
              {#each targets as target (target.id)}
                <option value={target.id}>{target.name} — {formatMHz(target.frequency_hz)}</option>
              {/each}
            </select>
          </label>
          {#if source.locked && !isAdmin}
            <label>Lock password <input type="password" bind:value={draft.password} autocomplete="off" /></label>
          {/if}
          <button class="primary" onclick={() => assign(source)}>{source.locked && isAdmin ? 'Override' : 'Assign'}</button>
        </div>
      {/if}

      {#if isAdmin && source.base_recorder_id}
        {#if source.locked}
          <div><button onclick={() => unlock(source)}>Remove lock</button></div>
        {:else if draft}
          <div class="fields">
            <label>New lock password <input type="password" bind:value={draft.lock} minlength="4" autocomplete="new-password" /></label>
            <button disabled={draft.lock.length < 4} onclick={() => lock(source)}>Lock</button>
          </div>
        {/if}
      {/if}
    </section>
  {/each}
</div>
