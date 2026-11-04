<script>
  import { onMount } from 'svelte';
  import { api, formatMHz, MODULATIONS } from '../lib.js';

  const blank = () => ({
    id: null, name: '', kind: 'audio', mhz: '', modulation: 'nfm', squelch: 50,
    clearcut_enabled: false, clearcut_system: '', clearcut_id: '', rdio_enabled: false, rdio_system: '', rdio_talkgroup: '',
  });
  // These frequencies are the standard ones each decoder listens on.
  const FIXED = { adsb: { modulation: 'adsb', mhz: 1090 }, ais: { modulation: 'ais', mhz: 162 } };

  let targets = $state([]);
  let form = $state(blank());
  let error = $state('');
  let message = $state('');

  async function refresh() {
    try {
      targets = await api('targets');
    } catch (failure) {
      error = failure.message;
    }
  }

  onMount(refresh);

  const optionalInt = (value) => (value === '' || value === null ? null : Number(value));

  async function save(event) {
    event.preventDefault();
    error = '';
    message = '';
    const fixed = FIXED[form.kind];
    const body = {
      name: form.name, kind: form.kind,
      frequency_hz: Math.round(Number(fixed ? fixed.mhz : form.mhz) * 1e6),
      modulation: fixed ? fixed.modulation : form.modulation,
      squelch: Number(form.squelch),
      clearcut_enabled: form.clearcut_enabled, clearcut_system: form.clearcut_system || null, clearcut_id: optionalInt(form.clearcut_id),
      rdio_enabled: form.rdio_enabled, rdio_system: optionalInt(form.rdio_system), rdio_talkgroup: optionalInt(form.rdio_talkgroup),
    };
    try {
      const saved = form.id ? await api(`targets/${form.id}`, { method: 'PUT', body }) : await api('targets', { method: 'POST', body });
      message = saved.profiles_cleared
        ? `Target saved. Its frequency or mode changed, so ${saved.profiles_cleared} saved SDR profile(s) were removed. Measure again on the Sources page.`
        : 'Target saved.';
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
      rdio_system: target.rdio_system ?? '', rdio_talkgroup: target.rdio_talkgroup ?? '',
    };
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
{#if message}<div class="msg" role="status">{message}</div>{/if}

<div class="box">
  <table class="table">
    <thead><tr><th>Name</th><th>Frequency</th><th>Mode</th><th>Squelch</th><th>Uploads to</th><th>Actions</th></tr></thead>
    <tbody>
      {#each targets as target (target.id)}
        <tr>
          <td class="title-cell">{target.name}</td>
          <td data-label="Frequency"><span class="mono">{formatMHz(target.frequency_hz)}</span></td>
          <td data-label="Mode"><span>{target.modulation.toUpperCase()}</span></td>
          <td data-label="Squelch"><span class="mono">{target.kind === 'audio' ? target.squelch || 'Off' : 'n/a'}</span></td>
          <td data-label="Uploads to"><span>{[target.clearcut_enabled && 'ClearCut', target.rdio_enabled && 'RDIO'].filter(Boolean).join(', ') || 'None'}</span></td>
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
      </select>
    </label>
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
    {/if}
    <div class="buttons form-buttons">
      <button class="main">Save</button>
      {#if form.id}<button type="button" onclick={() => (form = blank())}>Cancel</button>{/if}
    </div>
  </fieldset>
</form>
