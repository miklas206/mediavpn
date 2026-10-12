import {readFileSync, existsSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const root = new URL('../mediavpn/static/', import.meta.url);
const source = readFileSync(new URL('app.js', root), 'utf8');
const begin = source.indexOf('function countryCode(');
const end = source.indexOf('let serverProfile;', begin);
const context = vm.createContext({node: () => ({}), element: () => ({})});
vm.runInContext(source.slice(begin, end), context);

test('every country in the Proton catalog resolves to a bundled SVG flag', () => {
  const names = JSON.parse(readFileSync(new URL('fixtures/proton-country-names.json', import.meta.url)));
  assert.equal(names.length, 145);
  for (const name of names) {
    const flag = context.countryFlag(name);
    assert.ok(flag, `Missing country mapping: ${name}`);
    assert.ok(existsSync(new URL(flag.src.replace('/static/', ''), root)), `Missing SVG: ${name} -> ${flag.src}`);
  }
});

test('provider names and historical region aliases use current country codes', () => {
  for (const [name, code] of Object.entries({Germany:'de',DD:'de',CS:'rs',Serbia:'rs',Vietnam:'vn',Yemen:'ye',Zimbabwe:'zw',UK:'gb','Syrian Arab Republic':'sy','Palestine, State of':'ps'})) {
    assert.equal(context.countryFlag(name).src, `/static/flags/${code}.svg`);
  }
  assert.equal(context.countryLabel('Syrian Arab Republic'), new Intl.DisplayNames(['da'], {type:'region'}).of('SY'));
  assert.equal(context.countryFlag('Atlantis'), null);
  assert.equal(context.countryFlag(null), null);
});
