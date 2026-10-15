// Talks to the local Rigveda server. Only this worker holds the token and makes requests, so web pages
// never see it, and the server needs no CORS permissions that pages could use too.
const DEFAULTS = {port: 8765, token: '', disabledSites: []};

async function settings() {
  return {...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS)))};
}

async function api(path, body) {
  const s = await settings();
  if (!s.token) throw new Error('No token yet. Run `rigveda token` and paste it into the extension options.');
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${s.port}${path}`, {
      method: body ? 'POST' : 'GET',
      headers: {'Content-Type': 'application/json', Authorization: `Bearer ${s.token}`},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error(`Rigveda AI isn't running on port ${s.port}. Start it with \`rigveda serve\`.`);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function handle(message, sender) {
  const s = await settings();
  // A content script speaks for the page it runs in; only the popup may name another site.
  const site = sender.tab?.url ? new URL(sender.tab.url).hostname : (message.site || '');
  switch (message.type) {
    case 'enabled':
      return {enabled: Boolean(s.token) && !s.disabledSites.includes(site)};
    case 'suggest':
      if (s.disabledSites.includes(site)) return {results: message.fields.map(() => [])};
      return api('/api/suggest', {fields: message.fields});
    case 'status':
      return api('/api/suggest/status');
    case 'setSite': {
      if (sender.tab) return {error: 'Only the extension menu can change this.'};
      const sites = new Set(s.disabledSites);
      message.enabled ? sites.delete(site) : sites.add(site);
      await chrome.storage.local.set({disabledSites: [...sites]});
      return {ok: true};
    }
  }
  return {error: 'Unknown request'};
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  handle(message, sender).then(reply, error => reply({error: error.message}));
  return true; // reply asynchronously
});
