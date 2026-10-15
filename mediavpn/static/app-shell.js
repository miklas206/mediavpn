/* Shared app shell. Account navigation inherits the Hub; pages remain app-owned. */
(() => {
  const root = document.documentElement, header = document.querySelector('.topbar');
  const nav = document.querySelector('#workspace > nav, #workspace .vpn-tabs');
  const settings = document.querySelector('#app-settings');
  if (!header || !nav || !settings) return;
  root.dataset.appKind = nav.querySelector('[data-view]') ? 'seed' : 'vpn';
  nav.classList.add('app-navigation');
  const placeholder = document.createComment('app navigation'); nav.before(placeholder);
  const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'app-navigation-toggle';
  toggle.textContent = '☰'; toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls','app-navigation');
  nav.id = 'app-navigation'; header.insertBefore(toggle, header.firstChild);
  const control = document.createElement('label'); control.className = 'app-navigation-setting';
  const caption = document.createElement('span'), select = document.createElement('select');
  const choices = [['follow-hub','Follow MediaHub'],['sidebar','Sidebar'],['topbar','Top navigation'],['dock-top','Top dock'],['dock-bottom','Bottom dock'],['launcher','Compact menu']];
  for (const [value,label] of choices) {const option = new Option(label,value);option.dataset.label=label;select.append(option);}
  control.append(caption,select); settings.querySelector('.app-settings-panel').append(control);
  const key = nav.querySelector('[data-view]') ? 'mediaseed.navigation.v1' : 'mediavpn.navigation.v1';
  let identity = globalThis.MediaAppIdentity || null, local = null, saving = false;
  try {local = localStorage.getItem(key);} catch {}
  const valid = value => choices.some(([id]) => id === value && id !== 'follow-hub');
  const text = value => globalThis.MediaAppI18n?.t(value) || value;
  function close() {root.dataset.navigationOpen='false';toggle.setAttribute('aria-expanded','false');}
  function apply() {
    const override = identity?.managed ? identity.navigationOverride : local;
    const value = valid(override) ? override : valid(identity?.appearance?.navigation) ? identity.appearance.navigation : 'topbar';
    root.dataset.appNavigation = value;
    select.value = valid(override) ? override : 'follow-hub';
    select.disabled = saving;
    caption.textContent = text('Navigation layout'); toggle.setAttribute('aria-label',text('Open navigation'));
    for (const option of select.options) option.textContent = text(option.dataset.label);
    if (value === 'topbar') header.insertBefore(nav, header.querySelector('.app-settings-button'));
    else placeholder.parentNode.insertBefore(nav,placeholder.nextSibling);
    close();
  }
  toggle.onclick = () => {const open=root.dataset.navigationOpen!=='true';root.dataset.navigationOpen=String(open);toggle.setAttribute('aria-expanded',String(open));};
  nav.addEventListener('click', event => {if(event.target.closest('button')) close();});
  document.addEventListener('keydown', event => {if(event.key==='Escape') close();});
  select.onchange = async () => {
    const navigationOverride = valid(select.value) ? select.value : null;
    saving = true; select.disabled = true;
    try {
      if (identity?.managed) {const saved = await api('auth/navigation',{navigationOverride},'PUT');identity={...identity,...saved};}
      else {local=navigationOverride; if(local)localStorage.setItem(key,local);else localStorage.removeItem(key);}
    } catch(error) {window.dispatchEvent(new CustomEvent('mediahub-theme-error',{detail:error.message}));}
    finally {saving=false;apply();}
  };
  window.addEventListener('mediahub-identity', event => {if(!saving){identity=event.detail;apply();}});
  new MutationObserver(apply).observe(root,{attributes:true,attributeFilter:['lang']});
  const workspace=document.querySelector('#workspace');
  const visibility=()=>{nav.hidden=workspace.hidden;toggle.hidden=workspace.hidden;};
  new MutationObserver(visibility).observe(workspace,{attributes:true,attributeFilter:['hidden']});
  apply(); visibility();
})();
