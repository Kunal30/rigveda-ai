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
    case 'write':
      if (s.disabledSites.includes(site)) throw new Error(`Rigveda is switched off on ${site}.`);
      return api('/api/write', {...message.request, site});
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

// Writing help starts from the keyboard shortcut (Alt+Shift+R, changeable at chrome://extensions/shortcuts) or
// the right-click menu. Both come from the browser itself, so a web page can't start one.
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({id: 'write', title: 'Write with Rigveda', contexts: ['editable']});
});

function startWriting(tab, frameId) {
  if (tab?.id === undefined || !tab.url?.startsWith('http')) return;
  // From the shortcut every frame hears it, and the one holding the keyboard focus answers.
  const target = frameId === undefined ? {} : {frameId};
  chrome.tabs.sendMessage(tab.id, {type: 'startWriting', anyFrame: frameId === undefined}, target).catch(() => {});
}

chrome.commands.onCommand.addListener((command, tab) => { if (command === 'write') startWriting(tab); });
chrome.contextMenus.onClicked.addListener((info, tab) => { if (info.menuItemId === 'write') startWriting(tab, info.frameId); });
