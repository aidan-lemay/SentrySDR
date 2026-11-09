<script>
  import { onMount } from 'svelte';
  import { api, DAYS, formatMHz, formatSchedule, problemNote, stickProblems, zonedInputValue } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { clearAfter } from '../notice.svelte.js';

  let { user } = $props();
  const canEdit = user.permissions.includes('assign');
  const blank = () => ({
    id: null, name: '', source_id: '', target_id: '', repeat: 'weekly', days: [], start: '22:00', end: '23:00',
    onceStart: '', onceEnd: '', onEnd: 'return', enabled: true,
  });

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
      repeat: form.repeat, on_end: form.onEnd,
      ...(form.repeat === 'once'
        ? { once_start: form.onceStart, once_end: form.onceEnd }
        : { schedule_days: form.days, schedule_start: form.start, schedule_end: form.end }),
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
      repeat: recorder.repeat, days: [...recorder.schedule_days], start: recorder.schedule_start ?? '22:00', end: recorder.schedule_end ?? '23:00',
      onceStart: recorder.once_start ? zonedInputValue(recorder.once_start, tz) : '', onceEnd: recorder.once_end ? zonedInputValue(recorder.once_end, tz) : '',
      onEnd: recorder.on_end, enabled: recorder.enabled,
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
<p class="small">A schedule takes over its source for the times shown. Afterwards the source goes back to what it was doing, unless the schedule keeps its target.{tz ? ` Times are in ${tz}.` : ''}</p>

{#if error}<div class="err" role="alert">{error}</div>{/if}
{#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}

<div class="box">
  <h2>Scheduled recorders</h2>
  <table class="table">
    <thead><tr><th>Name</th><th>Source</th><th>Target</th><th>When</th><th>Afterwards</th><th>Actions</th></tr></thead>
    <tbody>
      {#each scheduled as recorder (recorder.id)}
        <tr>
          <td class="title-cell">{recorder.name}{#if !recorder.enabled}{' '}<span class="st-warn">(off)</span>{:else if recorder.finished}{' '}<span class="dim">(finished)</span>{:else if recorder.running}{' '}<span class="st-ok">(running)</span>{/if}</td>
          <td data-label="Source"><span>{recorder.source_name}</span></td>
          <td data-label="Target"><span>{recorder.target_name} <span class="mono small nowrap">{formatMHz(recorder.frequency_hz)}</span></span></td>
          <td data-label="When"><span>{formatSchedule(recorder, tz)}</span></td>
          <td data-label="Afterwards"><span class="small">{recorder.on_end === 'keep' ? 'Keeps this target' : 'Goes back'}</span></td>
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
        <tr><td class="empty" colspan="6">No schedules yet.</td></tr>
      {/each}
    </tbody>
  </table>
</div>

{#if alwaysOn.length}
  <div class="box">
    <h2>Always on</h2>
    <table class="table">
      <thead><tr><th>Source</th><th>Target</th>{#if canEdit}<th>Assigned by</th>{/if}</tr></thead>
      <tbody>
        {#each alwaysOn as recorder (recorder.id)}
          <tr>
            <td class="title-cell">{recorder.source_name}</td>
            <td data-label="Target"><span>{recorder.target_name} <span class="mono small nowrap">{formatMHz(recorder.frequency_hz)}</span></span></td>
            {#if canEdit}<td data-label="Assigned by"><span>{recorder.assigned_by}</span></td>{/if}
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
      <label class="field wide"><span>Target</span>
        <select bind:value={form.target_id} required>
          <option value="" disabled>Choose...</option>
          {#each targets.filter((t) => t.kind !== 'rtltcp' || user.role === 'admin' || t.id === Number(form.target_id)) as target (target.id)}<option value={target.id}>{target.name} ({formatMHz(target.frequency_hz)}){problemNote(chosenSource, target)}</option>{/each}
        </select>
        {#if form.target_id !== '' && stickProblems(chosenSource, targets.find((t) => t.id === Number(form.target_id))).length}
          <span class="hint st-warn">{chosenSource.name} may not suit that target: {stickProblems(chosenSource, targets.find((t) => t.id === Number(form.target_id))).join(', ')}. You can still schedule it.</span>
        {/if}
      </label>
      <div class="field"><span>Repeats</span>
        <div class="flags">
          <label class="tick"><input type="radio" name="repeat" value="weekly" bind:group={form.repeat} /> Every week</label>
          <label class="tick"><input type="radio" name="repeat" value="once" bind:group={form.repeat} /> Once</label>
        </div>
      </div>
      {#if form.repeat === 'once'}
        <label class="field"><span>Starts</span><input type="datetime-local" bind:value={form.onceStart} required /></label>
        <label class="field"><span>Ends</span>
          <input type="datetime-local" bind:value={form.onceEnd} min={form.onceStart || undefined} required />
          <span class="hint">{tz ? `In ${tz}, the server's time zone.` : ''} Up to 31 days long. It stays listed as finished afterwards.</span>
        </label>
      {:else}
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
      {/if}
      <label class="field"><span>When it ends</span>
        <select bind:value={form.onEnd}>
          <option value="return">Go back</option>
          <option value="keep">Keep this target</option>
        </select>
        <span class="hint">{form.onEnd === 'keep' ? 'When the time runs out, this target becomes the source\'s normal assignment, even if it had none. Turning the schedule off early doesn\'t.' : (chosenSource && !chosenSource.base_target_id ? 'The source goes back to having no assignment.' : 'The source goes back to its normal assignment.')}</span>
      </label>
      <div class="field">
        <span>Enabled</span>
        <label class="tick"><input type="checkbox" bind:checked={form.enabled} /> Run this schedule</label>
      </div>
      {#if selectedLocked}<p class="small st-warn">This source's assignment is locked. Only an administrator can change its schedule.</p>{/if}
      <div class="buttons form-buttons">
        <button class="main" disabled={(form.repeat === 'weekly' && !form.days.length) || selectedLocked}>Save</button>
        {#if form.id}<button type="button" onclick={() => (form = blank())}>Cancel</button>{/if}
      </div>
    </fieldset>
  </form>
{/if}
