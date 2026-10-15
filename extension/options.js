const $ = id => document.getElementById(id);

async function load() {
  const {token = '', port = 8765, disabledSites = []} = await chrome.storage.local.get(['token', 'port', 'disabledSites']);
  $('token').value = token;
  $('port').value = port;
  $('sites').textContent = disabledSites.length ? `Turned off on: ${disabledSites.join(', ')} (turn back on from the menu on that site).` : '';
}

$('save').addEventListener('click', async () => {
  const port = Number($('port').value) || 8765;
  await chrome.storage.local.set({token: $('token').value.trim(), port});
  const reply = await chrome.runtime.sendMessage({type: 'status'});
  $('result').className = reply?.error ? 'bad' : 'ok';
  $('result').textContent = reply?.error || `Connected. ${reply.approved} approved facts can be suggested.`;
});

load();
