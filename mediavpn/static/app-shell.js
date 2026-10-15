/* Shared app shell. Account navigation inherits the Hub; pages remain app-owned. */
(() => {
  const root = document.documentElement, header = document.querySelector('.topbar');
  const nav = document.querySelector('#workspace > nav, #workspace .vpn-tabs');
  const settings = document.querySelector('#app-settings');
  if (!header || !nav || !settings) return;
  root.dataset.appKind = nav.querySelector('[data-view]') ? 'seed' : 'vpn';
  nav.classList.add('app-navigation');
  const paths = {
    downloads:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
    feeds:'<path d="M4 4a16 16 0 0 1 16 16M4 11a9 9 0 0 1 9 9"/><circle cx="5" cy="19" r="1"/>',
    'ratio-scout':'<path d="m3 17 6-6 4 4 8-10m-6 0h6v6"/>',
    trackers:'<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
    vpn:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    connection:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    apps:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    cloudflare:'<path d="M6 18a4 4 0 0 1-1-8 7 7 0 0 1 13 1 3.5 3.5 0 0 1 0 7Z"/>'
  };
  const icon = path => {const holder=document.createElement('span');holder.className='app-nav-icon';holder.setAttribute('aria-hidden','true');holder.innerHTML=`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;return holder;};
  for(const button of nav.querySelectorAll('button')) {
    const label=document.createElement('span');label.className='app-nav-label';while(button.firstChild)label.append(button.firstChild);
    button.append(icon(paths[button.dataset.view||button.dataset.vpnTab]||paths.apps),label);
  }
  const back=document.createElement('a');back.className='app-hub-back';back.hidden=true;
  const backLabel=document.createElement('span');back.append(icon('<path d="m10 6-6 6 6 6M4 12h16"/>'),backLabel);header.insertBefore(back,header.querySelector('#logout'));

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
    backLabel.textContent=text('Back to MediaHub');back.title=text('Back to MediaHub');back.setAttribute('aria-label',back.title);
    let hubUrl=null;try{const url=new URL(identity?.hubHomeUrl);if(identity?.managed&&url.protocol==='https:'&&!url.username&&!url.password&&!url.search&&!url.hash&&url.pathname==='/')hubUrl=url.href;}catch{}
    back.hidden=!hubUrl||document.querySelector('#workspace').hidden;if(hubUrl)back.href=hubUrl;else back.removeAttribute('href');
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
  const visibility=()=>{nav.hidden=workspace.hidden;toggle.hidden=workspace.hidden;back.hidden=workspace.hidden||!back.hasAttribute('href');};
  new MutationObserver(visibility).observe(workspace,{attributes:true,attributeFilter:['hidden']});
  apply(); visibility();
})();
