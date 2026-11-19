<script>
  // Your tuner bookmarks, as GQRX keeps them: each can be in several folders, each folder has a
  // color, and you choose which folders to see. They're kept with your account on the main server,
  // so every SDR, on any machine, shows the same ones. Bookmarks in the folders you see also show
  // on the spectrum, through onvisible.
  import { onMount } from 'svelte';
  import { api } from './lib.js';
  import Notice from './Notice.svelte';
  import { clearAfter } from './notice.svelte.js';

  let { current, onpick, onvisible = () => {} } = $props();
  const MODE_LABELS = { nfm: 'NFM', am: 'AM', wfm: 'WFM', wfm_stereo: 'WFM stereo', usb: 'USB', lsb: 'LSB', cw: 'CW' };
  const FORMATS = [['sentrysdr', 'SentrySDR (keeps everything)'], ['gqrx', 'GQRX bookmarks.csv'], ['sdrpp', 'SDR++ frequency manager']];
  const NONE = '';  // stands for "in no folder" in the folder list
  const NONE_COLOR = '#8c8a83';
  const SHOWN_KEY = 'sentrysdr-bookmark-folders';

  let items = $state([]);
  let folders = $state([]);
  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));
  let search = $state('');
  let hidden = $state(new Set(loadHidden()));  // folders you've unticked; anything new shows
  let editing = $state(null);
  let menu = $state(null);  // { x, y, kind: 'bookmark' | 'folder', item }
  let renaming = $state(null);  // { from, to }
  let adding = $state(blankAdd());
  let addOpen = $state(false);
  let importFormat = $state('sentrysdr');
  let replace = $state(false);
  let fileInput = $state();

  function loadHidden() {
    try { return JSON.parse(localStorage.getItem(SHOWN_KEY) || '[]'); } catch { return []; }
  }
  function blankAdd() {
    return { name: '', mhz: '', mode: 'nfm', folders: [], newFolder: '' };
  }

  const colorOf = $derived(Object.fromEntries(folders.map((f) => [f.name, f.color])));
  const visibleIn = (b) => (b.folders.length ? b.folders.some((f) => !hidden.has(f)) : !hidden.has(NONE));
  const colorFor = (b) => colorOf[b.folders.find((f) => !hidden.has(f)) ?? b.folders[0]] ?? NONE_COLOR;
  const matches = (b) => !search || `${b.name} ${b.notes} ${b.folders.join(' ')} ${(b.frequency_hz / 1e6).toFixed(4)}`.toLowerCase().includes(search.toLowerCase());
  const shown = $derived(items.filter((b) => visibleIn(b) && matches(b)));
  const unfiled = $derived(items.filter((b) => !b.folders.length).length);
  const mhz = (hz) => String(Number((hz / 1e6).toFixed(6)));  // 146.52, not 146.5200

  $effect(() => {
    onvisible(items.filter(visibleIn).map((b) => ({ ...b, color: colorFor(b) })));
  });

  function setHidden(next) {
    hidden = new Set(next);
    try { localStorage.setItem(SHOWN_KEY, JSON.stringify([...hidden])); } catch { /* not kept */ }
  }
  const toggle = (name, on) => setHidden(on ? [...hidden].filter((n) => n !== name) : [...hidden, name]);

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

  async function load() {
    [items, folders] = await Promise.all([api('me/bookmarks'), api('me/bookmark-folders')]);
  }

  // --- adding -------------------------------------------------------------------------------
  function openAdd(event) {
    addOpen = event.currentTarget.open;
    if (addOpen && current.frequency_hz) {
      adding = { ...blankAdd(), mhz: mhz(current.frequency_hz), mode: current.mode, folders: [] };
    }
  }

  async function add(event) {
    event.preventDefault();
    const chosen = [...adding.folders, ...(adding.newFolder.trim() ? [adding.newFolder.trim()] : [])];
    const body = {
      name: adding.name, mode: adding.mode, folders: chosen,
      frequency_hz: Math.round(Number(String(adding.mhz).replace(',', '.')) * 1e6),
      bandwidth_hz: adding.mode === current.mode ? current.bandwidth_hz : null,
    };
    if (await run(() => api('me/bookmarks', { method: 'POST', body }), 'Bookmark added.')) {
      adding = { ...blankAdd(), mhz: adding.mhz, mode: adding.mode, folders: adding.folders };
      await load();
    }
  }

  // --- the context menu -------------------------------------------------------------------------
  function openMenu(event, kind, item) {
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.type === 'contextmenu' ? event.clientX : rect.right;
    const y = event.type === 'contextmenu' ? event.clientY : rect.bottom;
    menu = { x: Math.min(x, innerWidth - 190), y: Math.min(y, innerHeight - 160), kind, item };
  }

  function closeMenu() { menu = null; }

  function onWindowKey(event) {
    if (event.key === 'Escape') closeMenu();
  }

  function startEdit(b) {
    editing = { id: b.id, name: b.name, mhz: mhz(b.frequency_hz), mode: b.mode, bandwidth_hz: b.bandwidth_hz, folders: [...b.folders], notes: b.notes, newFolder: '' };
    closeMenu();
  }

  async function save(event) {
    event.preventDefault();
    const body = {
      name: editing.name, mode: editing.mode, bandwidth_hz: editing.bandwidth_hz, notes: editing.notes,
      folders: [...editing.folders, ...(editing.newFolder.trim() ? [editing.newFolder.trim()] : [])],
      frequency_hz: Math.round(Number(String(editing.mhz).replace(',', '.')) * 1e6),
    };
    if (await run(() => api(`me/bookmarks/${editing.id}`, { method: 'PUT', body }), 'Saved.')) {
      editing = null;
      await load();
    }
  }

  async function remove(b) {
    closeMenu();
    if (await run(() => api(`me/bookmarks/${b.id}`, { method: 'DELETE' }), `Removed ${b.name}.`)) await load();
  }

  // --- folders -----------------------------------------------------------------------------------
  async function recolor(folder, color) {
    if (await run(() => api(`me/bookmark-folders/${encodeURIComponent(folder.name)}`, { method: 'PUT', body: { color } }))) await load();
  }

  async function rename(event) {
    event.preventDefault();
    const { from, to } = renaming;
    if (to.trim() && to.trim() !== from
      && await run(() => api(`me/bookmark-folders/${encodeURIComponent(from)}`, { method: 'PUT', body: { name: to.trim() } }), 'Renamed.')) {
      if (hidden.has(from)) setHidden([...[...hidden].filter((n) => n !== from), to.trim()]);
      await load();
    }
    renaming = null;
  }

  async function removeFolder(folder) {
    closeMenu();
    if (await run(() => api(`me/bookmark-folders/${encodeURIComponent(folder.name)}`, { method: 'DELETE' }),
      `Removed the folder ${folder.name}. Its bookmarks are still here.`)) await load();
  }

  // --- import -------------------------------------------------------------------------------------
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

<svelte:window onpointerdown={(event) => menu && !event.target.closest('.context-menu') && closeMenu()} onkeydown={onWindowKey} />

<div class="bookmarks">
  {#if error}<div class="err" role="alert">{error}</div>{/if}
  {#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}

  <div class="bm-heading small"><span>Folders to show</span>
    <button class="link-button" onclick={() => setHidden([])}>All</button>
    <button class="link-button" onclick={() => setHidden([...folders.map((f) => f.name), NONE])}>None</button>
  </div>
  <ul class="bm-folders">
    {#each folders as folder (folder.name)}
      <li oncontextmenu={(event) => openMenu(event, 'folder', folder)}>
        {#if renaming?.from === folder.name}
          <form class="bm-rename" onsubmit={rename}>
            <input bind:value={renaming.to} maxlength="60" aria-label={`New name for ${folder.name}`} />
            <button class="main">Rename</button>
            <button type="button" onclick={() => (renaming = null)}>Cancel</button>
          </form>
        {:else}
          <label><input type="checkbox" checked={!hidden.has(folder.name)} onchange={(event) => toggle(folder.name, event.target.checked)} />
            <input type="color" class="bm-swatch" value={folder.color} onchange={(event) => recolor(folder, event.target.value)} aria-label={`Color of ${folder.name}`} />
            <span>{folder.name}</span> <span class="dim">{folder.count}</span></label>
          <button class="bm-more" aria-label={`More for ${folder.name}`} onclick={(event) => openMenu(event, 'folder', folder)}>…</button>
        {/if}
      </li>
    {/each}
    {#if unfiled || !folders.length}
      <li><label><input type="checkbox" checked={!hidden.has(NONE)} onchange={(event) => toggle(NONE, event.target.checked)} />
        <span class="bm-swatch bm-swatch-none" style:background={NONE_COLOR}></span><span>In no folder</span> <span class="dim">{unfiled}</span></label></li>
    {/if}
  </ul>

  <div class="search">
    <div class="pair">
      <input type="search" bind:value={search} aria-label="Search bookmarks" placeholder="Search bookmarks" />
      {#if search}<button type="button" onclick={() => (search = '')}>Clear</button>{/if}
    </div>
  </div>

  <ul class="bm-list">
    {#each shown as b (b.id)}
      <li oncontextmenu={(event) => openMenu(event, 'bookmark', b)}>
        {#if editing?.id === b.id}
          <form class="bm-edit" onsubmit={save}>
            <label>Name <input bind:value={editing.name} maxlength="120" /></label>
            <label>MHz <input class="mono" inputmode="decimal" bind:value={editing.mhz} required /></label>
            <label>Mode <select bind:value={editing.mode}>{#each Object.entries(MODE_LABELS) as [id, label] (id)}<option value={id}>{label}</option>{/each}</select></label>
            <fieldset><legend>Folders</legend>
              {#each folders as f (f.name)}<label class="small"><input type="checkbox" value={f.name} bind:group={editing.folders} /> {f.name}</label>{/each}
              <input bind:value={editing.newFolder} maxlength="60" placeholder="New folder" aria-label="New folder" />
            </fieldset>
            <label>Notes <input bind:value={editing.notes} maxlength="500" /></label>
            <div class="buttons"><button class="main">Save</button><button type="button" onclick={() => (editing = null)}>Cancel</button></div>
          </form>
        {:else}
          <span class="bm-color" style:background={colorFor(b)} aria-hidden="true"></span>
          <button class="bm-name" onclick={() => onpick(b)} title={b.notes || `Tune to ${b.name}`}>
            <span>{b.name}</span>
            <span class="small mono">{mhz(b.frequency_hz)}</span>
            <span class="small dim">{MODE_LABELS[b.mode]}</span>
          </button>
          <button class="bm-more" aria-label={`More for ${b.name}`} onclick={(event) => openMenu(event, 'bookmark', b)}>…</button>
        {/if}
      </li>
    {:else}
      <li class="small dim bm-empty">{items.length ? 'None in the folders shown, or matching the search.' : 'No bookmarks yet.'}</li>
    {/each}
  </ul>

  <details class="bm-section" ontoggle={openAdd}>
    <summary class="small">Add a bookmark</summary>
    <form class="bm-edit" onsubmit={add}>
      <label>Name <input bind:value={adding.name} maxlength="120" placeholder="Optional" /></label>
      <label>MHz <input class="mono" inputmode="decimal" bind:value={adding.mhz} required /></label>
      <label>Mode <select bind:value={adding.mode}>{#each Object.entries(MODE_LABELS) as [id, label] (id)}<option value={id}>{label}</option>{/each}</select></label>
      <fieldset><legend>Folders</legend>
        {#each folders as f (f.name)}<label class="small"><input type="checkbox" value={f.name} bind:group={adding.folders} /> {f.name}</label>{/each}
        <input bind:value={adding.newFolder} maxlength="60" placeholder="New folder" aria-label="New folder" />
      </fieldset>
      <div class="buttons"><button class="main">Add bookmark</button>
        {#if current.frequency_hz}<button type="button" onclick={() => { adding.mhz = mhz(current.frequency_hz); adding.mode = current.mode; }}>Use my channel</button>{/if}
      </div>
    </form>
  </details>

  <details class="bm-section">
    <summary class="small">Import and export</summary>
    <label class="field"><span>Format</span>
      <select bind:value={importFormat}>{#each FORMATS as [id, label] (id)}<option value={id}>{label}</option>{/each}</select>
    </label>
    <label class="field"><span>File to import</span><input type="file" bind:this={fileInput} accept=".json,.csv,text/csv,application/json" /></label>
    <label class="tick small"><input type="checkbox" bind:checked={replace} /> Replace all my bookmarks with these</label>
    <div class="buttons"><button type="button" onclick={importFile}>Import</button></div>
    <p class="small">Export:
      {#each FORMATS as [id, label], i (id)}{i ? ' · ' : ''}<a href={`api/me/bookmarks/export?format=${id}`} download>{label}</a>{/each}
    </p>
    <p class="small dim">GQRX and SDR++ keep less than SentrySDR does, so a trip through them can lose notes, or change a mode to the nearest one they have. Folder colors go both ways with GQRX.</p>
  </details>
</div>

{#if menu}
  <div class="context-menu" role="menu" style:left={`${menu.x}px`} style:top={`${menu.y}px`}>
    {#if menu.kind === 'bookmark'}
      <button role="menuitem" onclick={() => { onpick(menu.item); closeMenu(); }}>Tune to it</button>
      <button role="menuitem" onclick={() => startEdit(menu.item)}>Edit</button>
      <button role="menuitem" onclick={() => remove(menu.item)}>Remove</button>
    {:else}
      <button role="menuitem" onclick={() => { renaming = { from: menu.item.name, to: menu.item.name }; closeMenu(); }}>Rename</button>
      <button role="menuitem" onclick={() => { setHidden([...folders.map((f) => f.name), NONE].filter((n) => n !== menu.item.name)); closeMenu(); }}>Show only this</button>
      <button role="menuitem" onclick={() => removeFolder(menu.item)}>Remove folder</button>
    {/if}
  </div>
{/if}
