let csrf = '';
let needsSetup = false;
let appRole = 'administrator';
let busy = false;
let noticeTimer;
const message = document.querySelector('#message');
const dialog = document.querySelector('#profile-dialog');
function scheduleNotice() { clearTimeout(noticeTimer); if (message.textContent && !message.matches(':hover') && document.activeElement !== message) noticeTimer = setTimeout(() => { message.textContent = ''; }, 6000); }
function notice(text, tone = 'danger') { message.textContent = text; message.dataset.tone = tone; scheduleNotice(); }
message.addEventListener('mouseenter', () => clearTimeout(noticeTimer));
message.addEventListener('focus', () => clearTimeout(noticeTimer));
message.addEventListener('mouseleave', scheduleNotice);
message.addEventListener('blur', scheduleNotice);
async function api(path, body, method) {
  const response = await fetch('/api/' + path, {method:method || (body ? 'POST' : 'GET'), headers:{'Content-Type':'application/json','X-MediaVPN-CSRF':csrf}, ...(body ? {body:JSON.stringify(body)} : {})});
  const result = await response.json();
  if (!response.ok) { const error = new Error(typeof result.detail === 'string' ? result.detail : 'Handlingen kunne ikke gennemføres.'); error.status = response.status; throw error; }
  return result;
}
function node(tag, text, className) { const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element; }
async function action(button, handler) { if (busy) return; busy = true; button.disabled = true; try { await handler(); } catch(error) { notice(error.message); } finally { busy = false; button.disabled = false; } }
function control(text, handler, className) { const button = node('button',text,className); button.type = 'button'; button.onclick = () => action(button,handler); return button; }
const statusNames = {disabled:'Runtime ikke aktiveret',stopped:'Stoppet',connected:'Forbundet',unverified:'Ikke verificeret',starting:'Starter',unavailable:'Runtime utilgængelig'};
function countryFlag(country) {
  if (!country) return null;
  let code = /^[A-Z]{2}$/.test(country) ? country : null;
  try {
    const english = new Intl.DisplayNames(['en'], {type: 'region'});
    for (let first = 65; !code && first <= 90; first++) for (let second = 65; second <= 90; second++) {
      const candidate = String.fromCharCode(first, second);
      if (english.of(candidate) === country) { code = candidate; break; }
    }
  } catch {}
  if (!code) return null;
  const flag = node('img', undefined, 'country-flag');
  flag.src = `/static/flags/${code.toLowerCase()}.svg`;
  flag.alt = ''; flag.width = 20; flag.height = 15;
  flag.onerror = () => { flag.hidden = true; };
  return flag;
}
function countryLabel(country) {
  if (!country) return 'Land ukendt';
  try {
    const english = new Intl.DisplayNames(['en'], {type: 'region'});
    const danish = new Intl.DisplayNames(['da'], {type: 'region'});
    if (/^[A-Z]{2}$/.test(country)) return danish.of(country);
    for (let first = 65; first <= 90; first++) for (let second = 65; second <= 90; second++) {
      const code = String.fromCharCode(first, second);
      if (english.of(code) === country) return danish.of(code);
    }
  } catch {}
  return country;
}
let serverProfile;
let serverDialogCycle = 0;
let serverMeasurementCycle = 0;
const serverDialog = document.querySelector('#server-dialog');
function setServerChoice(value) {
  document.querySelector('#server-country').value = value;
  document.querySelector('#server-selected').textContent = value === '*' ? 'Valgt: Automatisk · alle lande' : value ? `Valgt: ${countryLabel(value)}` : 'Valgt: Serveren fra min konfiguration';
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
  document.querySelector('#server-choices').replaceChildren(serverChoice('Automatisk · alle lande', '*'), serverChoice('Brug serveren fra min konfiguration', ''));
  const countries = document.querySelector('#server-countries'); countries.replaceChildren();
  document.querySelector('#server-country-search').value = '';
  setServerChoice(profile.auto_country || '');
  document.querySelector('#server-recommendations').replaceChildren();
  document.querySelector('#server-measurement').textContent = 'Måler anbefalede lande fra din server…';
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
    document.querySelector('#server-measurement').textContent = `Anbefalet efter ${result.tested} målte kandidater · ${new Date(result.measuredAt * 1000).toLocaleTimeString('da-DK')} · laveste belastning prioriteres, derefter svartid. Målingen kan være op til ét minut gammel.`;
    if (!result.loadAvailable) document.querySelector('#server-measurement').textContent += ' Proton udleverer ikke belastningsdata lige nu; anbefalingerne bruger kun målt svartid.';
    const table = node('table');
    const head = node('thead'); const heading = node('tr'); heading.append(node('th', 'Land'), node('th', 'Belastning'), node('th', 'Svartid'), node('th', 'Målt server')); head.append(heading);
    const body = node('tbody');
    for (const row of result.recommendations.slice(0, 6)) {
      const line = node('tr'); const country = node('td');
      const choose = serverChoice(countryLabel(row.country), row.country);
      const flag = countryFlag(row.country); if (flag) choose.append(flag);
      country.append(choose); line.append(country, node('td', row.loadPercent == null ? 'Ukendt' : `${row.loadPercent} %`), node('td', `${row.latencyMs} ms`), node('td', row.server)); body.append(line);
    }
    table.append(head, body); document.querySelector('#server-recommendations').replaceChildren(table);
    setServerChoice(document.querySelector('#server-country').value);
  } catch {
    if (measurement === serverMeasurementCycle && cycle === serverDialogCycle) document.querySelector('#server-measurement').textContent = 'Anbefalinger kunne ikke måles. Du kan stadig vælge land; svartiden måles ved start.';
  }
}
document.querySelector('#server-country-search').oninput = event => {
  const query = event.target.value.trim().toLocaleLowerCase('da-DK');
  for (const button of document.querySelectorAll('#server-countries button')) button.hidden = !`${button.textContent} ${button.dataset.serverChoice}`.toLocaleLowerCase('da-DK').includes(query);
};
document.querySelector('#refresh-servers').onclick = event => action(event.currentTarget, async () => { document.querySelector('#server-measurement').textContent = 'Henter friske data og måler serverne…'; await loadRecommendations(serverDialogCycle, true); });
document.querySelector('#close-server').onclick = () => serverDialog.close();
document.querySelector('#server-form').onsubmit = async event => {
  event.preventDefault();
  await action(document.querySelector('#save-server'), async () => {

    const result = await api(`profiles/${serverProfile}/server-selection`, {country: document.querySelector('#server-country').value || null}, 'PUT');
    serverDialog.close(); await refresh(); notice(result.reconnecting ? 'Servervalg gemt. VPN genforbindes automatisk; tilsluttede apps afventer den nye verificerede port.' : 'Servervalg gemt. Profilen forbliver stoppet, indtil du trykker Start.', 'success');
  });
};
async function refresh() {
  const [runtime, profiles] = await Promise.all([api('runtime'),api('profiles')]);
  document.querySelector('#runtime-state').textContent = runtime.enabled ? 'VPN-runtime aktiveret · status kontrolleret nu' : 'Preview · tunnelstart er blokeret. Ingen trafik føres gennem VPN endnu.';
  document.querySelector('#seedbox-state').textContent = runtime.mediaSeedConnected ? 'Forbundet gennem verificeret VPN' : runtime.seedbox?.reason || 'Ikke tilsluttet';
  const target = document.querySelector('#profiles'); target.replaceChildren();
  if (!profiles.length) target.append(node('p','Ingen forbindelser endnu.','empty'));
  for (const profile of profiles) {
    const item = node('article',undefined,'profile'); const details = node('div');
    details.append(node('h2',profile.name),node('span',statusNames[profile.state] || 'Ukendt','status ' + (profile.healthy ? 'connected' : '')),node('p',`WireGuard · ${profile.endpoint}:${profile.port}`),node('p',`Offentlig IP: ${profile.publicIp || 'Ikke verificeret'} · VPN-port: ${profile.forwardedPort || 'Ikke verificeret'}`));
    const controls = node('div',undefined,'actions');
    const country = node('p', `Land: ${countryLabel(profile.country)}`, 'country-label');
    const flag = countryFlag(profile.country); if (flag) country.append(flag);
    details.append(country);
    details.append(node('p', profile.auto_country ? `Automatisk servervalg · ${profile.auto_country === '*' ? 'på tværs af lande' : profile.auto_country}${profile.selectedServer ? ' · ' + profile.selectedServer + ' · ' + profile.latencyMs + ' ms ved valg' : ' · vælges ved start'}` : 'Server fra din konfiguration'));
    controls.append(control('Servervalg', () => chooseServer(profile)));
    const start = control('Start',async()=>{await api(`profiles/${profile.id}/start`,{}); await refresh(); notice('Start anmodet. Forbindelsen kontrolleres.','success');},'primary'); start.disabled = !runtime.enabled;
    const stop = control('Stop',async()=>{await api(`profiles/${profile.id}/stop`,{}); await refresh(); notice('Forbindelsen er stoppet.','success');}); stop.disabled = !runtime.enabled;
    const forwarding = node('label'); const toggle = node('input'); toggle.type = 'checkbox'; toggle.checked = profile.port_forwarding === true; toggle.disabled = !runtime.enabled;
    forwarding.append(toggle, node('span', 'Proton port-forwarding (NAT-PMP)'));
    toggle.onchange = () => action(toggle, async () => { if (!confirm('Dette stopper tunnelen. Aktivér kun med en Proton-profil oprettet med NAT-PMP. Start profilen igen efter ændringen.')) { toggle.checked = !toggle.checked; return; } await api(`profiles/${profile.id}/forwarding`, {enabled: toggle.checked}, 'PUT'); await refresh(); notice('Port-forwarding-valget er gemt. Profilen er stoppet; start den igen.', 'success'); });
    forwarding.hidden = appRole !== 'administrator';
    controls.hidden = appRole !== 'administrator';
    details.append(forwarding);
    controls.append(start,stop,control('Fjern',async()=>{if (!confirm(`Stop og fjern ${profile.name}? Profilens private nøgle slettes.`)) return; await api('profiles/'+profile.id,undefined,'DELETE'); await refresh(); notice('Forbindelsen er fjernet.','success');},'danger'));
    item.append(details,controls); target.append(item);
  }
}
async function workspace() { document.querySelector('#add').hidden = appRole !== 'administrator'; document.querySelector('#identity').hidden = true; document.querySelector('.topbar').hidden = false; document.querySelector('#workspace').hidden = false; try { await refresh(); } catch(error) { notice(error.message); } }
document.querySelector('#login').onsubmit = async event => { event.preventDefault(); const button = document.querySelector('#login-submit'); await action(button,async()=>{const body = Object.fromEntries(new FormData(event.target)); if (needsSetup) { await api('auth/setup',body); needsSetup = false; } csrf = (await api('auth/login',body)).csrf; event.target.reset(); await workspace();}); };
document.querySelector('#logout').onclick = event => action(event.target,async()=>{await api('auth/logout',{}); location.reload();});
document.querySelector('#add').onclick = () => dialog.showModal();
document.querySelector('#close-dialog').onclick = () => dialog.close();
document.querySelector('#refresh').onclick = event => action(event.target,refresh);
document.querySelector('#profile-form').onsubmit = async event => {event.preventDefault(); const form = event.target; await action(form.querySelector('button[type="submit"]'),async()=>{const file = document.querySelector('#configuration').files[0]; if (!file || file.size > 16384) throw new Error('Vælg en WireGuard-fil på højst 16 KiB.'); await api('profiles',{name:form.elements.namedItem('name').value,configuration:await file.text(),port_forwarding:form.elements.namedItem('port_forwarding').checked}); form.reset(); dialog.close(); await refresh(); notice('Forbindelsen er gemt og ikke startet.','success');});};
async function initializeLogin() {
  try {
    const status = await api('auth/status'); needsSetup = status.needsSetup;
    if (status.managed) {
      document.querySelector('#login').hidden = true;
      try { const identity = await api('auth/me'); csrf = identity.csrf; appRole = identity.role; await workspace(); }
      catch (error) { if (error.status === 401) location.replace(status.loginUrl); else notice(error.message); }
      return;
    }
    document.querySelector('#identity-title').textContent = needsSetup ? 'En frisk begyndelse.' : 'Velkommen tilbage.';
    document.querySelector('#login-submit').textContent = needsSetup ? 'Opret administrator' : 'Log ind';
    try { const identity = await api('auth/me'); csrf = identity.csrf; appRole = identity.role || 'administrator'; await workspace(); } catch {}
  } catch(error) { notice(error.message); }
}
void initializeLogin();
setInterval(() => { if (!document.hidden && !document.querySelector('#workspace').hidden && !busy) refresh().catch(() => {}); }, 15000);
