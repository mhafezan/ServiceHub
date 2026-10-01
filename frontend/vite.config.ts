// Configure local API forwarding without bundling any integration credentials.

import { defineConfig } from 'vite';
export default defineConfig({server: {proxy: {'/api': 'http://localhost:8000', '/guides': 'http://localhost:8000'}}});
