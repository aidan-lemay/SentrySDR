<script>
  import { onMount } from 'svelte';
  import { api, DAYS, formatMHz, formatSchedule } from '../lib.js';

  let { user } = $props();
  const canEdit = user.permissions.includes('assign');
  const blank = () => ({ id: null, name: '', source_id: '', target_id: '', days: [], start: '22:00', end: '23:00', enabled: true, lock_password: '' });

  let recorders = $state([]);
  let sources = $state([]);
  let targets = $state([]);
  let form = $state(blank());
  let error = $state('');
  let message = $state('');
  let tz = $state('');

  const selectedLocked = $derived(sources.find((s) => s.id === Number(form.source_id))?.locked && user.role !== 'admin');
  const scheduled = $derived(recorders.filter((r) => r.schedule_days));
  const alwaysOn = $derived(recorders.filter((r) => !r.schedule_days));

  async function refresh() {
    try {
      [recorders, sources, targets] = await Promise.all([api('recorders'), api('sources'), api('targets')]);
    } catch (failure) {
      error = failure.message;
    }
  }

  onMount(async () => {
    refresh();
    tz = (await api('me').catch(() => null))?.timezone ?? '';
  });

  async function save(event) {
    event.preventDefault();
    error = '';
    message = '';
    const body = {
      name: form.name, source_id: Number(form.source_id), target_id: Number(form.target_id), enabled: form.enabled,
      schedule_days: form.days, schedule_start: form.start, schedule_end: form.end,
      lock_password: form.lock_password || null,
    };
    try {
      const reply = form.id
        ? await api(`recorders/${form.id}`, { method: 'PUT', body })
        : await api('recorders', { method: 'POST', body });
      message = reply.warning ?? 'Schedule saved.';
      form = blank();
      await refresh();
    } catch (failure) {
      error = failure.detail === 'locked' ? 'This source is locked. Enter its lock password.' : failure.message;
    }
  }

  function edit(recorder) {
    form = {
      id: recorder.id, name: recorder.name, source_id: recorder.source_id, target_id: recorder.target_id,
      days: [...recorder.schedule_days], start: recorder.schedule_start, end: recorder.schedule_end,
      enabled: recorder.enabled, lock_password: '',
    };
  }

  async function remove(recorder) {
    error = '';
    try {
      await api(`recorders/${recorder.id}`, {
        method: 'DELETE',
        headers: form.lock_password ? { 'X-Lock-Password': form.lock_password } : {},
      });
      await refresh();
    } catch (failure) {
      error = failure.detail === 'locked' ? 'This source is locked. Enter its lock password in the form, then remove again.' : failure.message;
    }
  }

  function toggleDay(day) {
    form.days = form.days.includes(day) ? form.days.filter((d) => d !== day) : [...form.days, day].sort();
  }
</script>

<div class="page-head">
  <h1>Schedule</h1>
  {#if tz}<span class="muted small">Times are in {tz}</span>{/if}
</div>

{#if error}<div class="notice error" role="alert">{error}</div>{/if}
{#if message}<div class="notice" role="status">{message}</div>{/if}

<section class="panel">
  <h2 style="margin-bottom:.5rem">Scheduled recorders</h2>
  <p class="muted small" style="margin-top:0">A schedule takes over its source for the window, then the source returns to its assigned target.</p>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Name</th><th>Source</th><th>Target</th><th>When</th><th></th></tr></thead>
      <tbody>
        {#each scheduled as recorder (recorder.id)}
          <tr>
            <td>{recorder.name}{#if !recorder.enabled} <span class="pill warn">Off</span>{/if}</td>
            <td>{recorder.source_name}</td>
            <td>{recorder.target_name} <span class="muted mono small">{formatMHz(recorder.frequency_hz)}</span></td>
            <td>{formatSchedule(recorder)}</td>
            <td>
              {#if canEdit}
                <div class="row">
                  <button onclick={() => edit(recorder)}>Edit</button>
                  <button class="danger" onclick={() => remove(recorder)}>Remove</button>
                </div>
              {/if}
            </td>
          </tr>
        {:else}
          <tr><td colspan="5" class="muted">No schedules yet.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>

{#if alwaysOn.length}
  <section class="panel">
    <h2 style="margin-bottom:.5rem">Always-on assignments</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Source</th><th>Target</th><th>Assigned by</th></tr></thead>
        <tbody>
          {#each alwaysOn as recorder (recorder.id)}
            <tr><td>{recorder.source_name}</td><td>{recorder.target_name} <span class="muted mono small">{formatMHz(recorder.frequency_hz)}</span></td><td>{recorder.assigned_by}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
{/if}

{#if canEdit}
  <form class="panel stack" onsubmit={save}>
    <h2>{form.id ? 'Edit schedule' : 'New schedule'}</h2>
    <div class="fields">
      <label>Name <input bind:value={form.name} required maxlength="120" placeholder="Tuesday net" /></label>
      <label>Source
        <select bind:value={form.source_id} required>
          <option value="" disabled>Choose a source</option>
          {#each sources as source (source.id)}<option value={source.id}>{source.name}</option>{/each}
        </select>
      </label>
      <label>Target
        <select bind:value={form.target_id} required>
          <option value="" disabled>Choose a target</option>
          {#each targets as target (target.id)}<option value={target.id}>{target.name} — {formatMHz(target.frequency_hz)}</option>{/each}
        </select>
      </label>
      <label>Start <input type="time" bind:value={form.start} required /></label>
      <label>End <input type="time" bind:value={form.end} required /></label>
    </div>
    <fieldset style="border:none;padding:0;margin:0">
      <legend class="small muted">Days</legend>
      <div class="days">
        {#each DAYS as day, index}
          <label><input type="checkbox" checked={form.days.includes(index)} onchange={() => toggleDay(index)} /> {day}</label>
        {/each}
      </div>
    </fieldset>
    <label class="check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
    {#if selectedLocked}
      <label>Lock password for this source <input type="password" bind:value={form.lock_password} autocomplete="off" /></label>
    {/if}
    <div class="row">
      <button class="primary" disabled={!form.days.length}>Save schedule</button>
      {#if form.id}<button type="button" onclick={() => (form = blank())}>Cancel</button>{/if}
    </div>
  </form>
{/if}
