// Rigved's hands on a web page: what can be clicked (numbered, for "click 14"), clicking, scrolling, finding text,
// number hints and typing. Asked by the extension's worker (background.js), never by the page. Only the top frame
// answers. Nothing here is sent anywhere but to Rigveda on this computer.
(() => {
  if (window.top !== window || window.__rigvedaBrowse) return;
  window.__rigvedaBrowse = true;

  const CLICKABLE = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], ' +
    '[role=tab], [role=menuitem], [role=checkbox], [role=option], [role=switch], [role=radio], [contenteditable=true], ' +
    '[onclick], [tabindex]:not([tabindex="-1"])';
  const MOST = 150;
  const numbers = new WeakMap();  // element → its number, kept for as long as the page is
  const byNumber = new Map();
  let next = 1;
  let hints = null;

  function numberOf(element) {
    if (!numbers.has(element)) {
      numbers.set(element, next);
      byNumber.set(next, new WeakRef(element));
      next += 1;
    }
    return numbers.get(element);
  }

  function shown(element) {
    const box = element.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) return false;
    const style = getComputedStyle(element);
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) > 0.05;
  }

  function onScreen(element) {
    const box = element.getBoundingClientRect();
    return box.bottom > 0 && box.top < innerHeight && box.right > 0 && box.left < innerWidth;
  }

  function label(element) {
    const words = element.getAttribute('aria-label') || element.innerText || element.getAttribute('title') ||
      element.querySelector?.('img[alt]')?.alt || element.value || element.placeholder || element.name || '';
    const kind = element.tagName === 'INPUT' || element.tagName === 'TEXTAREA' ? 'field' : '';
    return {text: words.replace(/\s+/g, ' ').trim().slice(0, 100), kind};
  }

  function role(element) {
    const tag = element.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'input' || tag === 'textarea') return element.type === 'submit' || element.type === 'button' ? 'button' : `field (${element.type || 'text'})`;
    return element.getAttribute('role') || (tag === 'select' ? 'list' : tag === 'button' ? 'button' : tag);
  }

  function controls() {
    const found = [];
    for (const element of document.querySelectorAll(CLICKABLE)) {
      if (!shown(element)) continue;
      const {text} = label(element);
      if (!text && role(element).startsWith('field') === false) continue;
      found.push({element, seen: onScreen(element)});
    }
    found.sort((one, other) => (other.seen - one.seen) ||
      (one.element.getBoundingClientRect().top - other.element.getBoundingClientRect().top));
    return found.slice(0, MOST);
  }

  function look() {
    const listed = controls().map(({element, seen}) => ({n: numberOf(element), label: label(element).text || role(element),
      role: role(element), seen}));
    const headings = [...document.querySelectorAll('h1, h2, h3')].filter(shown).slice(0, 20)
      .map(heading => heading.innerText.replace(/\s+/g, ' ').trim().slice(0, 80)).filter(Boolean);
    const height = document.documentElement.scrollHeight - innerHeight;
    return {ok: true, url: location.href, title: document.title, controls: listed, headings,
      scrolled: height > 0 ? Math.round(100 * scrollY / height) : null, video: videoState(mainVideo())};
  }

  function element(n) {
    const found = byNumber.get(Number(n))?.deref();
    return found && found.isConnected ? found : null;
  }

  function click(n, expected) {
    const found = element(n);
    if (!found) return {ok: false, why: 'no such number'};
    const now = label(found).text;
    if (expected && now !== expected) return {ok: false, why: 'changed'};  // after a yes: only the button asked about
    found.scrollIntoView({block: 'center', behavior: 'instant'});
    if (found.matches('input:not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable=true]')) {
      found.focus();
    } else {
      found.focus({preventScroll: true});
      found.click();
    }
    if (hints) setTimeout(drawHints, 400);
    return {ok: true, label: now || role(found)};
  }

  function scroll(how) {
    const step = {down: 0.8, up: -0.8, 'page down': 0.95, 'bit down': 0.35, 'bit up': -0.35}[how];
    if (how === 'top') scrollTo({top: 0, behavior: 'smooth'});
    else if (how === 'bottom') scrollTo({top: document.documentElement.scrollHeight, behavior: 'smooth'});
    else scrollBy({top: (step ?? 0.8) * innerHeight, behavior: 'smooth'});
    return {ok: true};
  }

  let lastFound = null;
  function find(text) {
    const wanted = String(text || '').toLowerCase().trim();
    if (!wanted) return {ok: true, found: false};
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const matches = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.textContent.toLowerCase().includes(wanted) && node.parentElement && shown(node.parentElement)) matches.push(node.parentElement);
    }
    if (!matches.length) return {ok: true, found: false};
    const after = lastFound ? matches.findIndex(match => lastFound.compareDocumentPosition(match) & Node.DOCUMENT_POSITION_FOLLOWING) : 0;
    const chosen = matches[after >= 0 ? after : 0];
    lastFound = chosen;
    chosen.scrollIntoView({block: 'center', behavior: 'smooth'});
    const before = chosen.style.outline;
    chosen.style.outline = '3px solid #f5a623';
    setTimeout(() => { chosen.style.outline = before; }, 2500);
    return {ok: true, found: true, count: matches.length};
  }

  function drawHints() {
    hints?.remove();
    hints = document.createElement('div');
    hints.setAttribute('aria-hidden', 'true');
    hints.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647';
    for (const {element: found, seen} of controls()) {
      if (!seen) continue;
      const box = found.getBoundingClientRect();
      const tag = document.createElement('span');
      tag.textContent = numberOf(found);
      tag.style.cssText = `position:fixed;left:${Math.max(0, box.left - 4)}px;top:${Math.max(0, box.top - 6)}px;` +
        'font:bold 12px/1 system-ui,sans-serif;color:#1b1b1b;background:#ffd84d;border:1px solid #8a6d00;' +
        'border-radius:4px;padding:2px 4px;box-shadow:0 1px 3px rgba(0,0,0,.35)';
      hints.appendChild(tag);
    }
    document.documentElement.appendChild(hints);
  }

  let redraw = 0;
  addEventListener('scroll', () => {
    if (!hints) return;
    clearTimeout(redraw);
    redraw = setTimeout(drawHints, 150);
  }, {passive: true});

  function setHints(on) {
    if (on) drawHints();
    else { hints?.remove(); hints = null; }
    return {ok: true};
  }

  function mainVideo() {
    const videos = [...document.querySelectorAll('video')].filter(shown);
    return videos.sort((one, other) => {
      const a = one.getBoundingClientRect(), b = other.getBoundingClientRect();
      return b.width * b.height - a.width * a.height;
    })[0] || null;
  }

  function videoState(video) {
    return video ? {paused: video.paused, at: Math.round(video.currentTime), length: Number.isFinite(video.duration) ? Math.round(video.duration) : null,
      muted: video.muted || video.volume === 0, speed: video.playbackRate} : null;
  }

  async function playVideo(what, seconds) {
    const video = mainVideo();
    if (!video) return {ok: false, why: 'no video'};
    const step = Number(seconds) || 10;
    switch (what) {
      case 'play': await video.play().catch(() => {}); break;
      case 'pause': video.pause(); break;
      case 'forward': video.currentTime = Math.min(video.currentTime + step, Number.isFinite(video.duration) ? video.duration - 1 : Infinity); break;
      case 'back': video.currentTime = Math.max(0, video.currentTime - step); break;
      case 'mute': video.muted = true; break;
      case 'unmute': video.muted = false; if (video.volume === 0) video.volume = 1; break;
      case 'faster': video.playbackRate = Math.min(3, video.playbackRate + 0.25); break;
      case 'slower': video.playbackRate = Math.max(0.25, video.playbackRate - 0.25); break;
      case 'normal speed': video.playbackRate = 1; break;
      case 'full screen': try { await (video.closest('[class*=player]') || video).requestFullscreen(); } catch { return {ok: false, why: 'needs a key'}; } break;
      case 'exit full screen': if (document.fullscreenElement) await document.exitFullscreen().catch(() => {}); break;
      case 'captions': {
        const tracks = [...video.textTracks];
        if (!tracks.length) return {ok: false, why: 'no captions'};
        const showing = tracks.some(track => track.mode === 'showing');
        tracks.forEach((track, index) => { track.mode = showing ? 'disabled' : index === 0 ? 'showing' : 'disabled'; });
        break;
      }
      default: return {ok: false, why: `can't ${what} a video`};
    }
    return {ok: true, ...videoState(video)};
  }

  function type(text) {
    const box = document.activeElement;
    if (!box || !(box.isContentEditable || /^(INPUT|TEXTAREA)$/.test(box.tagName))) return {ok: false, why: 'no box'};
    if (!document.execCommand('insertText', false, String(text))) {  // what sites built on React notice too
      box.value += String(text);
      box.dispatchEvent(new Event('input', {bubbles: true}));
    }
    return {ok: true};
  }

  // Where you're looking: the mouse moving over this page tells Rigved you mean Chrome (at most every 2 s).
  let told = 0;
  const attend = () => {
    if (Date.now() - told < 2000) return;
    told = Date.now();
    chrome.runtime.sendMessage({type: 'attention'}).catch(() => {});
  };
  addEventListener('mousemove', attend, {passive: true});
  addEventListener('wheel', attend, {passive: true});

  // While Rigved is focused on Chrome, a thin glow around the page shows it, in the orb's colour.
  let glow = null;
  function setGlow(on) {
    if (on && !glow) {
      glow = document.createElement('div');
      glow.setAttribute('aria-hidden', 'true');
      glow.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646;border-radius:6px;' +
        'box-shadow:inset 0 0 0 2px rgba(64,210,255,.85),inset 0 0 22px rgba(64,210,255,.35);transition:opacity .4s;opacity:0';
      document.documentElement.appendChild(glow);
      requestAnimationFrame(() => { if (glow) glow.style.opacity = '1'; });
    } else if (!on && glow) {
      const fading = glow;
      glow = null;
      fading.style.opacity = '0';
      setTimeout(() => fading.remove(), 450);
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message?.type === 'rigveda-glow') { setGlow(Boolean(message.on)); return; }
    if (message?.type !== 'rigveda-browse') return;
    const command = message.command || {};
    const answer = {
      look: () => look(),
      label: () => { const found = element(command.n); return found ? {ok: true, label: label(found).text || role(found)} : {ok: false, why: 'no such number'}; },
      click: () => click(command.n, command.label),
      scroll: () => scroll(command.how),
      find: () => find(command.text),
      hints: () => setHints(command.on !== false),
      type: () => type(command.text),
      video: () => playVideo(command.what, command.seconds),
    }[command.do];
    if (!answer) { reply({ok: false, why: `can't ${command.do} here`}); return; }
    Promise.resolve(answer()).then(reply, error => reply({ok: false, why: String(error)}));
    return true;  // the answer may come later (a video starting to play)
  });
})();
