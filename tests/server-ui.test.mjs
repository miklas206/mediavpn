import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

test('server selection offers manual fallback and sends only the chosen country', async () => {
  const nodes = new Map();
  const create = () => ({value: '', dataset: {}, children: [], addEventListener() {},
    append(...items) {this.children.push(...items);}, replaceChildren(...items) {this.children = items;},
    showModal() {this.open = true;}, close() {this.open = false;}, matches: () => false});
  const find = key => {if (!nodes.has(key)) nodes.set(key, create()); return nodes.get(key);};
  const writes = [];
  const context = vm.createContext({document: {querySelector: find, querySelectorAll: () => [], createElement: create, addEventListener() {}},
    setInterval() {}, setTimeout() {}, clearTimeout() {}, confirm: () => true,
    fetch: async (url, options) => {
      if (options.method === 'PUT') writes.push([url, JSON.parse(options.body)]);
      return {ok: true, json: async () => url === '/api/proton/countries' ? {countries: ['Denmark', 'Germany']} : url === '/api/proton/recommendations' ? {recommendations: [{country: 'Denmark', server: 'dk-01.protonvpn.net', latencyMs: 10}], tested: 4, measuredAt: Date.now() / 1000} : {needsSetup: true}};
    },
  });
  vm.runInContext(readFileSync(new URL('../mediavpn/static/i18n.js', import.meta.url), 'utf8') + '\n' + readFileSync(new URL('../mediavpn/static/app.js', import.meta.url), 'utf8'), context);
  context.MediaAppI18n.setLanguage('da'); context.refresh = async () => {};
  assert.equal(context.countryLabel('Albania'), 'Albanien');
  assert.equal(context.countryLabel('DE'), 'Tyskland');
  assert.equal(context.countryFlag('Albania').src, '/static/flags/al.svg');
  assert.equal(context.countryFlag('Denmark').src, '/static/flags/dk.svg');
  assert.equal(context.countryFlag(null), null);
  await context.chooseServer({id: 'synthetic', auto_country: null});
  assert.equal(find('#server-dialog').open, true);
  assert.deepEqual(find('#server-choices').children.map(row => row.dataset.serverChoice), ['*', '']);
  assert.deepEqual(find('#server-countries').children.map(row => row.dataset.serverChoice), ['Denmark', 'Germany']);
  assert.equal(find('#server-country').value, '');
  find('#server-country').value = 'Denmark';
  await find('#server-form').onsubmit({preventDefault() {}});
  assert.deepEqual(writes[0], ['/api/profiles/synthetic/server-selection', {country: 'Denmark'}]);
  assert.equal(find('#server-dialog').open, false);
  await context.chooseServer({id: 'synthetic', auto_country: 'Denmark'});
  assert.equal(find('#server-country').value, 'Denmark');
  find('#server-country').value = '';
  await find('#server-form').onsubmit({preventDefault() {}});
  assert.equal(writes[1][1].country, null);
  await context.chooseServer({id: 'synthetic', auto_country: '*'});
  await find('#server-form').onsubmit({preventDefault() {}});
  assert.equal(writes[2][1].country, '*');
});
