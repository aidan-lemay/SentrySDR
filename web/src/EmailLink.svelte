<script>
  // Opened from a link in an email, signed in or not. Confirming happens on its own;
  // turning emails off waits for a press, because mail scanners open every link in an email.
  import { onMount } from 'svelte';
  import { api } from './lib.js';

  let { route } = $props();
  const action = route.split('?')[0];
  const params = new URLSearchParams(route.split('?')[1] ?? '');
  const body = { u: Number(params.get('u')), t: params.get('t') ?? '' };

  let result = $state('');
  let error = $state('');
  let busy = $state(false);

  async function send(path) {
    busy = true;
    try {
      result = (await api(path, { method: 'POST', body })).detail;
    } catch (failure) {
      error = failure.message;
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    if (action === 'email-confirm') send('email/confirm');
  });
</script>

<header class="site"><div class="site-top"><span class="site-name">SentrySDR</span></div></header>
<main>
  <h1>{action === 'email-confirm' ? 'Confirm alert emails' : 'Turn off alert emails'}</h1>
  {#if error}<div class="err" role="alert">{error}</div>{/if}
  {#if result}
    <div class="msg" role="status">{result}</div>
  {:else if action === 'email-off' && !error}
    <p>Stop SentrySDR sending you alert emails? Your keywords and watches stay, and you can turn emails back on from the Alerts page.</p>
    <div class="buttons"><button class="main" disabled={busy} onclick={() => send('email/off')}>Turn off alert emails</button></div>
  {:else if busy}
    <p>Confirming...</p>
  {/if}
  <p class="small"><a href="#/alerts?tab=settings">Go to SentrySDR</a></p>
</main>
