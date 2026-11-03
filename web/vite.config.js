import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// base './' keeps every URL relative, so the app works at a domain root,
// on localhost, or under a reverse proxy sub-path without rebuilding.
export default defineConfig({
  base: './',
  plugins: [svelte()],
  build: { outDir: 'dist', assetsInlineLimit: 0 },
  server: { proxy: { '/api': 'http://127.0.0.1:8642' } },
});
