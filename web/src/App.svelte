<script>
  import { onMount } from 'svelte';
  import { api, setSignedOutHandler } from './lib.js';
  import Auth from './views/Auth.svelte';
  import Sources from './views/Sources.svelte';
  import Schedule from './views/Schedule.svelte';
  import Recordings from './views/Recordings.svelte';
  import Targets from './views/Targets.svelte';
  import Maps from './views/Maps.svelte';
  import Account from './views/Account.svelte';
  import Admin from './views/Admin.svelte';

  let user = $state(null);
  let loading = $state(true);
  let route = $state(location.hash.slice(2) || 'sources');

  const view = $derived(route.split('?')[0]);
  const can = (permission) => user?.permissions.includes(permission);
  const links = $derived([
    { id: 'sources', label: 'Sources' },
    { id: 'schedule', label: 'Schedule' },
    { id: 'recordings', label: 'Recordings', show: can('listen_recordings') },
    { id: 'targets', label: 'Targets', show: can('manage_targets') },
    { id: 'maps', label: 'Maps', show: can('maps') },
    { id: 'admin', label: 'Admin', show: user?.role === 'admin' },
    { id: 'account', label: 'Account' },
  ].filter((link) => link.show !== false));

  async function refreshUser() {
    try {
      user = await api('me');
    } catch {
      user = null;
    }
  }

  async function signOut() {
    await api('auth/logout', { method: 'POST' }).catch(() => {});
    user = null;
  }

  onMount(() => {
    setSignedOutHandler(() => { user = null; });
    const onHash = () => { route = location.hash.slice(2) || 'sources'; };
    addEventListener('hashchange', onHash);
    refreshUser().finally(() => { loading = false; });
    return () => removeEventListener('hashchange', onHash);
  });
</script>

{#if loading}
  <div class="empty">Loading…</div>
{:else if !user}
  <Auth onSignedIn={refreshUser} />
{:else}
  <header class="topbar">
    <span class="brand">SentrySDR</span>
    <nav aria-label="Main">
      {#if !user.must_change_password}
        {#each links as link (link.id)}
          <a href={`#/${link.id}`} class:active={view === link.id} aria-current={view === link.id ? 'page' : undefined}>{link.label}</a>
        {/each}
      {/if}
    </nav>
    <span class="muted small">{user.name}</span>
    <button onclick={signOut}>Sign out</button>
  </header>
  <main>
    {#if user.must_change_password}
      <Account {user} onChange={refreshUser} forced />
    {:else if view === 'schedule'}
      <Schedule {user} />
    {:else if view === 'recordings'}
      <Recordings {user} {route} />
    {:else if view === 'targets'}
      <Targets />
    {:else if view === 'maps'}
      <Maps {user} />
    {:else if view === 'account'}
      <Account {user} onChange={refreshUser} />
    {:else if view === 'admin' && user.role === 'admin'}
      <Admin />
    {:else}
      <Sources {user} />
    {/if}
  </main>
{/if}
