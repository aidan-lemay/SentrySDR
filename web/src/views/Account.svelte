<script>
  import { onMount } from 'svelte';
  import QRCode from 'qrcode';
  import { api, arrangeSources, formatTime, layoutOf } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { clearAfter } from '../notice.svelte.js';

  let { user, onChange, forced = false } = $props();
  let name = $state(user.name);
  let email = $state(user.email);
  let emailPassword = $state('');
  let current = $state('');
  let next = $state('');
  let totpPassword = $state('');
  let offCode = $state('');
  let setup = $state(null);
  let qr = $state('');
  let code = $state('');
  let activity = $state([]);
  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));

  const emailChanged = $derived(email.trim().toLowerCase() !== user.email);

  async function run(action, success) {
    error = '';
    message = '';
    scrollTo(0, 0);
    try {
      const reply = await action();
      if (success) message = success;
      return reply ?? true;
    } catch (failure) {
      error = failure.message;
      return null;
    }
  }

  async function saveProfile(event) {
    event.preventDefault();
    const done = await run(() => api('me', { method: 'PATCH', body: { name, email, current_password: emailPassword || null } }), 'Profile saved.');
    if (done) {
      emailPassword = '';
      await onChange();
    }
  }

  async function changePassword(event) {
    event.preventDefault();
    const done = await run(() => api('me/password', { method: 'POST', body: { current_password: current, new_password: next } }), 'Password changed. Your other devices were signed out.');
    if (done) {
      current = '';
      next = '';
      await onChange();
    }
  }

  async function startTotp(event) {
    event.preventDefault();
    const reply = await run(() => api('me/totp/setup', { method: 'POST', body: { password: totpPassword } }));
    if (reply) {
      setup = reply;
      qr = await QRCode.toDataURL(reply.uri, { margin: 2, width: 200 });
      totpPassword = '';
    }
  }

  async function finishTotp(event) {
    event.preventDefault();
    const done = await run(() => api('me/totp/enable', { method: 'POST', body: { code } }), 'Two-factor sign-in is on.');
    if (done) {
      setup = null;
      code = '';
      await onChange();
    }
  }

  async function disableTotp(event) {
    event.preventDefault();
    const done = await run(() => api('me/totp/disable', { method: 'POST', body: { password: totpPassword, code: offCode } }), 'Two-factor sign-in is off.');
    if (done) {
      totpPassword = '';
      offCode = '';
      await onChange();
    }
  }

  // --- how the Sources page is arranged: for this user only --------------------------------
  let arranged = $state(null);  // what arrangeSources() gives, once the section is opened
  let allSources = [];
  let groupName = $state('');
  let layoutNote = $state(null);  // { kind, text }
  let saveTimer;

  async function openLayout(event) {
    if (!event.currentTarget.open || arranged) return;
    try {
      const [sources, layout] = await Promise.all([api('sources'), api('me/source-layout')]);
      allSources = sources;
      arranged = arrangeSources(sources, layout);
    } catch (failure) {
      layoutNote = { kind: 'err', text: failure.message };
    }
  }

  // Every change saves itself, a moment later so a run of clicks is one save.
  function saveLayout() {
    clearTimeout(saveTimer);
    layoutNote = null;
    saveTimer = setTimeout(async () => {
      try {
        await api('me/source-layout', { method: 'PUT', body: layoutOf(arranged) });
        layoutNote = { kind: 'msg', text: 'Saved.' };
      } catch (failure) {
        layoutNote = { kind: 'err', text: failure.message };
      }
    }, 400);
  }

  function move(list, i, step) {
    [list[i], list[i + step]] = [list[i + step], list[i]];
    saveLayout();
  }

  // A source into a group (at its end), or out of one (to just after it).
  function setGroup(from, i, to) {
    const list = from === null ? arranged : arranged[from].sources;
    const source = from === null ? list[i].source : list[i];
    if (to === '') {
      list.splice(i, 1);
      arranged.splice(from + 1, 0, { source });
    } else {
      const group = arranged[Number(to)];
      list.splice(i, 1);
      group.sources.push(source);
    }
    saveLayout();
  }

  const groupTaken = (name, except = null) => arranged.some((e) => e !== except && e.group?.toLowerCase() === name.toLowerCase());

  function addGroup(event) {
    event.preventDefault();
    const name = groupName.trim();
    if (!name) return;
    if (groupTaken(name)) {
      layoutNote = { kind: 'err', text: `You already have a group called ${name}.` };
      return;
    }
    arranged.push({ group: name, collapsed: false, sources: [] });
    groupName = '';
    saveLayout();
  }

  function renameGroup(entry) {
    const name = prompt('Rename the group', entry.group)?.trim();
    if (!name || name === entry.group) return;
    if (groupTaken(name, entry)) {
      layoutNote = { kind: 'err', text: `You already have a group called ${name}.` };
      return;
    }
    entry.group = name;
    saveLayout();
  }

  // Its sources stay where the group was.
  function removeGroup(i) {
    arranged.splice(i, 1, ...arranged[i].sources.map((source) => ({ source })));
    saveLayout();
  }

  function resetLayout() {
    if (!confirm('Put the Sources page back in the usual order, with no groups?')) return;
    arranged = arrangeSources(allSources, { items: [] });
    saveLayout();
  }

  onMount(async () => {
    if (!forced) activity = (await api('me/audit').catch(() => [])) ?? [];
  });
</script>

<h1>{forced ? 'Choose a new password' : 'My account'}</h1>
{#if forced}<div class="warn">An administrator reset your password. Choose a new one to continue.</div>{/if}
{#if error}<div class="err" role="alert">{error}</div>{/if}
{#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}

{#if !forced}
  <form onsubmit={saveProfile}>
    <fieldset>
      <legend>Profile</legend>
      <label class="field"><span>Name</span><input bind:value={name} required maxlength="120" autocomplete="name" /></label>
      <label class="field"><span>Email</span><input type="email" bind:value={email} required autocomplete="email" /></label>
      {#if emailChanged}
        <label class="field"><span>Current password</span><input type="password" bind:value={emailPassword} required autocomplete="current-password" /></label>
      {/if}
      <div class="buttons form-buttons"><button class="main">Save</button></div>
    </fieldset>
  </form>
{/if}

<form onsubmit={changePassword}>
  <fieldset>
    <legend>Password</legend>
    <label class="field"><span>{forced ? 'Temporary password' : 'Current password'}</span><input type="password" bind:value={current} required autocomplete="current-password" /></label>
    <label class="field"><span>New password</span>
      <input type="password" bind:value={next} required minlength="12" autocomplete="new-password" />
      <span class="hint">12 characters or more.</span>
    </label>
    <div class="buttons form-buttons"><button class="main">Change password</button></div>
  </fieldset>
</form>

{#if !forced}
  {#if user.permissions.includes('listen_recordings') || user.permissions.includes('maps')}
    <p class="small">Keywords and email alerts are on the <a href="#/alerts?tab=settings">Alerts</a> page.</p>
  {/if}

  <fieldset>
    <legend>Two-factor sign-in</legend>
    {#if user.totp_enabled}
      <p><span class="st-ok">On.</span> You'll enter a code from your authenticator app each time you sign in.</p>
      <form onsubmit={disableTotp}>
        <label class="field"><span>Password</span><input type="password" bind:value={totpPassword} required autocomplete="current-password" /></label>
        <label class="field"><span>Code from your app</span><input bind:value={offCode} required inputmode="numeric" autocomplete="one-time-code" maxlength="6" /></label>
        <div class="buttons form-buttons"><button class="bad">Turn off</button></div>
      </form>
    {:else if setup}
      <p>Scan this in your authenticator app or type in the key, then enter the code it shows.</p>
      {#if qr}<img src={qr} alt="QR code for your authenticator app" width="200" height="200" />{/if}
      <div class="secret small">{setup.secret}</div>
      <form onsubmit={finishTotp}>
        <label class="field"><span>Code</span><input bind:value={code} required inputmode="numeric" maxlength="6" autocomplete="one-time-code" /></label>
        <div class="buttons form-buttons"><button class="main">Turn on</button></div>
      </form>
    {:else}
      <p>Off. Turning it on adds a code from an authenticator app to your sign-in.</p>
      <form onsubmit={startTotp}>
        <label class="field"><span>Password</span><input type="password" bind:value={totpPassword} required autocomplete="current-password" /></label>
        <div class="buttons form-buttons"><button>Set up</button></div>
      </form>
    {/if}
  </fieldset>
{/if}

{#if !forced}
  <fieldset>
    <legend>Sources page</legend>
    <details ontoggle={openLayout}>
      <summary class="small" style="padding:.3rem 0">Order and groups</summary>
      <p class="small dim">Only you see this arrangement.</p>
      {#if layoutNote?.kind === 'err'}<div class="err" role="alert">{layoutNote.text}</div>{/if}
      {#if arranged && !arranged.length}<p class="small dim">No sources yet.</p>{/if}
      {#if arranged?.length}
        <ol class="arrange">
          {#each arranged as entry, i (entry.source ? `s${entry.source.id}` : `g${entry.group}`)}
            {#if entry.source}
              <li>{@render line(entry.source, arranged, i, null)}</li>
            {:else}
              <li class="group">
                <div class="line">
                  <strong>{entry.group}</strong>
                  <span class="buttons">
                    <button aria-label={`Move ${entry.group} up`} disabled={i === 0} onclick={() => move(arranged, i, -1)}>↑</button>
                    <button aria-label={`Move ${entry.group} down`} disabled={i === arranged.length - 1} onclick={() => move(arranged, i, 1)}>↓</button>
                    <button onclick={() => renameGroup(entry)}>Rename</button>
                    <button onclick={() => removeGroup(i)}>Remove</button>
                  </span>
                </div>
                <ol class="arrange inner">
                  {#each entry.sources as source, j (source.id)}
                    <li>{@render line(source, entry.sources, j, i)}</li>
                  {:else}
                    <li class="small dim">Empty. Choose this group for a source to put it here.</li>
                  {/each}
                </ol>
              </li>
            {/if}
          {/each}
        </ol>
        <form onsubmit={addGroup}>
          <label class="field"><span>New group</span><input bind:value={groupName} maxlength="60" /></label>
          <div class="buttons form-buttons">
            <button>Add group</button>
            <button type="button" onclick={resetLayout}>Back to the usual order</button>
            {#if layoutNote?.kind === 'msg'}<span class="small st-ok" role="status">{layoutNote.text}</span>{/if}
          </div>
        </form>
      {/if}
    </details>
  </fieldset>
{/if}

{#snippet line(source, list, i, group)}
  <div class="line">
    <span>{source.name}</span>
    <span class="buttons">
      <button aria-label={`Move ${source.name} up`} disabled={i === 0} onclick={() => move(list, i, -1)}>↑</button>
      <button aria-label={`Move ${source.name} down`} disabled={i === list.length - 1} onclick={() => move(list, i, 1)}>↓</button>
      <select aria-label={`Group for ${source.name}`} value={group === null ? '' : String(group)} onchange={(event) => setGroup(group, i, event.target.value)}>
        <option value="">No group</option>
        {#each arranged as entry, k (entry.source ? `s${entry.source.id}` : `g${entry.group}`)}
          {#if !entry.source}<option value={String(k)}>{entry.group}</option>{/if}
        {/each}
      </select>
    </span>
  </div>
{/snippet}

{#if !forced && activity.length}
  <div class="box">
    <h2>Recent activity</h2>
    <table class="table">
      <thead><tr><th>When</th><th>Action</th><th>From</th></tr></thead>
      <tbody>
        {#each activity.slice(0, 25) as row}
          <tr>
            <td data-label="When"><span class="small">{formatTime(row.ts)}</span></td>
            <td data-label="Action"><span class="mono small">{row.action}</span></td>
            <td data-label="From"><span class="mono small">{row.ip}</span></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .arrange { list-style: none; padding: 0; margin: .4rem 0; }
  .arrange li { border-bottom: 1px solid var(--rule); padding: .15rem 0; }
  .arrange li:last-child { border-bottom: 0; }
  .arrange.inner { margin: 0 0 .2rem .4rem; padding-left: .6rem; border-left: 3px solid var(--rule); }
  .line { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0 .6rem; }
  .line .buttons { margin: .15rem 0; }
  .line select { width: auto; max-width: 12rem; }
</style>
