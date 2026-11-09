<script>
  import { onMount } from 'svelte';
  import QRCode from 'qrcode';
  import { api, formatTime } from '../lib.js';
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

  // --- keywords
  let keywordList = $state([]);
  let newWord = $state('');
  let newApplies = $state('both');

  async function loadKeywords() {
    keywordList = await api('me/keywords').catch(() => []);
  }

  async function addKeyword(event) {
    event.preventDefault();
    if (await run(() => api('me/keywords', { method: 'POST', body: { word: newWord, applies: newApplies } }), 'Keyword added.')) {
      newWord = '';
      await loadKeywords();
    }
  }

  async function removeKeyword(keyword) {
    if (await run(() => api(`me/keywords/${keyword.id}`, { method: 'DELETE' }), 'Keyword removed.')) await loadKeywords();
  }

  onMount(async () => {
    if (!forced) loadKeywords();
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
  <form onsubmit={addKeyword}>
    <fieldset>
      <legend>Keywords</legend>
      <p class="small">Calls whose transcript, or ACARS messages whose text, contains one of these are flagged as important for you, and appear under <a href="#/alerts">Alerts</a>. Whole words or phrases, ignoring case: "fire" matches "Fire!" but not "firefighter". Only you see your keywords.</p>
      {#each keywordList as keyword (keyword.id)}
        <div class="buttons" style="margin:.2rem 0">
          <span class="mono">{keyword.word}</span>
          <span class="small dim">{({ both: 'calls and messages', calls: 'calls', messages: 'messages' })[keyword.applies]}</span>
          <button type="button" onclick={() => removeKeyword(keyword)}>Remove</button>
        </div>
      {:else}
        <p class="small dim">No keywords yet.</p>
      {/each}
      <label class="field"><span>New keyword</span><input bind:value={newWord} minlength="2" maxlength="80" placeholder="structure fire" required /></label>
      <label class="field"><span>Look in</span>
        <select bind:value={newApplies}>
          <option value="both">Calls and ACARS messages</option>
          <option value="calls">Call transcripts only</option>
          <option value="messages">ACARS messages only</option>
        </select>
      </label>
      <div class="buttons form-buttons"><button class="main">Add keyword</button></div>
    </fieldset>
  </form>

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
