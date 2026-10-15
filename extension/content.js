// Shows suggestions from your approved Rigveda facts under form fields. Tab or Enter accepts, the arrow keys
// choose, Esc dismisses. Never fills passwords or payment cards (the server refuses those too) and never submits.
// Writing help: Alt+Shift+R (or right-click → Write with Rigveda) in a text box shows a draft from your files,
// or the next sentences of what you're writing. Tab inserts it at the cursor.
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
    .meta{color:#b8b0d8;font-size:11px}.draft .value{font-weight:400;white-space:pre-wrap;max-height:260px;overflow:auto;line-height:1.45}
    .box.wide{max-width:560px}.wait .value{color:#d8d2f0;font-weight:400;font-style:italic}.hint{padding:5px 12px;color:#8f88b5;font-size:11px;border-top:1px solid #2a2450}
  </style><div class="box" hidden></div>`;
  const box = root.querySelector('.box');
  document.documentElement.append(host);

  let field = null, items = [], chosen = 0, request = 0, writing = null;
  const dismissed = new WeakSet();

  function eligible(el) {
    if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement)) return false;
    if (el.disabled || el.readOnly) return false;
    const type = (el.type || '').toLowerCase();
    return !['password', 'hidden', 'file', 'submit', 'button', 'reset', 'checkbox', 'radio', 'image', 'range', 'color'].includes(type);
  }

  function empty(el) {
    return el instanceof HTMLSelectElement ? !el.value : !String(el.value ?? el.textContent ?? '').trim();
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
    writing = null;
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
      item.className = 'item' + (i === chosen ? ' on' : '') + (s.kind ? ' ' + s.kind : '');
      const value = document.createElement('div');
      value.className = 'value';
      value.textContent = s.value;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = [s.label, s.source].filter(Boolean).join(' · ');
      item.append(value);
      if (meta.textContent) item.append(meta);
      item.addEventListener('mousedown', event => event.preventDefault()); // keep focus in the field
      item.addEventListener('click', event => { if (event.isTrusted) accept(i); });
      box.append(item);
    });
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = writing ? writing.hint : 'Tab to fill · ↑↓ to choose · Esc to close · Rigveda AI';
    box.append(hint);
    box.classList.toggle('wide', Boolean(writing));
    box.hidden = false;
    place();
  }

  function accept(i) {
    if (!field || !items[i]) return;
    if (writing) return items[i].kind === 'draft' && insertDraft(writing, items[i].value);
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
    if (box.hidden || !event.isTrusted || event.target !== field) return;
    if (writing) {
      if (event.key === 'Escape') {
        request++; // a draft still on its way is dropped
        hide();
        event.preventDefault();
      } else if (event.key === 'Tab' && !event.shiftKey && items[0]?.kind === 'draft') {
        accept(0);
        event.preventDefault(); // stay in the box, after the inserted text
      }
      return;
    }
    if (!items.length) return;
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

  // Writing help.
  function textBox(el) {
    if (el instanceof HTMLTextAreaElement) return !el.disabled && !el.readOnly;
    if (el instanceof HTMLInputElement) return eligible(el) && ['text', 'search'].includes((el.type || 'text').toLowerCase());
    return Boolean(el?.isContentEditable);
  }

  // The text either side of the cursor, and how to put the cursor back there.
  function caret(el) {
    if (!(el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement)) {
      const selection = getSelection();
      let range = selection.rangeCount ? selection.getRangeAt(0) : null;
      if (!range || !el.contains(range.commonAncestorContainer)) {
        range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
      }
      const head = document.createRange(), tail = document.createRange();
      head.selectNodeContents(el);
      head.setEnd(range.startContainer, range.startOffset);
      tail.selectNodeContents(el);
      tail.setStart(range.endContainer, range.endOffset);
      return {before: head.toString(), after: tail.toString(), range: range.cloneRange(), text: el.textContent};
    }
    const start = el.selectionStart ?? el.value.length, end = el.selectionEnd ?? start;
    return {before: el.value.slice(0, start), after: el.value.slice(end), start, end, text: el.value};
  }

  function insertDraft(state, text) {
    const el = state.el;
    hide();
    el.focus();
    if (state.range) {
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(state.range);
    } else {
      el.setSelectionRange(state.start, state.end);
    }
    // insertText keeps the page's undo history and is what editors such as Gmail's listen for.
    if (document.execCommand('insertText', false, text)) return;
    if (state.range) {
      state.range.deleteContents();
      state.range.insertNode(document.createTextNode(text));
      getSelection().collapseToEnd();
    } else {
      el.setRangeText(text, state.start, state.end, 'end');
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
  }

  function notice(message, el) {
    field = el || null;
    writing = {hint: 'Rigveda AI', el};
    items = [{value: message, label: '', source: '', kind: 'wait'}];
    render();
    if (!el) { box.style.left = `${Math.max(4, innerWidth - box.offsetWidth - 16)}px`; box.style.top = '16px'; }
    const shown = items;
    setTimeout(() => { if (items === shown) hide(); }, 5000);
  }

  async function startWriting() {
    const el = document.activeElement;
    if (!textBox(el)) return notice('Click in a text box first, then press Alt+Shift+R.', null);
    const at = caret(el);
    const ask = {
      field: {...describe(el), maxLength: el.maxLength > 0 ? el.maxLength : 0},
      before: at.before.slice(-3000), after: at.after.slice(0, 600), title: document.title.slice(0, 200),
    };
    const mine = ++request;
    field = el;
    writing = {el, ...at, hint: 'Writing from your files on this computer… · Esc to cancel'};
    items = [{value: at.before.trim() ? 'Continuing your text…' : 'Drafting an answer…', label: '', source: '', kind: 'wait'}];
    render();
    const reply = await new Promise(resolve => chrome.runtime.sendMessage({type: 'write', request: ask}, resolve));
    if (mine !== request || !writing || writing.el !== el) return; // cancelled or replaced
    if (document.activeElement !== el || caret(el).text !== at.text) return hide(); // you kept typing
    if (!reply || reply.error || !reply.text?.trim()) {
      return notice(reply?.error || 'Nothing to suggest here from your files.', el);
    }
    const from = reply.sources.length ? 'from ' + reply.sources.slice(0, 3).map(s => s.name).join(', ')
      : 'nothing in your files covers this' + (reply.text.includes('[') ? ': fill in the [brackets]' : '');
    writing.hint = 'Tab to insert · Alt+Shift+R for another · Esc to discard';
    items = [{value: reply.text, label: reply.mode === 'continue' ? 'Continues your text' : 'Draft', source: from, kind: 'draft'}];
    chosen = 0;
    render();
  }

  chrome.runtime.onMessage.addListener(message => {
    // The shortcut reaches every frame on the page; only the one with the keyboard focus answers.
    if (message.type === 'startWriting' && (!message.anyFrame || document.hasFocus())) startWriting();
  });

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
