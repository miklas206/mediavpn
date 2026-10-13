/* Appearance only: this module never reads or writes app/VPN configuration. */
(() => {
  const names = {
    'midnight-glass': 'Midnight Glass', 'soft-orbit': 'Soft Orbit',
    'precision-terminal': 'Precision Terminal', 'floating-layers': 'Floating Layers',
    'edge-signal': 'Edge Signal', light: 'Light', classic: 'MediaHub Classic',
  };
  const palettes = {
    'midnight-glass': ['#0b1821','#132530','#1b303e','#29404e','#e9f0f6','#a6b9c9','#35d7f6','#103b4d','#03151e'],
    'soft-orbit': ['#101a2d','#1b2d49','#294163','#3b5275','#edf3ff','#b1c2dd','#99b8ff','#263e65','#06172e'],
    light: ['#eef3f6','#ffffff','#e1ebf1','#c8d7e1','#162b3a','#526b7b','#087ea3','#d9edf3','#ffffff'],
    'precision-terminal': ['#0b1210','#141c19','#202c26','#35493e','#edf7f1','#a4bcae','#7ce3b6','#1b3b2c','#062518'],
    'floating-layers': ['#101a2b','#1b2a40','#273b55','#425a78','#f0f5ff','#b4c7e0','#88b5ff','#263e63','#07162c'],
    'edge-signal': ['#0d1115','#1c2025','#2b3037','#444c56','#f8f4ed','#c2bbb0','#f5bd59','#44351d','#241a09'],
    classic: ['#080d15','#101923','#162231','#263547','#edf3fc','#94a5bc','#25c9ed','#102d40','#041520'],
  };
  const keys = ['--bg','--surface','--raised','--line','--text','--muted','--accent','--accent-bg','--accent-contrast'];
  const valid = name => Object.prototype.hasOwnProperty.call(names, name);
  const root = document.documentElement;
  const storageKey = 'mediahub-app-theme-v2';
  let initial = 'midnight-glass';
  try { const saved = localStorage.getItem(storageKey); if (valid(saved)) initial = saved; } catch {}
  const url = new URL(location.href);
  const incoming = url.searchParams.get('mediahub_theme');
  if (valid(incoming)) initial = url.searchParams.get("mediahub_mode") === "light" ? "light" : incoming;
  const select = document.createElement('select');
  select.setAttribute('aria-label', 'Theme / Tema');
  for (const [value, text] of Object.entries(names)) {
    const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option);
  }
  const control = document.createElement('label'); control.className = 'theme-switch';
  const caption = document.createElement('span'); caption.textContent = 'Theme';
  control.append(caption, select);
  document.querySelector('.topbar')?.append(control);
  function apply(name) {
    if (!valid(name)) return false;
    root.dataset.look = name === 'light' ? 'midnight-glass' : name;
    root.dataset.appearanceMode = name === 'light' ? 'light' : 'dark';
    keys.forEach((key,index) => root.style.setProperty(key,palettes[name][index]));
    root.style.setProperty('--accent-line', 'color-mix(in srgb, var(--accent) 36%, var(--line))');
    root.style.setProperty('--healthy', name === 'light' ? '#087952' : '#6ed9a6');
    root.style.setProperty('--danger', name === 'light' ? '#bb3549' : '#fa7c87');
    select.value = name;
    try { localStorage.setItem(storageKey,name); } catch {}
    return true;
  }
  select.addEventListener('change', () => apply(select.value));
  new MutationObserver(() => { caption.textContent = root.lang === 'da' ? 'Tema' : 'Theme'; }).observe(root, { attributes: true, attributeFilter: ['lang'] });
  apply(initial);
  const message = document.querySelector('#message');
  if (message) {
    const dismiss = document.createElement('button'); dismiss.type = 'button'; dismiss.className = 'notification-dismiss';
    dismiss.textContent = '×'; dismiss.setAttribute('aria-label', 'Dismiss notification'); dismiss.hidden = !message.textContent;
    dismiss.addEventListener('click', () => { message.textContent = ''; });
    message.parentElement.append(dismiss);
    new MutationObserver(() => { dismiss.hidden = !message.textContent; }).observe(message, { childList: true, characterData: true, subtree: true });
  }
  globalThis.MediaAppTheme = { apply };
})();
