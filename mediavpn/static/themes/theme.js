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
  let managed = false, hubIdentity = null, managedKeys = [], overrideHandler = null, savingOverride = false;
  const systemMode = matchMedia('(prefers-color-scheme: light)');
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
  const follow = document.createElement('option'); follow.value = 'follow-hub'; follow.textContent = 'Follow MediaHub'; follow.hidden = true; select.prepend(follow);
  const control = document.createElement('label'); control.className = 'theme-switch';
  const caption = document.createElement('span'); caption.textContent = 'Theme';
  control.append(caption, select);
  const appearanceHost = document.querySelector('#panel-appearance');
  const settings = document.createElement(appearanceHost ? 'section' : 'dialog'); settings.className = appearanceHost ? 'app-settings-dialog app-settings-inline' : 'app-settings-dialog'; settings.id = 'app-settings'; settings.setAttribute('aria-labelledby','app-settings-title');
  settings.innerHTML = '<div class="app-settings-heading"><h2 id="app-settings-title"></h2><button type="button" class="app-settings-close"></button></div><section class="app-settings-panel"><h3 class="app-settings-appearance"></h3><p class="app-settings-help"></p></section><p class="app-settings-message" role="status" aria-live="polite" hidden></p>';
  settings.querySelector('.app-settings-panel').append(control);
  const button = document.createElement('button'); button.type = 'button'; button.className = 'app-settings-button'; button.setAttribute('aria-haspopup','dialog'); button.setAttribute('aria-controls','app-settings');
  button.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 3-.6 2.2-2 .9L4.3 5.5 2.5 8.6l1.6 1.6-.2 2.2-1.4 1.7 1.8 3.1 2.1-.6 2 1 .6 2.4h3.6l.6-2.4 2-1 2.1.6 1.8-3.1-1.4-1.7-.2-2.2 1.6-1.6-1.8-3.1-2.1.6-2-.9L12.6 3Z"/><circle cx="10.8" cy="11.5" r="3"/></svg><span></span>';
  const topbar = document.querySelector('.topbar');
  if (appearanceHost) appearanceHost.append(settings);
  else { topbar?.insertBefore(button,topbar.querySelector('#logout')); document.body.append(settings); }
  const localMessage=settings.querySelector('.app-settings-message');
  button.addEventListener('click',()=>{localMessage.hidden=true;settings.showModal();});
  const close=settings.querySelector('.app-settings-close');close.hidden=!!appearanceHost;close.textContent='\u00d7';close.addEventListener('click',()=>settings.close());
  settings.addEventListener('click',event=>{const r=settings.getBoundingClientRect();if(event.target===settings && (event.clientX<r.left || event.clientX>r.right || event.clientY<r.top || event.clientY>r.bottom))settings.close();});
  const extensionLink=document.querySelector('.browser-extension-link');
  let extension=null;
  if(extensionLink) {
    extension=document.createElement('section');extension.className='app-settings-panel app-settings-extension';
    extension.innerHTML='<h3></h3><p class="extension-help"></p><label><span class="extension-browser-label"></span><select class="settings-extension-browser"><option value="chromium">Chrome / Edge / Brave / Opera</option><option value="firefox">Firefox</option><option value="safari">Safari / Xcode</option></select></label><div class="app-settings-actions"><a class="primary settings-extension-download" download></a></div>';
    extensionLink.className='settings-extension-guide';extension.querySelector('.app-settings-actions').append(extensionLink);settings.insertBefore(extension,localMessage);
    extension.querySelector('select').value=/Firefox/i.test(navigator.userAgent)?'firefox':/Safari/i.test(navigator.userAgent)&&!/Chrome|Chromium|Edg/i.test(navigator.userAgent)?'safari':'chromium';
    extension.querySelector('select').addEventListener('change',label);
  }

  function apply(name) {
    if (managed || !valid(name)) return false;
    managedKeys.forEach(key => root.style.removeProperty(key)); managedKeys = [];
    root.dataset.themeSource = 'local';
    delete root.dataset.density; delete root.dataset.reducedMotion;
    root.dataset.look = name === 'light' ? 'midnight-glass' : name;
    root.dataset.appearanceMode = name === 'light' ? 'light' : 'dark';
    root.style.colorScheme = root.dataset.appearanceMode;
    keys.forEach((key,index) => root.style.setProperty(key,palettes[name][index]));
    root.style.setProperty('--accent-line', 'color-mix(in srgb, var(--accent) 36%, var(--line))');
    root.style.setProperty('--healthy', name === 'light' ? '#087952' : '#6ed9a6');
    root.style.setProperty('--danger', name === 'light' ? '#bb3549' : '#fa7c87');
    select.value = name;
    try { localStorage.setItem(storageKey,name); } catch {}
    return true;
  }
  select.addEventListener('change', async () => {
    if (!managed) { apply(select.value); return; }
    if (!overrideHandler || savingOverride) return;
    const override = select.value === 'follow-hub' ? null : select.value;
    savingOverride = true; select.disabled = true; localMessage.hidden=true;
    try {
      const result = await overrideHandler(override);
      savingOverride = false;
      applyIdentity({...hubIdentity, themeOverride: result.themeOverride});
    } catch (error) {
      savingOverride = false; applyIdentity(hubIdentity);
      localMessage.textContent=globalThis.MediaAppI18n?.t(error.message)||error.message; localMessage.hidden=false;
      window.dispatchEvent(new CustomEvent('mediahub-theme-error', {detail: error.message}));
    } finally { savingOverride = false; select.disabled = false; }
  });
  const inheritedName = document.createElement('span'); inheritedName.className = 'managed-theme-name'; inheritedName.hidden = true; control.append(inheritedName);
  function label() {
    const da=root.lang==='da';
    settings.querySelector('h2').textContent=appearanceHost?(da?'Udseende':'Appearance'):(da?'Indstillinger':'Settings');
    button.querySelector('span').textContent=da?'Indstillinger':'Settings';button.title=da?'Indstillinger':'Settings';button.setAttribute('aria-label',button.title);
    close.setAttribute('aria-label',da?'Luk indstillinger':'Close settings');
    settings.querySelector('.app-settings-appearance').textContent=da?'Udseende':'Appearance';
    settings.querySelector('.app-settings-help').textContent=da?'Tema til denne app. Gemmes automatisk.':'Choose a theme for this app. Changes save automatically.';
    if(extension) {
      const browser=extension.querySelector('select').value;
      extension.querySelector('h3').textContent=da?'Browserudvidelse':'Browser extension';
      extension.querySelector('.extension-browser-label').textContent=da?'Browser':'Browser';
      extension.querySelector('.extension-help').textContent=browser==='safari'?(da?'Safari-kilde til Xcode. Kræver en Mac og manuel pakning.':'Safari source for Xcode. Requires a Mac and manual packaging.'):(da?'Send magnetlinks og torrentfiler til MediaSeed. Installeres manuelt.':'Send magnet links and torrent files to MediaSeed. Manual installation.');
      const download=extension.querySelector('.settings-extension-download');download.href='/static/extensions/mediaseed-'+browser+(browser==='safari'?'-source':'')+'.zip';download.textContent=browser==='safari'?(da?'Hent Safari-kilde':'Download Safari source'):(da?'Hent udvidelse':'Download extension');
      extensionLink.textContent=da?'Installationsvejledning':'Installation guide';
    }

    caption.textContent = root.lang === 'da' ? 'Tema' : 'Theme';
    follow.textContent = root.lang === 'da' ? 'F\u00f8lg MediaHub' : 'Follow MediaHub';
    inheritedName.textContent = root.dataset.themeSource === 'override' ? (root.lang === 'da' ? 'Kun denne app' : 'This app only') : (root.lang === 'da' ? 'Fra MediaHub' : 'From MediaHub');
    control.title = managed ? (root.lang === 'da' ? 'F\u00f8lg MediaHub eller v\u00e6lg et tema kun til denne app.' : 'Follow MediaHub or choose a theme for this app only.') : '';
  }
  function applyIdentity(identity) {
    if (savingOverride) return;
    const wasManaged = managed;
    managed = identity?.managed === true;
    select.disabled = managed && !overrideHandler; select.hidden = false; follow.hidden = !managed; inheritedName.hidden = !managed;
    if (!managed) {
      hubIdentity = null;
      if (wasManaged) { let local = initial; try { const stored = localStorage.getItem(storageKey); if (valid(stored)) local = stored; } catch {} apply(local); }
      label(); return;
    }
    hubIdentity = identity;
    const engine = globalThis.MediaHubAppearance;
    const inherited = engine.readAppearance(identity.appearance);
    const override = valid(identity.themeOverride) ? identity.themeOverride : null;
    const preset = override && engine.themePresets.find(item => item.id === (override === 'light' ? 'midnight-glass' : override));
    const value = preset ? {...preset.value, mode: override === 'light' ? 'light' : inherited?.mode || 'inherit', density: inherited?.density || 'comfortable', reducedMotion: inherited?.reducedMotion ?? false} : inherited;
    const base = ['dark','light','system'].includes(identity.theme) ? identity.theme : 'dark';
    const mode = value?.mode && value.mode !== 'inherit' ? value.mode : base;
    const dark = mode === 'dark' || (mode === 'system' && !systemMode.matches);
    managedKeys.forEach(key => root.style.removeProperty(key));
    root.dataset.themeSource = override ? 'override' : 'mediahub';
    select.value = override || 'follow-hub';
    root.dataset.look = value ? value.style : 'midnight-glass';
    root.dataset.appearanceMode = dark ? 'dark' : 'light';
    root.dataset.density = value?.density || 'comfortable';
    root.dataset.reducedMotion = String(value?.reducedMotion ?? false);
    root.style.colorScheme = dark ? 'dark' : 'light';
    const tokens = value ? engine.appearanceTokens(value, dark) : Object.fromEntries(keys.map((key,index) => [key,palettes[dark ? 'midnight-glass' : 'light'][index]]));
    tokens['--healthy'] = dark ? '#6ed9a6' : '#087952'; tokens['--danger'] = dark ? '#fa7c87' : '#bb3549';
    for (const [key,color] of Object.entries(tokens)) root.style.setProperty(key,color);
    managedKeys = Object.keys(tokens);
    label();
  }
  systemMode.addEventListener('change', () => { if (hubIdentity) applyIdentity(hubIdentity); });
  new MutationObserver(label).observe(root, { attributes: true, attributeFilter: ['lang'] });
  apply(initial);
  label();
  const message = document.querySelector('#message');
  if (message) {
    const dismiss = document.createElement('button'); dismiss.type = 'button'; dismiss.className = 'notification-dismiss';
    dismiss.textContent = '×'; dismiss.setAttribute('aria-label', 'Dismiss notification'); dismiss.hidden = !message.textContent;
    dismiss.addEventListener('click', () => { message.textContent = ''; });
    message.parentElement.append(dismiss);
    new MutationObserver(() => { dismiss.hidden = !message.textContent; }).observe(message, { childList: true, characterData: true, subtree: true });
  }
  globalThis.MediaAppTheme = { apply, applyIdentity, setOverrideHandler(handler) { overrideHandler = handler; if (hubIdentity) applyIdentity(hubIdentity); } };
})();
