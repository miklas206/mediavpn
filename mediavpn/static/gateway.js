// Guided app domains; all host addresses and privileged Cloudflare calls stay server-side.
globalThis.MediaVPNGateway = (() => {
  const t = (...args) => globalThis.MediaAppI18n.t(...args);
  let data, shared, profiles = [], step = 0, initialized = false, saving = false, editingCredentials = false, tab = "connection", editingSetup = false, refreshing = false;
  let draft = {profileId: '', accountId: '', token: '', routes: []};
  const canActivate = () => !saving && !data?.busy && data?.configured && profiles.some(profile => profile.id === draft.profileId && profile.port_forwarding)
    && draft.routes.length > 0 && draft.routes.every(route => route.packageId && route.zoneId && route.label.trim())
    && !(data?.gateway?.prerequisites || []).some(check => check.state === 'blocked');
  const element = (tag, text, cls) => {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (cls) value.className = cls;
    return value;
  };
  const field = (title, input) => { const label = element('label', t(title)); label.append(input); return label; };
  function input(value, change, type = 'text') {
    const item = element('input'); item.type = type; item.value = value || '';
    item.oninput = () => { change(item.value); const activate = document.querySelector('#gateway-activate'); if (activate) activate.disabled = !canActivate(); };
    return item;
  }
  function button(title, handler, primary = false) {
    const item = element('button', t(title), primary ? 'primary' : ''); item.type = 'button'; item.disabled = saving;
    item.onclick = async () => {
      if (saving) return;
      saving = true; item.disabled = true;
      try { await handler(); } catch (error) { notice(error.message); }
      finally { saving = false; render(); }
    };
    return item;
  }
  function choices(items, value, change, label) {
    const select = element('select'); select.setAttribute('aria-label', t(label));
    select.append(Object.assign(element('option', t('Choose')), {value: ''}));
    for (const row of items) select.append(Object.assign(element('option', row.name), {value: row.id}));
    select.value = value || ''; select.onchange = () => { change(select.value); render(); };
    return select;
  }
  function summary(panel) {
    const report = element('div', undefined, 'gateway-status'); report.setAttribute('role', 'status');
    report.append(element('strong', t(data?.gateway?.providerReady ? 'Proton connection verified' : 'Proton connection not verified')));
    if (data?.gateway?.message) report.append(element('p', t(data.gateway.message), 'muted'));
    if (data?.gateway?.publicIp) report.append(element('p', data.gateway.publicIp));
    const details = element('details'); details.append(element('summary', t('Show checks and technical details')));
    const seen = new Set();
    for (const check of [...(data?.checks || []), ...(data?.issues || [])]) {
      if (!check.message || seen.has(check.message)) continue;
      seen.add(check.message); const line = element('p', t(check.message), 'muted');
      (check.state === 'blocked' || (data?.issues || []).includes(check) ? report : details).append(line);
    }
    report.append(details);
    for (const route of data?.routes || []) {
      const row = element('div', undefined, 'gateway-route-status');
      row.append(element('strong', route.hostname || route.label), element('span', t(route.httpsVerified ? 'Public HTTPS verified' : route.message || 'Waiting for gateway verification')));
      details.append(row);
    }
    panel.append(report);
  }
  function routeRows(panel) {
    panel.append(element('p', t('Choose installed apps and their public addresses. Private addresses, certificates, DNS and ports are configured automatically.'), 'muted'));
    draft.routes.forEach((route, index) => {
      const row = element('div', undefined, 'gateway-route');
      row.append(field('Installed app', choices((data.apps || []).map(app => ({id: app.packageId, name: app.name})), route.packageId, value => { route.packageId = value; }, 'Installed app')));
      row.append(field('Domain', choices(data.zones || [], route.zoneId, value => { route.zoneId = value; }, 'Domain')));
      const name = input(route.label, value => { route.label = value; }); name.maxLength = 100;
      row.append(field('Subdomain', name));
      const zone = (data.zones || []).find(item => item.id === route.zoneId);
      row.append(element('small', (route.label || 'app') + '.' + (zone?.name || '…')));
      row.append(button('Remove domain from this setup', async () => { draft.routes.splice(index, 1); }));
      panel.append(row);
    });
    const add = button('Add app domain', async () => {
      const app = (data.apps || []).find(item => !draft.routes.some(row => row.packageId === item.packageId)) || data.apps?.[0];
      draft.routes.push({packageId: app?.packageId || '', zoneId: data.zones?.[0]?.id || '', label: ''});
    });
    add.disabled = saving || !data.apps?.length || !data.zones?.length || draft.routes.length >= 32;
    panel.append(add);
    if (!data.apps?.length) panel.append(element('p', t('Install an app in MediaHub before adding its domain.'), 'muted'));
  }
  function outgoing(panel) {
    if (!shared) return;
    const details = element('details'); details.append(element('summary', t('Apps using this VPN for internet access')));
    details.append(element('p', t('Web domains and outbound app traffic share the selected Proton connection. Apps selected here remain blocked if that connection fails.'), 'muted'));
    for (const app of shared.apps || []) {
      const toggle = element('input'); toggle.type = 'checkbox'; toggle.checked = app.enabled === true;
      toggle.disabled = saving || app.enabled === null || (!app.enabled && !shared.gateway?.ready);
      toggle.onchange = async () => {
        if (saving) return;
        saving = true; toggle.disabled = true;
        try { shared = await api('hub-apps/' + app.id, {enabled: toggle.checked}, 'PUT'); }
        catch (error) { notice(error.message); }
        finally { saving = false; render(); }
      };
      const label = element('label'); label.append(toggle, element('span', app.name + ' · ' + t(app.ready ? 'Connected' : 'Not connected'))); details.append(label);
      if (app.message) details.append(element('p', t(app.message), 'muted'));
    }
    panel.append(details);
  }
  function plexPort(panel) {
    const state = data?.plexPort || {available: false, enabled: false, ready: false};
    const card = element('section', undefined, 'plex-port-card'); card.append(element('h3', t('Plex remote access')));
    const toggle = element('input'); toggle.type = 'checkbox'; toggle.checked = state.enabled;
    toggle.disabled = saving || (!state.enabled && (!state.available || !data?.enabled));
    toggle.onchange = async () => {
      if (saving) return;
      saving = true;
      try { data = await api('hub-web-gateway/plex-port', {enabled: toggle.checked}, 'PUT'); notice(t('Plex port configuration saved'), 'success'); }
      catch (error) { notice(error.message); } finally { saving = false; render(); }
    };
    const label = element('label'); label.append(toggle, element('span', t('Use a separate Proton port for Plex'))); card.append(label);
    card.append(element('p', t(state.message || (state.ready ? 'Plex remote access verified through Proton' : state.enabled ? 'Waiting for a separate verified Plex TCP port' : 'Connect Plex to the shared VPN before enabling its separate port')), 'muted'));
    if (!state.available) card.append(element('p', t('Install Plex through MediaHub before enabling its separate port'), 'muted'));
    if (state.publicIp && state.publicPort) card.append(element('strong', state.publicIp + ':' + state.publicPort));
    panel.append(card);
  }
  function render() {
    let panel = document.querySelector('#web-gateway');
    if (!panel) {
      panel = element('section', undefined, 'secondary gateway-wizard'); panel.id = 'web-gateway';
      document.querySelector('#panel-apps').append(panel);
    }
    document.querySelector(tab === 'cloudflare' ? '#panel-cloudflare' : '#panel-apps').append(panel);
    document.querySelectorAll('.gateway-loading').forEach(item => item.remove());
    panel.replaceChildren(element('h2', t(tab === 'cloudflare' ? 'Cloudflare access' : 'Apps and domains')));
    panel.append(element('p', t('One guided setup for Proton, Cloudflare and your installed apps.'), 'muted'));
    if (!data) { panel.append(element('p', t('Open MediaVPN through MediaHub to configure app domains.'))); return; }
    const titles = ['Requirements', 'Proton profile', 'Cloudflare access', 'Apps and domains'];
    const nav = element('nav', undefined, 'gateway-steps'); nav.setAttribute('aria-label', t('Gateway setup'));
    const showGuide = !data.enabled || editingSetup;
    const displayStep = tab === 'cloudflare' ? 2 : step;
    titles.forEach((title, index) => {
      const link = button((index + 1) + '. ' + t(title), async () => { step = index; });
      link.setAttribute('aria-current', index === step ? 'step' : 'false'); nav.append(link);
    });
    if (tab !== 'cloudflare' && showGuide) panel.append(nav, element('h3', t(titles[step])));
    if (tab !== 'cloudflare' && !showGuide) {
      for (const route of data.routes || []) { const row = element('article', undefined, 'compact-route'); row.append(element('strong', route.hostname), element('span', t(route.httpsVerified ? 'HTTPS verified' : ['blocked','conflict'].includes(route.status) ? 'Needs attention' : 'Checking...'), 'muted')); panel.append(row); }
      panel.append(button('Edit gateway setup', async () => { editingSetup = true; step = 3; }));
    } else if (displayStep === 0) {
      const list = element('ul');
      for (const line of [
        'Install MediaVPN and the apps you want to publish through MediaHub. Only installed apps appear in this guide.',
        'Use a paid Proton plan and a P2P WireGuard profile created with NAT-PMP enabled. Moderate NAT must be disabled.',
        'Use a Cloudflare-managed domain. The API token needs Zone Read, DNS Edit, Origin Rules Edit and Zone Settings Read for the selected zones.',
        'Set Cloudflare SSL/TLS to Full (strict). MediaVPN obtains and renews certificates automatically.',
        'The host needs Docker and TUN access. No GPU is required. The installer prepares supported Proxmox hosts and reports missing requirements.',
      ]) list.append(element('li', t(line)));
      panel.append(list);
      const help = element('a', t('Open Proton configuration guide')); help.href = 'https://protonvpn.com/support/port-forwarding-manual-setup'; help.target = '_blank'; help.rel = 'noopener noreferrer'; panel.append(help);
    } else if (displayStep === 1) {
      panel.append(field('VPN profile', choices(profiles, draft.profileId, value => { draft.profileId = value; }, 'VPN profile')));
      const selected = profiles.find(profile => profile.id === draft.profileId);
      panel.append(element('p', t(selected?.port_forwarding ? 'NAT-PMP is enabled for this profile.' : 'Choose a profile uploaded with Proton NAT-PMP enabled.'), 'muted'));
      panel.append(button('Upload Proton configuration', async () => document.querySelector('#profile-dialog').showModal()));
      panel.append(element('p', t('Keep the server from your Proton configuration initially. Other VPN profiles are stopped when the shared gateway starts.'), 'muted'));
    } else if (displayStep === 2) {
      panel.append(element('p', t(data.configured ? 'Cloudflare is connected. Select your app domains in the next step.' : 'Connect your Cloudflare account once. Your token is stored privately and is never shown again.'), 'muted'));
      if (data.configured && !editingCredentials) {
        const replace = button('Replace Cloudflare token', async () => { editingCredentials = true; });
        replace.disabled = saving || data.enabled || data.busy;
        panel.append(replace);
        if (data.enabled) panel.append(element('p', t('Disable public domains before replacing the Cloudflare token.'), 'muted'));
      }
      if (!data.configured || editingCredentials) {
        const form = element('form', undefined, 'gateway-credentials');
        const account = input(draft.accountId, value => { draft.accountId = value; }); account.required = true; account.maxLength = 32; account.pattern = '[a-fA-F0-9]{32}';
        const token = input(draft.token, value => { draft.token = value; }, 'password'); token.required = true; token.autocomplete = 'new-password';
        form.append(field('Cloudflare account ID', account), field('Cloudflare API token', token));
        const save = element('button', t('Connect Cloudflare'), 'primary'); save.type = 'submit'; save.disabled = saving;
        form.append(save); form.onsubmit = async event => {
          event.preventDefault(); if (saving) return; saving = true; save.disabled = true;
          try { data = await api('hub-web-gateway/credentials', {accountId: draft.accountId.trim(), token: draft.token.trim()}, 'PUT'); draft.token = ''; editingCredentials = false; notice(t('Cloudflare account connected'), 'success'); }
          catch (error) { notice(error.message); } finally { saving = false; render(); }
        };
        panel.append(form);
      }
      const help = element('a', t('Create a scoped Cloudflare API token')); help.href = 'https://dash.cloudflare.com/profile/api-tokens'; help.target = '_blank'; help.rel = 'noopener noreferrer'; panel.append(help);
      panel.append(element('p', t('Existing records and tunnels are preserved. A conflicting hostname is blocked until you choose a free name or explicitly migrate the old setup.'), 'muted'));
    } else {
      routeRows(panel);
      const selected = profiles.find(profile => profile.id === draft.profileId);
      const activate = button('Activate gateway and app domains', async () => {
        data = await api('hub-web-gateway/config', {enabled: true, profileId: draft.profileId, routes: draft.routes}, 'PUT');
        draft.routes = data.routes.map(route => ({id: route.id, packageId: route.packageId, zoneId: route.zoneId, label: route.label}));
        editingSetup = false; notice(t('Gateway setup saved. DNS, certificates and public HTTPS are being checked.'), 'success');
      }, true);
      activate.id = 'gateway-activate'; activate.disabled = !canActivate();
      panel.append(activate);
      if (!selected?.port_forwarding) panel.append(element('p', t('Complete the Proton profile step with NAT-PMP enabled before activation.'), 'muted'));
    }
    const actions = element('div', undefined, 'actions');
    if (tab !== 'cloudflare' && showGuide && step) actions.append(button('Back', async () => { step--; }));
    if (tab !== 'cloudflare' && showGuide && step < 3) actions.append(button('Next', async () => { step++; }, true));
    actions.append(button('Refresh gateway status', async () => { await api('hub-web-gateway/reconcile', {}); await refresh(profiles, true); }));
    if (data.enabled) actions.append(button('Disable public domains', async () => {
      data = await api('hub-web-gateway/config', {enabled: false, profileId: data.profileId, routes: []}, 'PUT');
      draft.routes = []; notice(t('Public domains disabled. Check gateway status for cleanup progress.'), 'success');
    }));
    if (tab !== 'cloudflare') { panel.append(actions); summary(panel); outgoing(panel); plexPort(panel); }
  }
  async function refresh(currentProfiles, force = false) {
    if (appRole !== 'administrator' || refreshing) return;
    profiles = currentProfiles || profiles;
    const focused = document.activeElement?.closest?.('#web-gateway');
    if ((saving && !force) || (focused && ['INPUT', 'SELECT'].includes(document.activeElement.tagName) && !force)) return;
    refreshing = true;
    try {
      const response = await Promise.all([api('hub-web-gateway'), api('hub-apps').catch(() => null)]);
      data = response[0]; shared = response[1];
      if (!Array.isArray(data.apps) || !Array.isArray(data.routes)) throw new Error(t('Update MediaHub and MediaVPN to use the guided gateway.'));
      if (!initialized) {
        draft = {profileId: data.profileId || shared?.gateway?.profileId || (profiles.length === 1 ? profiles[0].id : ''), accountId: data.accountId || '', token: '', routes: data.routes.map(route => ({id: route.id, packageId: route.packageId, zoneId: route.zoneId, label: route.label}))};
        step = data.enabled ? 3 : 0; initialized = true;
      }
      if (!draft.profileId && profiles.length === 1) draft.profileId = profiles[0].id;
      document.querySelector('#shared-apps')?.remove();
      render();
    } catch (error) {
      notice(error.message);
      if (!data) render();
    } finally { refreshing = false; }
  }
  return {refresh, setTab(value) { tab = value; if (data) render(); }};
})();
