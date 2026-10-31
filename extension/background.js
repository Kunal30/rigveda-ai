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
    case 'attention':  // the mouse moved over a page (browse.js)
      if (sender.tab?.active) { pointerAt = Date.now(); attend(); }
      return {ok: true};
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

// ── Rigved in the browser: commands from the voice listener, through the local server (browser.py) ──
// The worker asks the server for something to do and holds the request open; the server hands it a command
// ("click 14", "go to the Gmail tab"), and the answer goes back the same way. Pages are asked by browse.js.

async function activeTab() {
  const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  return tab;
}

async function onPage(tab, command) {
  if (!tab?.id || !/^https?:/.test(tab.url || '')) return {ok: false, why: 'no page'};
  const send = () => chrome.tabs.sendMessage(tab.id, {type: 'rigveda-browse', command}, {frameId: 0});
  try {
    return await send();
  } catch {  // a tab opened before the extension was (re)loaded has no browse.js yet
    try {
      await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['browse.js']});
      return await send();
    } catch {
      return {ok: false, why: 'no page'};
    }
  }
}

function address(said) {
  const text = String(said || '').trim();
  if (!text) return undefined;
  if (/^https?:\/\//.test(text)) return text;
  if (/^[\w-]+(\.[\w-]+)+(\/\S*)?$/.test(text)) return `https://${text}`;
  return `https://www.google.com/search?q=${encodeURIComponent(text)}`;
}

async function browse(command) {
  const tab = await activeTab();
  switch (command.do) {
    case 'look': {
      const tabs = (await chrome.tabs.query({})).map(each => ({id: each.id, title: each.title, active: each.id === tab?.id,
        site: (() => { try { return new URL(each.url).hostname; } catch { return ''; } })()}));
      const page = await onPage(tab, {do: 'look'});
      return {ok: true, tabs, page: page.ok ? page : {title: tab?.title, url: tab?.url, controls: [], headings: []}};
    }
    case 'back': await chrome.tabs.goBack(tab.id); return {ok: true};
    case 'forward': await chrome.tabs.goForward(tab.id); return {ok: true};
    case 'reload': await chrome.tabs.reload(tab.id); return {ok: true};
    case 'tab': {
      const chosen = await chrome.tabs.update(Number(command.id), {active: true});
      await chrome.windows.update(chosen.windowId, {focused: true});
      return {ok: true, title: chosen.title};
    }
    case 'close': {
      const open = new Set((await chrome.tabs.query({})).map(each => each.id));
      const ids = (command.ids || []).map(Number).filter(id => open.has(id));
      if (ids.length) await chrome.tabs.remove(ids);
      return {ok: true, closed: ids.length};
    }
    case 'reopen': {
      const restored = await chrome.sessions.restore();
      return {ok: true, title: restored?.tab?.title || restored?.window?.tabs?.[0]?.title || ''};
    }
    case 'go': {
      await chrome.tabs.update(tab.id, {url: address(command.to)});
      return {ok: true};
    }
    case 'focus': {  // "focus on Chrome": Rigved stays with the browser until told to stop
      locked = command.on !== false;
      await chrome.storage.local.set({focusLocked: locked});
      attend();
      return {ok: true, on: locked};
    }
    case 'new': {
      const created = await chrome.tabs.create({url: address(command.url)});
      return {ok: true, title: created.title};
    }
    default:
      return onPage(tab, command);
  }
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      let job;
      try {
        job = await api('/api/browser/next');
      } catch {
        await new Promise(wake => setTimeout(wake, 5000));  // Rigveda isn't running, or no token yet
        continue;
      }
      if (!job.id) continue;
      const result = await browse(job.command || {}).catch(error => ({ok: false, why: error.message}));
      await api('/api/browser/done', {id: job.id, result}).catch(() => {});
    }
  } finally {
    polling = false;
  }
}

// A worker with nothing to do is stopped after half a minute; this keeps it, and the alarm brings it back if not.
setInterval(() => chrome.runtime.getPlatformInfo(), 20_000);
chrome.alarms.create('rigveda-browse', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'rigveda-browse') poll(); });
chrome.runtime.onStartup.addListener(poll);
poll();

// ── focus: whether Rigved takes what you say to be about Chrome ──
// On while you've said "focus on Chrome", while the mouse is over a page here, or while Chrome is the window in front
// (and for a while after the mouse leaves). The badge on the Rigveda button and a glow around the page show it.
const LINGER = 45_000;
let pointerAt = 0, focusedAt = 0, locked = false, chromeFocused = false, shownOn = null, shownTab = null;
chrome.storage.local.get('focusLocked').then(saved => { locked = Boolean(saved.focusLocked); attend(); });

chrome.windows.onFocusChanged.addListener(windowId => {
  chromeFocused = windowId !== chrome.windows.WINDOW_ID_NONE;
  if (chromeFocused) focusedAt = Date.now();
  attend();
});
chrome.tabs.onActivated.addListener(() => { shownTab = null; attend(); });

function focusOn() {
  return locked || chromeFocused || Date.now() - Math.max(pointerAt, focusedAt) < LINGER;
}

let toldAt = 0;
async function attend() {
  const on = focusOn();
  const tab = await activeTab().catch(() => null);
  if (on !== shownOn) {
    chrome.action.setBadgeText({text: on ? (locked ? 'ON' : '●') : ''});
    chrome.action.setBadgeBackgroundColor({color: '#1fb8e8'});
    chrome.action.setTitle({title: on ? 'Rigved is focused on Chrome' + (locked ? ' (say "stop focusing" to end it)' : '') : 'Rigveda AI'});
  }
  if (tab?.id && (on !== shownOn || tab.id !== shownTab)) {
    for (const other of await chrome.tabs.query({})) {
      if (other.id !== tab.id || !on) chrome.tabs.sendMessage(other.id, {type: 'rigveda-glow', on: false}, {frameId: 0}).catch(() => {});
    }
    if (on) chrome.tabs.sendMessage(tab.id, {type: 'rigveda-glow', on: true}, {frameId: 0}).catch(() => {});
    shownTab = tab.id;
  }
  const changed = on !== shownOn;
  shownOn = on;
  if (changed || (on && Date.now() - toldAt > 3000)) {  // Rigveda hears where you are, at most every 3 s
    toldAt = Date.now();
    api('/api/browser/seen', {on, locked, focused: chromeFocused, title: tab?.title || '', url: tab?.url || ''}).catch(() => {});
  }
}
setInterval(attend, 5000);  // to notice the mouse has been away long enough
