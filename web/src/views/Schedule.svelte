<script>
  import { onMount } from 'svelte';
  import { antennaCovers, api, DAYS, formatMHz, formatSchedule } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { clearAfter } from '../notice.svelte.js';

  let { user } = $props();
  const canEdit = user.permissions.includes('assign');
  const blank = () => ({ id: null, name: '', source_id: '', target_id: '', days: [], start: '22:00', end: '23:00', enabled: true });

  let recorders = $state([]);
  let sources = $state([]);
  let targets = $state([]);
  let form = $state(blank());
  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));
  let tz = $state('');

  const chosenSource = $derived(sources.find((s) => s.id === Number(form.source_id)));
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
    };
    try {
      const reply = form.id
        ? await api(`recorders/${form.id}`, { method: 'PUT', body })
        : await api('recorders', { method: 'POST', body });
      message = reply.warning ?? 'Schedule saved.';
      form = blank();
      await refresh();
    } catch (failure) {
      error = failure.message;
    }
  }

  function edit(recorder) {
    form = {
      id: recorder.id, name: recorder.name, source_id: recorder.source_id, target_id: recorder.target_id,
      days: [...recorder.schedule_days], start: recorder.schedule_start, end: recorder.schedule_end,
      enabled: recorder.enabled,
    };
    document.getElementById('schedule-form')?.scrollIntoView();
  }

  async function remove(recorder) {
    if (!confirm(`Remove the schedule ${recorder.name}?`)) return;
    error = '';
    try {
      await api(`recorders/${recorder.id}`, { method: 'DELETE' });
      await refresh();
    } catch (failure) {
      error = failure.message;
    }
  }

  function toggleDay(day) {
    form.days = form.days.includes(day) ? form.days.filter((d) => d !== day) : [...form.days, day].sort();
  }
</script>

<h1>Schedule</h1>
<p class="small">A schedule takes over its source for the times shown, then the source goes back to its assigned target.{#if tz} Times are in {tz}.{/if}</p>

{#if error}<div class="err" role="alert">{error}</div>{/if}
{#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}

<div class="box">
  <h2>Scheduled recorders</h2>
  <table class="table">
    <thead><tr><th>Name</th><th>Source</th><th>Target</th><th>When</th><th>Actions</th></tr></thead>
    <tbody>
      {#each scheduled as recorder (recorder.id)}
        <tr>
          <td class="title-cell">{recorder.name}{#if !recorder.enabled} <span class="st-warn">(off)</span>{/if}</td>
          <td data-label="Source"><span>{recorder.source_name}</span></td>
          <td data-label="Target"><span>{recorder.target_name} <span class="mono small nowrap">{formatMHz(recorder.frequency_hz)}</span></span></td>
          <td data-label="When"><span>{formatSchedule(recorder)}</span></td>
          <td>
            {#if canEdit}
              <div class="buttons" style="margin:0">
                <button onclick={() => edit(recorder)}>Edit</button>
                <button class="bad" onclick={() => remove(recorder)}>Remove</button>
              </div>
            {/if}
          </td>
        </tr>
      {:else}
        <tr><td class="empty" colspan="5">No schedules yet.</td></tr>
      {/each}
    </tbody>
  </table>
</div>

{#if alwaysOn.length}
  <div class="box">
    <h2>Always on</h2>
    <table class="table">
      <thead><tr><th>Source</th><th>Target</th><th>Assigned by</th></tr></thead>
      <tbody>
        {#each alwaysOn as recorder (recorder.id)}
          <tr>
            <td class="title-cell">{recorder.source_name}</td>
            <td data-label="Target"><span>{recorder.target_name} <span class="mono small nowrap">{formatMHz(recorder.frequency_hz)}</span></span></td>
            <td data-label="Assigned by"><span>{recorder.assigned_by}</span></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

{#if canEdit}
  <form id="schedule-form" onsubmit={save}>
    <fieldset>
      <legend>{form.id ? 'Edit schedule' : 'Add a schedule'}</legend>
      <label class="field"><span>Name</span><input bind:value={form.name} required maxlength="120" /></label>
      <label class="field"><span>Source</span>
        <select bind:value={form.source_id} required>
          <option value="" disabled>Choose...</option>
          {#each sources as source (source.id)}<option value={source.id}>{source.name}</option>{/each}
        </select>
      </label>
      <label class="field"><span>Target</span>
        <select bind:value={form.target_id} required>
          <option value="" disabled>Choose...</option>
          {#each targets as target (target.id)}<option value={target.id}>{target.name} ({formatMHz(target.frequency_hz)}){antennaCovers(chosenSource?.antenna_ranges, target.frequency_hz) ? '' : ', outside antenna range'}</option>{/each}
        </select>
        {#if chosenSource?.antenna_name && form.target_id !== '' && !antennaCovers(chosenSource.antenna_ranges, targets.find((t) => t.id === Number(form.target_id))?.frequency_hz)}
          <span class="hint st-warn">{chosenSource.antenna_name} on {chosenSource.name} doesn't cover this frequency, so reception may be poor.</span>
        {/if}
      </label>
      <label class="field"><span>Start time</span><input type="time" bind:value={form.start} required /></label>
      <label class="field"><span>End time</span><input type="time" bind:value={form.end} required /></label>
      <div class="field">
        <span>Days</span>
        <div class="days">
          {#each DAYS as day, index}
            <label class="tick"><input type="checkbox" checked={form.days.includes(index)} onchange={() => toggleDay(index)} /> {day}</label>
          {/each}
        </div>
      </div>
      <div class="field">
        <span>Enabled</span>
        <label class="tick"><input type="checkbox" bind:checked={form.enabled} /> Run this schedule</label>
      </div>
      {#if selectedLocked}<p class="small st-warn">This source's assignment is locked. Only an administrator can change its schedule.</p>{/if}
      <div class="buttons form-buttons">
        <button class="main" disabled={!form.days.length || selectedLocked}>Save</button>
        {#if form.id}<button type="button" onclick={() => (form = blank())}>Cancel</button>{/if}
      </div>
    </fieldset>
  </form>
{/if}
