<script>
  // One user's keywords, email alerts, and the channels and aircraft they want emailed about.
  import { onMount } from 'svelte';
  import { api } from '../lib.js';
  import Notice from '../Notice.svelte';
  import { clearAfter } from '../notice.svelte.js';

  let error = $state('');
  let message = $state('');
  clearAfter(() => message, () => (message = ''));

  let email = $state(null);
  let keywordList = $state([]);
  let watches = $state([]);
  let targets = $state([]);
  let newWord = $state('');
  let newApplies = $state('both');
  let newEmail = $state(true);
  let newChannel = $state('');
  let newAircraft = $state('');
  let toneOuts = $state([]);
  let newToneOut = $state('');
  let saving = $state(false);
  let emergency = $state(null);
  const GROUPS = [['adsb', 'Aircraft (ADS-B)'], ['ais', 'Ships (AIS)']];

  const APPLIES = { both: 'calls and messages', calls: 'calls', messages: 'messages' };
  const channels = $derived(watches.filter((w) => w.kind === 'channel'));
  const aircraft = $derived(watches.filter((w) => w.kind === 'aircraft'));
  const unwatched = $derived(targets.filter((t) => !channels.some((w) => w.target_id === t.id)));
  const pages = $derived(watches.filter((w) => w.kind === 'toneout'));
  const unfollowed = $derived(toneOuts.filter((t) => !pages.some((w) => w.value === String(t.id))));

  async function run(action, success) {
    error = '';
    message = '';
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
    [email, keywordList, watches, targets] = await Promise.all([
      api('me/email-alerts'), api('me/keywords'), api('me/watches'), api('targets').catch(() => []),
    ]);
    if (email.messages) emergency = await api('me/emergency-alerts');
    if (email.calls) toneOuts = await api('tone-outs').catch(() => []);
  }

  // --- email
  async function saveEmail(changes) {
    saving = true;
    const reply = await run(() => api('me/email-alerts', {
      method: 'PUT', body: { enabled: email.enabled, window: email.window, important: email.important, ...changes },
    }), (r) => (r.confirmation_sent ? `A confirmation link is on its way to ${r.address}. Open it to start getting emails.` : 'Saved.'));
    if (reply) email = reply;
    else email = await api('me/email-alerts').catch(() => email);
    saving = false;
  }

  async function resend() {
    const reply = await run(() => api('me/email-alerts/resend', { method: 'POST' }), (r) => `Sent again to ${r.address}.`);
    if (reply) email = reply;
  }

  // --- emergencies
  async function saveEmergency(changes) {
    const body = {
      codes: emergency.codes.filter((c) => c.chosen).map((c) => c.code), km: emergency.km, email: emergency.email, ...changes,
    };
    const reply = await run(() => api('me/emergency-alerts', { method: 'PUT', body }), 'Saved.');
    emergency = reply || await api('me/emergency-alerts').catch(() => emergency);
  }

  function toggleCode(code, on) {
    const codes = emergency.codes.filter((c) => (c.code === code ? on : c.chosen)).map((c) => c.code);
    saveEmergency({ codes });
  }

  // --- keywords
  async function addKeyword(event) {
    event.preventDefault();
    if (await run(() => api('me/keywords', { method: 'POST', body: { word: newWord, applies: newApplies, email: newEmail } }), 'Keyword added.')) {
      newWord = '';
      keywordList = await api('me/keywords');
    }
  }

  async function setKeywordEmail(keyword, value) {
    const reply = await run(() => api(`me/keywords/${keyword.id}`, { method: 'PATCH', body: { email: value } }));
    keywordList = await api('me/keywords').catch(() => keywordList);
    return reply;
  }

  async function removeKeyword(keyword) {
    if (await run(() => api(`me/keywords/${keyword.id}`, { method: 'DELETE' }), 'Keyword removed.')) keywordList = await api('me/keywords');
  }

  // --- watches
  async function addWatch(body, done) {
    if (await run(() => api('me/watches', { method: 'POST', body }), 'Added.')) {
      done();
      watches = await api('me/watches');
    }
  }

  async function removeWatch(watch) {
    if (await run(() => api(`me/watches/${watch.id}`, { method: 'DELETE' }), 'Removed.')) watches = await api('me/watches');
  }

  onMount(() => { load().catch((failure) => (error = failure.message)); });
</script>

{#if error}<div class="err" role="alert">{error}</div>{/if}
{#if message}<Notice ondismiss={() => (message = '')}>{message}</Notice>{/if}

{#if email}
  <fieldset>
    <legend>Email alerts</legend>
    {#if !email.server_enabled}
      <p class="small">Email alerts aren't set up on this server.</p>
    {:else}
      <label class="tick"><input type="checkbox" checked={email.enabled} disabled={saving} onchange={(event) => saveEmail({ enabled: event.target.checked })} /> Email me alerts</label>
      <p class="small">
        To <span class="mono">{email.address}</span>,
        {#if email.confirmed}<span class="st-ok">confirmed</span>.
        {:else if email.waiting}<span class="st-warn">waiting for you to open the confirmation link</span>.
        {:else}<span class="st-warn">not confirmed yet</span>.{/if}
        Change it under <a href="#/account">My account</a>.
      </p>
      {#if email.enabled && !email.confirmed}
        <div class="buttons"><button type="button" onclick={resend}>Send the confirmation link again</button></div>
      {/if}
      {#if email.enabled}
        <label class="field"><span>How often</span>
          <select value={email.window} onchange={(event) => saveEmail({ window: Number(event.target.value) })}>
            {#each email.windows as minutes (minutes)}<option value={minutes}>At most one email every {minutes === 60 ? 'hour' : `${minutes} minutes`}</option>{/each}
          </select>
          <span class="hint">Later alerts are grouped into the next email.</span>
        </label>
        <label class="tick"><input type="checkbox" checked={email.important} onchange={(event) => saveEmail({ important: event.target.checked })} /> Also when someone marks a call or message important</label>
      {/if}
    {/if}
  </fieldset>
{/if}

<form onsubmit={addKeyword}>
  <fieldset>
    <legend>Keywords</legend>
    <p class="small">Whole words, any case, in call transcripts and ACARS text. Only you see them.</p>
    {#each keywordList as keyword (keyword.id)}
      <div class="keyword-row">
        <span><span class="mono">{keyword.word}</span> <span class="small dim">{APPLIES[keyword.applies]}</span></span>
        <label class="tick small"><input type="checkbox" checked={keyword.email} onchange={(event) => setKeywordEmail(keyword, event.target.checked)} /> Email</label>
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
    <label class="tick"><input type="checkbox" bind:checked={newEmail} /> Email me when it matches</label>
    <div class="buttons form-buttons"><button class="main">Add keyword</button></div>
  </fieldset>
</form>

{#if email?.calls}
  <form onsubmit={(event) => { event.preventDefault(); addWatch({ kind: 'channel', target_id: Number(newChannel) }, () => (newChannel = '')); }}>
    <fieldset>
      <legend>Channels to email about</legend>
      <p class="small">An email for every call.</p>
      {#each channels as watch (watch.id)}
        <div class="keyword-row"><span>{watch.label}</span><button type="button" onclick={() => removeWatch(watch)}>Remove</button></div>
      {:else}
        <p class="small dim">None.</p>
      {/each}
      <label class="field"><span>Channel</span>
        <select bind:value={newChannel} required>
          <option value="" disabled>Choose...</option>
          {#each unwatched as target (target.id)}<option value={String(target.id)}>{target.name}</option>{/each}
        </select>
      </label>
      <div class="buttons form-buttons"><button class="main" disabled={!newChannel}>Add channel</button></div>
    </fieldset>
  </form>
{/if}

{#if email?.calls}
  <form onsubmit={(event) => { event.preventDefault(); addWatch({ kind: 'toneout', value: newToneOut }, () => (newToneOut = '')); }}>
    <fieldset>
      <legend>Tone-outs to email about</legend>
      <p class="small">Emailed straight away. Only heard on targets that decode tone-outs.</p>
      {#each pages as watch (watch.id)}
        <div class="keyword-row"><span>{watch.label}</span><button type="button" onclick={() => removeWatch(watch)}>Remove</button></div>
      {:else}
        <p class="small dim">None.</p>
      {/each}
      <label class="field"><span>Tone-out</span>
        <select bind:value={newToneOut} required>
          <option value="" disabled>Choose...</option>
          {#if !pages.some((w) => w.value === 'any')}<option value="any">Any tone-out, named or not</option>{/if}
          {#each unfollowed as row (row.id)}<option value={String(row.id)}>{row.name} ({row.a_hz}{row.b_hz ? ` / ${row.b_hz}` : ''} Hz)</option>{/each}
        </select>
        {#if !toneOuts.length}<span class="hint">None listed under Admin &gt; Tone-outs yet.</span>{/if}
      </label>
      <div class="buttons form-buttons"><button class="main" disabled={!newToneOut}>Add tone-out</button></div>
    </fieldset>
  </form>
{/if}

{#if emergency}
  <fieldset>
    <legend>Aircraft and ship emergencies</legend>
    <p class="small">Which ones alert you. All are still listed under Emergencies.</p>
    {#each GROUPS as [group, heading] (group)}
      <div class="small dim" style="margin-top:.4rem">{heading}</div>
      {#each emergency.codes.filter((c) => c.kind === group) as c (c.code)}
        <label class="tick"><input type="checkbox" checked={c.chosen} onchange={(event) => toggleCode(c.code, event.target.checked)} /> <span><strong>{c.label}</strong> <span class="small dim">{c.meaning}</span></span></label>
      {/each}
    {/each}
    <label class="field"><span>How far away</span>
      <select value={emergency.km ?? ''} onchange={(event) => saveEmergency({ km: event.target.value === '' ? null : Number(event.target.value) })}>
        <option value="">Anywhere a receiver hears</option>
        {#each emergency.distances as km (km)}<option value={km}>Within {km} km</option>{/each}
      </select>
      <span class="hint">From the SDR that heard it. SDRs without a location always count.</span>
    </label>
    {#if email?.server_enabled}
      <label class="tick"><input type="checkbox" checked={emergency.email} onchange={(event) => saveEmergency({ email: event.target.checked })} /> Email me about these straight away</label>
      <p class="small dim">Sent straight away.</p>
    {/if}
  </fieldset>
{/if}

{#if email?.messages}
  <form onsubmit={(event) => { event.preventDefault(); addWatch({ kind: 'aircraft', value: newAircraft }, () => (newAircraft = '')); }}>
    <fieldset>
      <legend>Aircraft to email about</legend>
      <p class="small">An email for every ACARS message, by registration or flight (N123UA, UA0123).</p>
      {#each aircraft as watch (watch.id)}
        <div class="keyword-row"><span class="mono">{watch.label}</span><button type="button" onclick={() => removeWatch(watch)}>Remove</button></div>
      {:else}
        <p class="small dim">None.</p>
      {/each}
      <label class="field"><span>Registration or flight</span><input class="mono" bind:value={newAircraft} required minlength="2" maxlength="16" autocapitalize="characters" /></label>
      <div class="buttons form-buttons"><button class="main">Add aircraft</button></div>
    </fieldset>
  </form>
{/if}
