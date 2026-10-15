const $ = id => document.getElementById(id);
const send = message => new Promise(resolve => chrome.runtime.sendMessage(message, resolve));

(async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const site = tab?.url?.startsWith('http') ? new URL(tab.url).hostname : '';
  const {disabledSites = [], port = 8765} = await chrome.storage.local.get(['disabledSites', 'port']);
  $('host').textContent = site || 'this page';
  $('site').checked = Boolean(site) && !disabledSites.includes(site);
  $('site').disabled = !site;
  $('site').addEventListener('change', () => send({type: 'setSite', site, enabled: $('site').checked}));
  $('facts').addEventListener('click', event => { event.preventDefault(); chrome.tabs.create({url: `http://127.0.0.1:${port}/facts`}); });

  const status = await send({type: 'status'});
  if (status?.error) {
    $('status').textContent = status.error;
    $('status').className = 'bad';
    return;
  }
  $('status').textContent = `Connected · ${status.approved} approved fact${status.approved === 1 ? '' : 's'}`;
  $('fill').disabled = !site || !$('site').checked;
  $('fill').addEventListener('click', async () => {
    $('fill').disabled = true;
    try {
      const reply = await chrome.tabs.sendMessage(tab.id, {type: 'fillPage'}, {frameId: 0});
      $('result').textContent = reply.filled
        ? `Filled ${reply.filled} of ${reply.fields} empty fields, outlined in orange. Check them, then submit yourself.`
        : 'No empty fields here match your approved facts.';
    } catch {
      $('result').textContent = 'Reload the page and try again.';
    } finally {
      $('fill').disabled = false;
    }
  });
})();
