<script>
  import { onMount } from 'svelte';
  import QRCode from 'qrcode';
  import { api, formatTime } from '../lib.js';

  let { user, onChange, forced = false } = $props();
  let name = $state(user.name);
  let email = $state(user.email);
  let emailPassword = $state('');
  let current = $state('');
  let next = $state('');
  let totpPassword = $state('');
  let setup = $state(null);
  let qr = $state('');
  let code = $state('');
  let activity = $state([]);
  let error = $state('');
  let message = $state('');

  const emailChanged = $derived(email.trim().toLowerCase() !== user.email);

  async function run(action, success) {
    error = '';
    message = '';
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
    const done = await run(() => api('me/password', { method: 'POST', body: { current_password: current, new_password: next } }), 'Password changed. Other devices were signed out.');
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
    const done = await run(() => api('me/totp/enable', { method: 'POST', body: { code } }), 'Two-factor authentication is on.');
    if (done) {
      setup = null;
      code = '';
      await onChange();
    }
  }

  async function disableTotp(event) {
    event.preventDefault();
    const done = await run(() => api('me/totp/disable', { method: 'POST', body: { password: totpPassword } }), 'Two-factor authentication is off.');
    if (done) {
      totpPassword = '';
      await onChange();
    }
  }

  onMount(async () => {
    if (!forced) activity = (await api('me/audit').catch(() => [])) ?? [];
  });
</script>

<div class="page-head"><h1>{forced ? 'Choose a new password' : 'Your account'}</h1></div>
{#if forced}<div class="notice warn">An administrator reset your password. Choose a new one to continue.</div>{/if}
{#if error}<div class="notice error" role="alert">{error}</div>{/if}
{#if message}<div class="notice" role="status">{message}</div>{/if}

<div class="grid">
  {#if !forced}
    <form class="panel stack" onsubmit={saveProfile}>
      <h2>Profile</h2>
      <label>Name <input bind:value={name} required maxlength="120" autocomplete="name" /></label>
      <label>Email <input type="email" bind:value={email} required autocomplete="email" /></label>
      {#if emailChanged}
        <label>Current password (to change email) <input type="password" bind:value={emailPassword} required autocomplete="current-password" /></label>
      {/if}
      <div><button class="primary">Save profile</button></div>
    </form>
  {/if}

  <form class="panel stack" onsubmit={changePassword}>
    <h2>Password</h2>
    <label>{forced ? 'Temporary password' : 'Current password'} <input type="password" bind:value={current} required autocomplete="current-password" /></label>
    <label>New password <input type="password" bind:value={next} required minlength="12" autocomplete="new-password" />
      <span class="small">At least 12 characters.</span>
    </label>
    <div><button class="primary">Change password</button></div>
  </form>

  {#if !forced}
    <section class="panel stack">
      <h2>Two-factor authentication</h2>
      {#if user.totp_enabled}
        <p class="small" style="margin:0"><span class="pill ok">On</span> You'll enter a code from your authenticator app at each sign-in.</p>
        <form class="stack" onsubmit={disableTotp}>
          <label>Password <input type="password" bind:value={totpPassword} required autocomplete="current-password" /></label>
          <div><button class="danger">Turn off</button></div>
        </form>
      {:else if setup}
        <p class="small" style="margin:0">Scan this with your authenticator app, or enter the key by hand, then type the code it shows.</p>
        {#if qr}<img src={qr} alt="QR code for your authenticator app" width="200" height="200" />{/if}
        <div class="secret small">{setup.secret}</div>
        <form class="stack" onsubmit={finishTotp}>
          <label>Code <input bind:value={code} required inputmode="numeric" maxlength="6" autocomplete="one-time-code" /></label>
          <div><button class="primary">Turn on</button></div>
        </form>
      {:else}
        <p class="small muted" style="margin:0">Adds a code from an authenticator app to your sign-in.</p>
        <form class="stack" onsubmit={startTotp}>
          <label>Password <input type="password" bind:value={totpPassword} required autocomplete="current-password" /></label>
          <div><button>Set up</button></div>
        </form>
      {/if}
    </section>
  {/if}
</div>

{#if !forced && activity.length}
  <section class="panel" style="margin-top:1rem">
    <h2 style="margin-bottom:.5rem">Recent activity</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>When</th><th>Action</th><th>From</th></tr></thead>
        <tbody>
          {#each activity as row}
            <tr><td>{formatTime(row.ts)}</td><td class="mono small">{row.action}</td><td class="mono small">{row.ip}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
{/if}
