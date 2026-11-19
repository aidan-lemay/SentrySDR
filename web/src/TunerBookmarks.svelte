<script>
  // Your tuner bookmarks. They're kept with your account on the main server, so every SDR,
  // on any machine, shows the same ones.
  import { onMount } from 'svelte';
  import { api } from './lib.js';
  import Notice from './Notice.svelte';
  import { clearAfter } from './notice.svelte.js';

  let { current, onpick } = $props();
  const MODE_LABELS = { nfm: 'NFM', am: 'AM', wfm: 'WFM', wfm_stereo: 'WFM stereo', usb: 'USB', lsb: 'LSB', cw: 'CW' };
  const FORMATS = [['sentrysdr', 'SentrySDR (keeps everything)'], ['gqrx', 'GQRX bookmarks.csv'], ['sdrpp', 'SDR++ frequency manager']];

  let items = $state([]);
  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));
  let search = $state('');
  let folder = $state('');
  let name = $state('');
  let newFolder = $state('');
  let editing = $state(null);
  let importFormat = $state('sentrysdr');
  let replace = $state(false);
  let fileInput = $state();

  const folders = $derived([...new Set(items.map((b) => b.folder).filter(Boolean))].sort());
  const shown = $derived(items.filter((b) => (!folder || b.folder === folder)
    && (!search || `${b.name} ${b.notes} ${(b.frequency_hz / 1e6).toFixed(4)}`.toLowerCase().includes(search.toLowerCase()))));
  const mhz = (hz) => (hz / 1e6).toFixed(4).replace(/0{1,2}$/, '');

  async function run(action, success) {
    error = '';
    try {
      const reply = await action();
      if (success) message = typeof success === 'function' ? success(reply) : success;
      return reply ?? true;
    } catch (failure) {
      error = failure.message;
      return null;
    }
  }

  const load = async () => { items = await api('me/bookmarks'); };

  async function add(event) {
    event.preventDefault();
    if (!current.frequency_hz) return;
    const body = { name, folder: newFolder, frequency_hz: current.frequency_hz, mode: current.mode, bandwidth_hz: current.bandwidth_hz };
    if (await run(() => api('me/bookmarks', { method: 'POST', body }), 'Bookmark added.')) {
      name = '';
      await load();
    }
  }

  async function save(event) {
    event.preventDefault();
    const { id, ...body } = editing;
    body.frequency_hz = Math.round(Number(String(body.mhz).replace(',', '.')) * 1e6);
    delete body.mhz;
    if (await run(() => api(`me/bookmarks/${id}`, { method: 'PUT', body }), 'Saved.')) {
      editing = null;
      await load();
    }
  }

  async function remove(bookmark) {
    if (await run(() => api(`me/bookmarks/${bookmark.id}`, { method: 'DELETE' }), 'Removed.')) await load();
  }

  async function importFile() {
    const file = fileInput?.files?.[0];
    if (!file) {
      error = 'Choose a file first.';
      return;
    }
    if (file.size > 900_000) {
      error = 'That file is too big to be a bookmark list.';
      return;
    }
    const text = await file.text();
    const reply = await run(() => api('me/bookmarks/import', { method: 'POST', body: { format: importFormat, text, replace } }),
      (r) => `Added ${r.added}${r.skipped ? `, skipped ${r.skipped} already here` : ''}${r.left_out ? `, left out ${r.left_out} over the limit` : ''}.`);
    if (reply) {
      fileInput.value = '';
      replace = false;
      await load();
    }
  }

  onMount(() => { load().catch((failure) => (error = failure.message)); });
</script>

<fieldset>
  <legend>Bookmarks</legend>
  {#if error}<div class="err" role="alert">{error}</div>{/if}
  {#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}
  <p class="small">Yours, on every SDR and every machine. Choosing one outside the span moves the span there if you're the one tuning.</p>
  <div class="search">
    <div class="pair">
      <input type="search" bind:value={search} aria-label="Search bookmarks" placeholder="Name, notes or frequency" />
      {#if search || folder}<button type="button" onclick={() => { search = ''; folder = ''; }}>Clear</button>{/if}
    </div>
    {#if folders.length}
      <label class="field"><span>Folder</span>
        <select bind:value={folder}>
          <option value="">All folders</option>
          {#each folders as f (f)}<option value={f}>{f}</option>{/each}
        </select>
      </label>
    {/if}
  </div>
  <div class="box">
    {#each shown as b (b.id)}
      {#if editing?.id === b.id}
        <form class="call" onsubmit={save}>
          <label class="field"><span>Name</span><input bind:value={editing.name} maxlength="120" /></label>
          <label class="field"><span>Frequency (MHz)</span><input class="mono" inputmode="decimal" bind:value={editing.mhz} required /></label>
          <label class="field"><span>Mode</span>
            <select bind:value={editing.mode}>{#each Object.entries(MODE_LABELS) as [id, label] (id)}<option value={id}>{label}</option>{/each}</select>
          </label>
          <label class="field"><span>Folder</span><input bind:value={editing.folder} maxlength="60" list="bookmark-folders" /></label>
          <label class="field"><span>Notes</span><input bind:value={editing.notes} maxlength="500" /></label>
          <div class="buttons"><button class="main">Save</button><button type="button" onclick={() => (editing = null)}>Cancel</button></div>
        </form>
      {:else}
        <div class="call keyword-row">
          <span>
            <button type="button" class="link-button" onclick={() => onpick(b)}>{b.name}</button>
            <span class="small mono">{mhz(b.frequency_hz)} MHz</span> <span class="small dim">{MODE_LABELS[b.mode]}{b.folder ? `, ${b.folder}` : ''}</span>
            {#if b.notes}<br /><span class="small dim">{b.notes}</span>{/if}
          </span>
          <span class="buttons">
            <button type="button" onclick={() => (editing = { id: b.id, name: b.name, mhz: mhz(b.frequency_hz), mode: b.mode, bandwidth_hz: b.bandwidth_hz, folder: b.folder, notes: b.notes })}>Edit</button>
            <button type="button" onclick={() => remove(b)}>Remove</button>
          </span>
        </div>
      {/if}
    {:else}
      <p class="pad dim small">{items.length ? 'None match.' : 'No bookmarks yet.'}</p>
    {/each}
  </div>
  <datalist id="bookmark-folders">{#each folders as f (f)}<option value={f}></option>{/each}</datalist>

  <form onsubmit={add}>
    <div class="field"><span>Bookmark your channel ({current.frequency_hz ? `${mhz(current.frequency_hz)} MHz, ${MODE_LABELS[current.mode]}` : 'not tuned yet'})</span>
      <input bind:value={name} maxlength="120" placeholder="Name" aria-label="Bookmark name" />
    </div>
    <label class="field"><span>Folder</span><input bind:value={newFolder} maxlength="60" list="bookmark-folders" placeholder="Optional" /></label>
    <div class="buttons form-buttons"><button class="main" disabled={!current.frequency_hz}>Add bookmark</button></div>
  </form>

  <details>
    <summary class="small tuner-summary">Import and export</summary>
    <label class="field"><span>Format</span>
      <select bind:value={importFormat}>{#each FORMATS as [id, label] (id)}<option value={id}>{label}</option>{/each}</select>
    </label>
    <label class="field"><span>File to import</span><input type="file" bind:this={fileInput} accept=".json,.csv,text/csv,application/json" /></label>
    <label class="tick small"><input type="checkbox" bind:checked={replace} /> Replace all my bookmarks with these</label>
    <div class="buttons form-buttons"><button type="button" onclick={importFile}>Import</button></div>
    <p class="small">Export:
      {#each FORMATS as [id, label], i (id)}{i ? ' · ' : ''}<a href={`api/me/bookmarks/export?format=${id}`} download>{label}</a>{/each}
    </p>
    <p class="small dim">GQRX and SDR++ keep less than SentrySDR does, so a trip through them can lose notes, or change a mode to the nearest one they have.</p>
  </details>
</fieldset>
