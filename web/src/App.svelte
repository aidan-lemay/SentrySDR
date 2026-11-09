<script>
  import { onMount } from 'svelte';
  import { api, setSignedOutHandler } from './lib.js';
  import Auth from './views/Auth.svelte';
  import Sources from './views/Sources.svelte';
  import Schedule from './views/Schedule.svelte';
  import Recordings from './views/Recordings.svelte';
  import Targets from './views/Targets.svelte';
  import Maps from './views/Maps.svelte';
  import Messages from './views/Messages.svelte';
  import Account from './views/Account.svelte';
  import Admin from './views/Admin.svelte';

  let user = $state(null);
  let loading = $state(true);
  let menuOpen = $state(false);
  let route = $state(location.hash.slice(2) || 'sources');

  const requested = $derived(route.split('?')[0]);
  const can = (permission) => user?.permissions.includes(permission);
  const links = $derived([
    { id: 'sources', label: 'Sources' },
    { id: 'schedule', label: 'Schedule' },
    { id: 'recordings', label: 'Recordings', show: can('listen_recordings') },
    { id: 'targets', label: 'Targets', show: can('manage_targets') },
    { id: 'maps', label: 'Maps', show: can('maps') },
    { id: 'messages', label: 'Messages', show: can('maps') },
    { id: 'admin', label: 'Admin', show: user?.role === 'admin' },
    { id: 'account', label: 'My account' },
  ].filter((link) => link.show !== false));
  // Only pages in this user's menu open, even if typed into the address bar. Anything else shows Sources.
  const view = $derived(links.some((link) => link.id === requested) ? requested : 'sources');

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
    menuOpen = false;
  }

  onMount(() => {
    setSignedOutHandler(() => { user = null; });
    const onHash = () => {
      route = location.hash.slice(2) || 'sources';
      menuOpen = false;
      scrollTo(0, 0);
    };
    addEventListener('hashchange', onHash);
    refreshUser().finally(() => { loading = false; });
    return () => removeEventListener('hashchange', onHash);
  });
</script>

{#if loading}
  <p class="pad">Loading...</p>
{:else if !user}
  <Auth onSignedIn={refreshUser} />
{:else}
  <header class="site">
    <div class="site-top">
      <span class="site-name">SentrySDR</span>
      {#if !user.must_change_password}
        <button class="menu-button" aria-expanded={menuOpen} onclick={() => (menuOpen = !menuOpen)}>{menuOpen ? 'Close' : 'Menu'}</button>
      {:else}
        <button onclick={signOut}>Sign out</button>
      {/if}
    </div>
    {#if !user.must_change_password}
      <nav class:open={menuOpen} aria-label="Main">
        {#each links as link (link.id)}
          <a href={`#/${link.id}`} aria-current={view === link.id ? 'page' : undefined}>{link.label}</a>
        {/each}
        <button class="nav-out" onclick={signOut}>Sign out ({user.name})</button>
      </nav>
      <div class="who small">
        <span>{user.name}</span>
        <button onclick={signOut}>Sign out</button>
      </div>
    {/if}
  </header>
  <main>
    {#if user.must_change_password}
      <Account {user} onChange={refreshUser} forced />
    {:else if view === 'schedule'}
      <Schedule {user} />
    {:else if view === 'recordings'}
      <Recordings {user} {route} />
    {:else if view === 'targets'}
      <Targets {user} />
    {:else if view === 'maps'}
      <Maps {user} />
    {:else if view === 'messages'}
      <Messages {route} />
    {:else if view === 'account'}
      <Account {user} onChange={refreshUser} />
    {:else if view === 'admin'}
      <Admin />
    {:else}
      <Sources {user} />
    {/if}
  </main>
{/if}
