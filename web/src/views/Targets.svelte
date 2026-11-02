<script>
  import { onMount } from 'svelte';
  import { api, formatMHz, MODULATIONS } from '../lib.js';

  const blank = () => ({
    id: null, name: '', kind: 'audio', mhz: '', modulation: 'nfm', squelch: 50,
    clearcut_enabled: false, clearcut_id: '', rdio_enabled: false, rdio_system: '', rdio_talkgroup: '',
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
      clearcut_enabled: form.clearcut_enabled, clearcut_id: optionalInt(form.clearcut_id),
      rdio_enabled: form.rdio_enabled, rdio_system: optionalInt(form.rdio_system), rdio_talkgroup: optionalInt(form.rdio_talkgroup),
    };
    try {
      if (form.id) await api(`targets/${form.id}`, { method: 'PUT', body });
      else await api('targets', { method: 'POST', body });
      message = 'Target saved.';
      form = blank();
      await refresh();
    } catch (failure) {
      error = failure.message;
    }
  }

  function edit(target) {
    form = {
      ...blank(), ...target, mhz: target.frequency_hz / 1e6,
      clearcut_id: target.clearcut_id ?? '', rdio_system: target.rdio_system ?? '', rdio_talkgroup: target.rdio_talkgroup ?? '',
    };
  }

  async function remove(target) {
    error = '';
    try {
      await api(`targets/${target.id}`, { method: 'DELETE' });
      await refresh();
    } catch (failure) {
      error = failure.message;
    }
  }
</script>

<div class="page-head"><h1>Targets</h1></div>
{#if error}<div class="notice error" role="alert">{error}</div>{/if}
{#if message}<div class="notice" role="status">{message}</div>{/if}

<section class="panel table-wrap">
  <table>
    <thead><tr><th>Name</th><th>Frequency</th><th>Mode</th><th>Squelch</th><th>Uploads</th><th></th></tr></thead>
    <tbody>
      {#each targets as target (target.id)}
        <tr>
          <td>{target.name}</td>
          <td class="mono">{formatMHz(target.frequency_hz)}</td>
          <td>{target.modulation.toUpperCase()}</td>
          <td class="mono">{target.kind === 'audio' ? target.squelch || 'Off' : '—'}</td>
          <td>
            {#if target.clearcut_enabled}<span class="pill">ClearCut</span>{/if}
            {#if target.rdio_enabled}<span class="pill">RDIO</span>{/if}
          </td>
          <td><div class="row"><button onclick={() => edit(target)}>Edit</button><button class="danger" onclick={() => remove(target)}>Delete</button></div></td>
        </tr>
      {:else}
        <tr><td colspan="6" class="muted">No targets yet. Add one below.</td></tr>
      {/each}
    </tbody>
  </table>
</section>

<form class="panel stack" onsubmit={save}>
  <h2>{form.id ? 'Edit target' : 'New target'}</h2>
  <div class="fields">
    <label>Name / alpha tag <input bind:value={form.name} required maxlength="120" placeholder="K1ABC" /></label>
    <label>Type
      <select bind:value={form.kind}>
        <option value="audio">Voice calls</option>
        <option value="adsb">ADS-B aircraft (1090 MHz)</option>
        <option value="ais">AIS ships (162 MHz)</option>
      </select>
    </label>
    {#if form.kind === 'audio'}
      <label>Frequency (MHz) <input type="number" step="0.0001" min="0.5" max="2200" bind:value={form.mhz} required class="mono" /></label>
      <label>Modulation
        <select bind:value={form.modulation}>
          {#each MODULATIONS as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
        </select>
      </label>
      <label>Squelch level <input type="number" min="0" max="100000" bind:value={form.squelch} class="mono" /></label>
    {/if}
  </div>
  {#if form.kind === 'audio'}
    <p class="muted small" style="margin:0">Squelch decides what counts as a call. Raise it if noise keeps recording; 0 records continuously. Digital modes never decode encrypted traffic.</p>
    <div class="fields">
      <label class="check"><input type="checkbox" bind:checked={form.clearcut_enabled} /> Upload to ClearCut</label>
      <label>ClearCut ID <input type="number" bind:value={form.clearcut_id} disabled={!form.clearcut_enabled} required={form.clearcut_enabled} /></label>
    </div>
    <div class="fields">
      <label class="check"><input type="checkbox" bind:checked={form.rdio_enabled} /> Upload to RDIO</label>
      <label>RDIO system <input type="number" bind:value={form.rdio_system} disabled={!form.rdio_enabled} required={form.rdio_enabled} /></label>
      <label>RDIO talkgroup <input type="number" bind:value={form.rdio_talkgroup} disabled={!form.rdio_enabled} required={form.rdio_enabled} /></label>
    </div>
  {/if}
  <div class="row">
    <button class="primary">Save target</button>
    {#if form.id}<button type="button" onclick={() => (form = blank())}>Cancel</button>{/if}
  </div>
</form>
