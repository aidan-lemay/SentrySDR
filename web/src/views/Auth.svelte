<script>
  import { api } from '../lib.js';

  let { onSignedIn } = $props();
  let mode = $state('login');
  let email = $state('');
  let name = $state('');
  let password = $state('');
  let totp = $state('');
  let needTotp = $state(false);
  let error = $state('');
  let message = $state('');
  let busy = $state(false);

  async function submit(event) {
    event.preventDefault();
    error = '';
    message = '';
    busy = true;
    try {
      if (mode === 'login') {
        await api('auth/login', { method: 'POST', body: { email, password, totp: totp || null } });
        password = '';
        await onSignedIn();
      } else {
        const reply = await api('auth/signup', { method: 'POST', body: { email, name, password } });
        message = reply.detail;
        password = '';
        mode = 'login';
      }
    } catch (failure) {
      if (failure.detail === 'totp_required') {
        needTotp = true;
      } else {
        error = failure.message;
      }
    } finally {
      busy = false;
    }
  }

  function switchMode(next) {
    mode = next;
    error = '';
    needTotp = false;
  }
</script>

<div class="auth">
  <h1 style="margin-bottom:.25rem">SentrySDR</h1>
  <p class="muted small">{mode === 'login' ? 'Sign in to continue.' : 'Request an account. An administrator must approve it before you can sign in.'}</p>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}
  {#if message}<div class="notice" role="status">{message}</div>{/if}
  <form class="panel stack" onsubmit={submit}>
    {#if mode === 'signup'}
      <label>Name <input bind:value={name} required maxlength="120" autocomplete="name" /></label>
    {/if}
    <label>Email <input type="email" bind:value={email} required autocomplete="username" /></label>
    <label>Password
      <input type="password" bind:value={password} required minlength={mode === 'signup' ? 12 : undefined}
        autocomplete={mode === 'login' ? 'current-password' : 'new-password'} />
      {#if mode === 'signup'}<span class="small">At least 12 characters.</span>{/if}
    </label>
    {#if needTotp}
      <label>Authentication code
        <!-- svelte-ignore a11y_autofocus -->
        <input bind:value={totp} inputmode="numeric" autocomplete="one-time-code" maxlength="6" required autofocus />
      </label>
    {/if}
    <button class="primary" disabled={busy}>{mode === 'login' ? 'Sign in' : 'Request account'}</button>
  </form>
  {#if mode === 'login'}
    <button class="tab" onclick={() => switchMode('signup')}>Request an account</button>
  {:else}
    <button class="tab" onclick={() => switchMode('login')}>Back to sign in</button>
  {/if}
</div>
