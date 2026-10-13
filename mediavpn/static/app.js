const {t, setLanguage, locale} = globalThis.MediaAppI18n;
let csrf = '';
let needsSetup = false;
let appRole = 'administrator';
let busy = false;
let noticeTimer;
const message = document.querySelector('#message');
const dialog = document.querySelector('#profile-dialog');
function scheduleNotice() { clearTimeout(noticeTimer); if (message.textContent && !message.matches(':hover') && document.activeElement !== message) noticeTimer = setTimeout(() => { message.textContent = ''; }, 6000); }
function notice(text, tone = 'danger') { message.textContent = t(text); message.dataset.tone = tone; scheduleNotice(); }
message.addEventListener('mouseenter', () => clearTimeout(noticeTimer));
message.addEventListener('focus', () => clearTimeout(noticeTimer));
message.addEventListener('mouseleave', scheduleNotice);
message.addEventListener('blur', scheduleNotice);
async function api(path, body, method) {
  const response = await fetch('/api/' + path, {method:method || (body ? 'POST' : 'GET'), headers:{'Content-Type':'application/json','X-MediaVPN-CSRF':csrf}, ...(body ? {body:JSON.stringify(body)} : {})});
  const result = await response.json();
  if (!response.ok) { const error = new Error(typeof result.detail === 'string' ? result.detail : t("The action could not be completed.")); error.status = response.status; throw error; }
  return result;
}
globalThis.MediaAppTheme.setOverrideHandler(themeOverride => api('auth/preferences', {themeOverride}, 'PUT'));
window.addEventListener('mediahub-theme-error', event => notice(event.detail));
function node(tag, text, className) { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element; }
async function action(button, handler) { if (busy) return; busy = true; button.disabled = true; try { await handler(); } catch(error) { notice(error.message); } finally { busy = false; button.disabled = false; } }
function control(text, handler, className) { const button = node('button',text,className); button.type = 'button'; button.onclick = () => action(button,handler); return button; }
const statusNames = {disabled:t("Runtime not enabled"),stopped:t("Stopped"),connected:t("Connected"),unverified:t("Unverified"),starting:t("Starting"),unavailable:t("Runtime unavailable")};
function countryCode(country) {
  if (typeof country !== 'string' || !country.trim()) return null;
  const name = country.trim();
  if (/^[A-Za-z]{2}$/.test(name)) {
    try { return new Intl.Locale('und-' + name.toUpperCase()).region; } catch { return name.toUpperCase(); }
  }
  const aliases = {
    'bosnia and herzegovina': 'BA', 'brunei darussalam': 'BN', "cote d'ivoire": 'CI',
    'czech republic': 'CZ', 'democratic republic of the congo': 'CD', 'hong kong': 'HK',
    'korea': 'KR', "lao people's democratic republic": 'LA', 'macao': 'MO',
    'palestine, state of': 'PS', 'russian federation': 'RU', 'syrian arab republic': 'SY', 'turkey': 'TR',
  };
  const normalized = name.toLocaleLowerCase('en');
  if (aliases[normalized]) return aliases[normalized];
  try {
    if (!countryCode.names) {
      const names = new Map();
      const english = new Intl.DisplayNames(['en'], {type: 'region'});
      const danish = new Intl.DisplayNames(['da'], {type: 'region'});
      for (let first = 65; first <= 90; first++) for (let second = 65; second <= 90; second++) {
        const code = String.fromCharCode(first, second);
        // Historical aliases (DD, CS, VD, YD, RH) can share modern names.
        // Only map canonical codes to the actual bundled flag filenames.
        if (new Intl.Locale('und-' + code).region !== code) continue;
        for (const display of [english.of(code), danish.of(code)]) {
          if (display !== code) names.set(display.toLocaleLowerCase('en'), code);
        }
      }
      countryCode.names = names;
    }
    return countryCode.names.get(normalized) || null;
  } catch { return null; }
}
function countryFlag(country) {
  const code = countryCode(country);
  if (!code) return null;
  const flag = node('img', undefined, 'country-flag');
  flag.src = `/static/flags/${code.toLowerCase()}.svg`;
  flag.alt = ''; flag.width = 20; flag.height = 15;
  flag.onerror = () => { flag.hidden = true; };
  return flag;
}
function countryLabel(country) {
  if (!country) return t("Country unknown");
  const code = countryCode(country);
  try { if (code) return new Intl.DisplayNames([locale()], {type: 'region'}).of(code); } catch {}
  return country;
}
let serverProfile;
let serverDialogCycle = 0;
let serverMeasurementCycle = 0;
const serverDialog = document.querySelector('#server-dialog');
function setServerChoice(value) {
  document.querySelector('#server-country').value = value;
  document.querySelector('#server-selected').textContent = value === '*' ? t("Selected: Automatic · all countries") : value ? t("Selected: {0}", {0: countryLabel(value)}) : t("Selected: Server from my configuration");
  for (const button of document.querySelectorAll('#server-dialog [data-server-choice]')) button.setAttribute('aria-pressed', String(button.dataset.serverChoice === value));
}
function serverChoice(text, value) {
  const button = node('button', text);
  button.type = 'button'; button.dataset.serverChoice = value;
  button.onclick = () => setServerChoice(value);
  return button;
}
async function chooseServer(profile) {
  const cycle = ++serverDialogCycle;
  serverProfile = profile.id;
  document.querySelector('#server-choices').replaceChildren(serverChoice(t("Automatic · all countries"), '*'), serverChoice(t("Use the server from my configuration"), ''));
  const countries = document.querySelector('#server-countries'); countries.replaceChildren();
  document.querySelector('#server-country-search').value = '';
  setServerChoice(profile.auto_country || '');
  document.querySelector('#server-recommendations').replaceChildren();
  document.querySelector('#server-measurement').textContent = t("Measuring recommended countries from your server…");
  document.querySelector('#save-server').disabled = true;
  serverDialog.showModal();
  try {
    const result = await api('proton/countries');
    if (cycle !== serverDialogCycle || !serverDialog.open) return;
    for (const country of result.countries) {
      const button = serverChoice(countryLabel(country), country);
      const flag = countryFlag(country); if (flag) button.append(flag);
      countries.append(button);
    }
    setServerChoice(document.querySelector('#server-country').value);
  } catch (error) { notice(error.message); }
  finally { document.querySelector('#save-server').disabled = false; }
  void loadRecommendations(cycle);
}
async function loadRecommendations(cycle, force = false) {
  const measurement = ++serverMeasurementCycle;
  try {
    const result = force ? await api('proton/recommendations/refresh', {}) : await api('proton/recommendations');
    if (measurement !== serverMeasurementCycle || cycle !== serverDialogCycle || !serverDialog.open) return;
    document.querySelector('#server-measurement').textContent = t("Sample: {0} measured candidates{1} · {2} · {3}. The measurement may be up to one minute old.", {0: result.tested, 1: result.totalCountries ? t(" · responses from {0} of {1} countries", {0: result.recommendations.length, 1: result.totalCountries}) : '', 2: new Date(result.measuredAt * 1000).toLocaleTimeString(locale()), 3: result.loadAvailable ? t("lowest load first, then latency") : t("recommended by measured latency")});
    if (!result.loadAvailable) document.querySelector('#server-measurement').textContent += result.loadStatus === 'authorization_required' ? t(" Proton requires API authentication for load data. The WireGuard configuration does not grant API access; automatic selection therefore uses latency only.") : t(" Load data could not be retrieved; automatic selection therefore uses measured latency only.");
    const table = node('table');
    const head = node('thead'); const heading = node('tr'); heading.append(node('th', t("Country")), node('th', t("Load")), node('th', t("Latency")), node('th', t("Measured server"))); head.append(heading);
    const body = node('tbody');
    for (const row of result.recommendations.slice(0, 6)) {
      const line = node('tr'); const country = node('td');
      const choose = serverChoice(countryLabel(row.country), row.country);
      const flag = countryFlag(row.country); if (flag) choose.append(flag);
      country.append(choose); line.append(country, node('td', row.loadPercent == null ? t("Unknown") : `${row.loadPercent} %`), node('td', `${row.latencyMs} ms`), node('td', row.server)); body.append(line);
    }
    table.append(head, body); document.querySelector('#server-recommendations').replaceChildren(table);
    setServerChoice(document.querySelector('#server-country').value);
  } catch (error) {
    if (measurement === serverMeasurementCycle && cycle === serverDialogCycle && serverDialog.open) {
      document.querySelector('#server-measurement').textContent = t("Recommendations could not be measured. You can still choose a country; latency is measured at startup.");
      notice(error.status === 401 ? t("Your login session expired. Open MediaVPN again from MediaHub.") : error.message || t("The MediaVPN connection was interrupted. Try refreshing recommendations again."));
    }
  }
}
document.querySelector('#server-country-search').oninput = event => {
  const query = event.target.value.trim().toLocaleLowerCase(locale());
  for (const button of document.querySelectorAll('#server-countries button')) button.hidden = !`${button.textContent} ${button.dataset.serverChoice}`.toLocaleLowerCase(locale()).includes(query);
};
document.querySelector('#refresh-servers').onclick = event => action(event.currentTarget, async () => { document.querySelector('#server-measurement').textContent = t("Fetching fresh data and measuring servers…"); await loadRecommendations(serverDialogCycle, true); });
document.querySelector('#close-server').onclick = () => serverDialog.close();
document.querySelector('#server-form').onsubmit = async event => {
  event.preventDefault();
  await action(document.querySelector('#save-server'), async () => {

    const result = await api(`profiles/${serverProfile}/server-selection`, {country: document.querySelector('#server-country').value || null}, 'PUT');
    serverDialog.close(); await refresh(); notice(result.reconnecting ? t("Server selection saved. VPN reconnects automatically; connected apps await the newly verified port.") : t("Server selection saved. The profile stays stopped until you press Start."), 'success');
  });
};
async function refresh() {
  const [runtime, profiles] = await Promise.all([api('runtime'),api('profiles')]);
  document.querySelector('#runtime-state').textContent = runtime.enabled ? t("VPN runtime enabled · status checked now") : t("Preview · tunnel startup is blocked. No traffic uses the VPN yet.");
  document.querySelector('#seedbox-state').textContent = runtime.mediaSeedConnected ? t("Connected through verified VPN") : t(runtime.seedbox?.reason) || t("Not connected");
  const target = document.querySelector('#profiles'); target.replaceChildren();
  if (!profiles.length) {
    const empty = node('div', undefined, 'empty');
    empty.append(node('div', '◇', 'empty-orbit'), node('h2', t("No connections yet.")), node('p', t("Create your first VPN connection."), 'muted'));
    if (appRole !== 'viewer') empty.append(control(t("New connection"), () => dialog.showModal(), 'primary'));
    target.append(empty);
  }
  for (const profile of profiles) {
    const item = node('article',undefined,'profile'); const details = node('div');
    details.append(node('h2',profile.name),node('span',t(statusNames[profile.state]) || t("Unknown"),'status ' + (profile.healthy ? 'connected' : '')),node('p',`WireGuard · ${profile.endpoint}:${profile.port}`),node('p',t("Public IP: {0} · VPN port: {1}", {0: profile.publicIp || t("Unverified"), 1: profile.forwardedPort || t("Unverified")})));
    const controls = node('div',undefined,'actions');
    const country = node('p', t("Country: {0}", {0: countryLabel(profile.country)}), 'country-label');
    const flag = countryFlag(profile.country); if (flag) country.append(flag);
    details.append(country);
    details.append(node('p', profile.auto_country ? t("Automatic server selection · {0}{1}", {0: profile.auto_country === '*' ? t("across countries") : profile.auto_country, 1: profile.selectedServer ? ' · ' + profile.selectedServer + ' · ' + profile.latencyMs + t(" ms at selection") : t(" · selected at startup")}) : t("Server from your configuration")));
    controls.append(control(t("Server selection"), () => chooseServer(profile)));
    const start = control('Start',async()=>{await api(`profiles/${profile.id}/start`,{}); await refresh(); notice(t("Start requested. Checking the connection."),'success');},'primary'); start.disabled = !runtime.enabled;
    const stop = control('Stop',async()=>{await api(`profiles/${profile.id}/stop`,{}); await refresh(); notice(t("The connection has stopped."),'success');}); stop.disabled = !runtime.enabled;
    const forwarding = node('label'); const toggle = node('input'); toggle.type = 'checkbox'; toggle.checked = profile.port_forwarding === true; toggle.disabled = !runtime.enabled;
    forwarding.append(toggle, node('span', t("Proton port forwarding (NAT-PMP)")));
    toggle.onchange = () => action(toggle, async () => { if (!confirm(t("This stops the tunnel. Only enable it with a Proton profile created with NAT-PMP. Start the profile again after changing it."))) { toggle.checked = !toggle.checked; return; } await api(`profiles/${profile.id}/forwarding`, {enabled: toggle.checked}, 'PUT'); await refresh(); notice(t("Port forwarding setting saved. The profile has stopped; start it again."), 'success'); });
    forwarding.hidden = appRole !== 'administrator';
    controls.hidden = appRole !== 'administrator';
    details.append(forwarding);
    controls.append(start,stop,control(t("Remove"),async()=>{if (!confirm(t("Stop and remove {0}? The profile's private key will be deleted.", {0: profile.name}))) return; await api('profiles/'+profile.id,undefined,'DELETE'); await refresh(); notice(t("The connection has been removed."),'success');},'danger'));
    item.append(details,controls); target.append(item);
  }
}
async function workspace() { document.querySelector('#add').hidden = appRole !== 'administrator'; document.querySelector('#identity').hidden = true; document.querySelector('.topbar').hidden = false; document.querySelector('#workspace').hidden = false; try { await refresh(); } catch(error) { notice(error.message); } }
document.querySelector('#login').onsubmit = async event => { event.preventDefault(); const button = document.querySelector('#login-submit'); await action(button,async()=>{const body = Object.fromEntries(new FormData(event.target)); if (needsSetup) { await api('auth/setup',body); needsSetup = false; } csrf = (await api('auth/login',body)).csrf; const identity = await api('auth/me'); setLanguage(identity.language || 'en'); globalThis.MediaAppTheme.applyIdentity(identity); event.target.reset(); await workspace();}); };
document.querySelector('#logout').onclick = event => action(event.target,async()=>{await api('auth/logout',{}); location.reload();});
document.querySelector('#add').onclick = () => dialog.showModal();
document.querySelector('#close-dialog').onclick = () => dialog.close();
document.querySelector('#refresh').onclick = event => action(event.target,refresh);
document.querySelector('#profile-form').onsubmit = async event => {event.preventDefault(); const form = event.target; await action(form.querySelector('button[type="submit"]'),async()=>{const file = document.querySelector('#configuration').files[0]; if (!file || file.size > 16384) throw new Error(t("Choose a WireGuard file no larger than 16 KiB.")); await api('profiles',{name:form.elements.namedItem('name').value,configuration:await file.text(),port_forwarding:form.elements.namedItem('port_forwarding').checked}); form.reset(); dialog.close(); await refresh(); notice(t("The connection is saved and has not started."),'success');});};
async function initializeLogin() {
  try {
    const status = await api('auth/status'); needsSetup = status.needsSetup;
    if (status.managed) {
      document.querySelector('#login').hidden = true;
      try { const identity = await api('auth/me'); setLanguage(identity.language || 'en'); globalThis.MediaAppTheme.applyIdentity(identity); csrf = identity.csrf; appRole = identity.role; await workspace(); }
      catch (error) { if (error.status === 401) location.replace(status.loginUrl); else notice(error.message); }
      return;
    }
    document.querySelector('#identity-title').textContent = needsSetup ? t("A fresh start.") : t("Welcome back.");
    document.querySelector('#login-submit').textContent = needsSetup ? t("Create administrator") : t("Sign in");
    try { const identity = await api('auth/me'); setLanguage(identity.language || 'en'); globalThis.MediaAppTheme.applyIdentity(identity); csrf = identity.csrf; appRole = identity.role || 'administrator'; await workspace(); } catch {}
  } catch(error) { notice(error.message); }
}
void initializeLogin();
setInterval(() => { if (!document.hidden && !document.querySelector('#workspace').hidden && !busy) refresh().catch(() => {}); }, 15000);

const accountLanguage = document.querySelector('#account-language');
if (accountLanguage) { accountLanguage.onchange = () => setLanguage(accountLanguage.value); setLanguage(accountLanguage.value || 'en'); }

let refreshingPreferences = false;
async function refreshAccountPreferences() {
  if (refreshingPreferences || document.hidden || document.querySelector('#workspace').hidden) return;
  refreshingPreferences = true;
  try { const identity = await api('auth/me'); setLanguage(identity.language || 'en'); globalThis.MediaAppTheme.applyIdentity(identity); } catch {} finally { refreshingPreferences = false; }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) void refreshAccountPreferences(); });
window.addEventListener('focus', () => { void refreshAccountPreferences(); });
setInterval(() => { void refreshAccountPreferences(); }, 60000);
