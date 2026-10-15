// Shows suggestions from your approved Rigveda facts under form fields. Tab or Enter accepts, the arrow keys
// choose, Esc dismisses. Never fills passwords or payment cards (the server refuses those too) and never submits.
(() => {
  if (window.__rigvedaAutofill) return;
  window.__rigvedaAutofill = true;

  let enabled = false;
  const refresh = () => chrome.runtime.sendMessage({type: 'enabled'}, reply => { enabled = Boolean(reply?.enabled); });
  refresh();
  chrome.storage.onChanged.addListener(refresh);

  // The dropdown lives in a closed shadow root: the page's styles can't change it and its scripts can't read it.
  const host = document.createElement('rigveda-autofill');
  host.style.cssText = 'all: initial; position: fixed; z-index: 2147483647; top: 0; left: 0;';
  const root = host.attachShadow({mode: 'closed'});
  root.innerHTML = `<style>
    .box{position:fixed;min-width:240px;max-width:420px;background:#14112e;color:#f4f1ea;border:1px solid #3a3266;border-radius:10px;
      box-shadow:0 10px 30px rgba(0,0,0,.35);font:13px system-ui,sans-serif;overflow:hidden}
    .item{padding:8px 12px;cursor:pointer;display:flex;flex-direction:column;gap:2px;border-left:3px solid transparent}
    .item.on{background:#2a2160;border-left-color:#f39a33}.value{font-weight:600;font-size:14px;word-break:break-word}
    .meta{color:#b8b0d8;font-size:11px}.hint{padding:5px 12px;color:#8f88b5;font-size:11px;border-top:1px solid #2a2450}
  </style><div class="box" hidden></div>`;
  const box = root.querySelector('.box');
  document.documentElement.append(host);

  let field = null, items = [], chosen = 0, request = 0;
  const dismissed = new WeakSet();

  function eligible(el) {
    if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement)) return false;
    if (el.disabled || el.readOnly) return false;
    const type = (el.type || '').toLowerCase();
    return !['password', 'hidden', 'file', 'submit', 'button', 'reset', 'checkbox', 'radio', 'image', 'range', 'color'].includes(type);
  }

  function empty(el) {
    return el instanceof HTMLSelectElement ? !el.value : !el.value.trim();
  }

  function text(node) {
    if (!node) return '';
    const copy = node.cloneNode(true);
    copy.querySelectorAll('input, select, textarea, option, script, style').forEach(child => child.remove());
    return copy.textContent.replace(/\s+/g, ' ').trim().slice(0, 120);
  }

  function describe(el) {
    const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => text(document.getElementById(id))).join(' ');
    const nearby = text(el.previousElementSibling || el.parentElement?.previousElementSibling);
    const field = {
      label: (el.labels && el.labels.length ? text(el.labels[0]) : '') || labelledBy,
      ariaLabel: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      type: el instanceof HTMLSelectElement ? 'select' : (el.type || el.tagName).toLowerCase(),
      nearby: nearby.slice(0, 80),
    };
    if (el instanceof HTMLSelectElement) field.options = [...el.options].slice(0, 300).map(o => ({value: o.value, text: o.text}));
    return field;
  }

  function suggest(fields) {
    return new Promise(resolve => chrome.runtime.sendMessage({type: 'suggest', fields}, reply => resolve(reply?.results || [])));
  }

  function setValue(el, suggestion) {
    if (el instanceof HTMLSelectElement) {
      el.value = suggestion.option ?? suggestion.value;
    } else {
      // The native setter, so pages built with React and similar frameworks notice the change.
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, suggestion.value);
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function hide() {
    box.hidden = true;
    items = [];
  }

  function place() {
    if (!field || box.hidden) return;
    const r = field.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) return hide();
    box.style.left = `${Math.max(4, Math.min(r.left, innerWidth - 250))}px`;
    box.style.top = `${r.bottom + 4}px`;
    box.style.minWidth = `${Math.max(240, r.width)}px`;
  }

  function render() {
    box.replaceChildren();
    items.forEach((s, i) => {
      const item = document.createElement('div');
      item.className = 'item' + (i === chosen ? ' on' : '');
      const value = document.createElement('div');
      value.className = 'value';
      value.textContent = s.value;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${s.label} · ${s.source}`;
      item.append(value, meta);
      item.addEventListener('mousedown', event => event.preventDefault()); // keep focus in the field
      item.addEventListener('click', event => { if (event.isTrusted) accept(i); });
      box.append(item);
    });
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'Tab to fill · ↑↓ to choose · Esc to close · Rigveda AI';
    box.append(hint);
    box.hidden = false;
    place();
  }

  function accept(i) {
    if (!field || !items[i]) return;
    setValue(field, items[i]);
    dismissed.add(field);
    hide();
  }

  let loading = null;

  async function show(el) {
    // The first click into a page reports focus twice (window, then field); keep what is already showing.
    if (el === field && (!box.hidden || loading === el)) return;
    field = el;
    hide();
    if (!enabled || !eligible(el) || !empty(el) || dismissed.has(el)) return;
    const mine = ++request;
    loading = el;
    const [results] = await suggest([describe(el)]).finally(() => { if (mine === request) loading = null; });
    if (mine !== request || field !== el || document.activeElement !== el || !empty(el)) return;
    items = results || [];
    chosen = 0;
    if (items.length) render();
  }

  document.addEventListener('focusin', event => show(event.target), true);
  // Hide when focus moves to something else, but not when the whole window loses focus for a moment
  // (switching apps and back): the field is still the active element then.
  document.addEventListener('focusout', event => {
    if (event.target === field) setTimeout(() => { if (document.activeElement !== field) hide(); });
  }, true);
  document.addEventListener('input', event => { if (event.target === field && event.isTrusted && !empty(field)) hide(); }, true);
  addEventListener('scroll', place, true);
  addEventListener('resize', place);

  document.addEventListener('keydown', event => {
    // Only real key presses: a page's script must not be able to accept a suggestion and then read it.
    if (box.hidden || !items.length || !event.isTrusted || event.target !== field) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      chosen = (chosen + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
      render();
      event.preventDefault();
    } else if (event.key === 'Tab' && !event.shiftKey) {
      accept(chosen); // focus then moves on to the next field as usual, so Tab, Tab, Tab fills a form
    } else if (event.key === 'Enter') {
      accept(chosen);
      event.preventDefault(); // Enter would otherwise submit the form
      event.stopPropagation();
    } else if (event.key === 'Escape') {
      dismissed.add(field);
      hide();
      event.preventDefault();
    }
  }, true);

  // "Fill this page" from the extension menu: fill every empty field with its best match, outlined for review.
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.type !== 'fillPage') return;
    const fields = [...document.querySelectorAll('input, select, textarea')]
      .filter(el => eligible(el) && empty(el) && el.getClientRects().length);
    if (!fields.length) { reply({filled: 0, fields: 0}); return; }
    suggest(fields.map(describe)).then(results => {
      let filled = 0;
      fields.forEach((el, i) => {
        const best = results[i]?.[0];
        if (!best || !empty(el)) return;
        setValue(el, best);
        el.style.outline = '2px solid #f39a33';
        el.style.outlineOffset = '1px';
        el.addEventListener('focus', () => { el.style.outline = ''; el.style.outlineOffset = ''; }, {once: true});
        filled++;
      });
      reply({filled, fields: fields.length});
    });
    return true;
  });
})();
