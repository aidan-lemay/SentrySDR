<script>
  import { api } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { clearAfter } from '../notice.svelte.js';

  let { onSignedIn } = $props();
  let mode = $state('login');
  let email = $state('');
  let name = $state('');
  let password = $state('');
  let totp = $state('');
  let needTotp = $state(false);
  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));
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

<div class="signin">
  <h1>SentrySDR</h1>
  {#if error}<div class="err" role="alert">{error}</div>{/if}
  {#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}
  <form onsubmit={submit}>
    <fieldset>
      <legend>{mode === 'login' ? 'Sign in' : 'Request an account'}</legend>
      {#if mode === 'signup'}
        <p class="small">An administrator has to approve your request before you can sign in.</p>
        <label class="stacked"><span>Name</span><input bind:value={name} required maxlength="120" autocomplete="name" /></label>
      {/if}
      <label class="stacked"><span>Email</span><input type="email" bind:value={email} required autocomplete="username" /></label>
      <label class="stacked"><span>Password{mode === 'signup' ? ' (12 characters or more)' : ''}</span>
        <input type="password" bind:value={password} required minlength={mode === 'signup' ? 12 : undefined}
          autocomplete={mode === 'login' ? 'current-password' : 'new-password'} />
      </label>
      {#if needTotp}
        <label class="stacked"><span>Code from your authenticator app</span>
          <input bind:value={totp} inputmode="numeric" autocomplete="one-time-code" maxlength="6" required />
        </label>
      {/if}
      <div class="buttons"><button class="main" disabled={busy}>{mode === 'login' ? 'Sign in' : 'Send request'}</button></div>
    </fieldset>
  </form>
  {#if mode === 'login'}
    <p><a href="#/" onclick={(event) => { event.preventDefault(); switchMode('signup'); }}>Request an account</a></p>
  {:else}
    <p><a href="#/" onclick={(event) => { event.preventDefault(); switchMode('login'); }}>Back to sign in</a></p>
  {/if}
</div>

<style>
  .stacked { display: block; margin: .5rem 0; }
  .stacked > span { display: block; font-size: .875rem; margin-bottom: .15rem; }
</style>
