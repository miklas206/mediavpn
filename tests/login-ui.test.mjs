import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

function element() {
  return { value: '', hidden: false, dataset: {}, classList: { add() {}, contains() { return false; } },
    matches() { return false; }, addEventListener() {}, setAttribute() {}, append() {}, replaceChildren() {}, querySelectorAll() { return []; } };
}

for (const status of [401, 403, 503]) {
  test(`managed app redirects automatically only for an absent session (${status})`, async () => {
    const nodes = new Map();
    const find = selector => {
      if (!nodes.has(selector)) nodes.set(selector, element());
      return nodes.get(selector);
    };
    const redirects = [];
    const context = vm.createContext({
      MediaAppTheme: {setOverrideHandler() {}, applyIdentity() {}},
      window: {addEventListener() {}},
      document: { querySelector: find, querySelectorAll: () => [], createElement: element, body: element(), addEventListener() {} },
      location: { replace: url => redirects.push(url) }, setInterval() {}, setTimeout() {}, clearTimeout() {},
      fetch: async url => ({ ok: url === '/api/auth/status', status,
        json: async () => url === '/api/auth/status'
          ? { managed: true, needsSetup: false, loginUrl: '/auth/hub/start' }
          : { detail: status === 401 ? 'Sign in required' : 'MediaHub unavailable or denied' },
      }),
    });
    vm.runInContext(readFileSync(new URL('../mediavpn/static/i18n.js', import.meta.url), 'utf8') + '\n' + readFileSync(new URL('../mediavpn/static/app.js', import.meta.url), 'utf8'), context);
    await context.initializeLogin();
    assert.equal(find('#login').hidden, true);
    assert.equal(redirects.length > 0, status === 401);
    if (status === 401) assert.ok(redirects.every(url => url === '/auth/hub/start'));
    else assert.equal(find('#message').textContent, 'MediaHub unavailable or denied');
  });
}
