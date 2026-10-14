import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const source = readFileSync(new URL('../mediavpn/static/i18n.js', import.meta.url), 'utf8');
const messages = JSON.parse(source.split('const messages = ')[1].split(/;\r?\n/)[0]);

test('shipped Danish catalog contains no lossy encoding or question marks inside words', () => {
  for (const [key, value] of Object.entries(messages)) {
    assert.doesNotMatch(value, /[\uFFFD\u00C2\u00C3]/, key);
    const embeddedQuestions = /\p{L}\?\p{L}|(?:^|\s)\?\p{L}/gu;
    assert.ok([...value.matchAll(embeddedQuestions)].length <= [...key.matchAll(embeddedQuestions)].length, key);
  }
});

test('Danish gateway labels and user values retain their letters through language changes', () => {
  const context = vm.createContext({});
  vm.runInContext(source, context);
  const {t, setLanguage} = context.MediaAppI18n;
  setLanguage('da');
  assert.equal(t('Shared VPN gateway'), 'F\u00e6lles VPN-gateway');
  assert.equal(t('Container running'), 'Containeren k\u00f8rer');
  assert.equal(t('Choose a domain'), 'V\u00e6lg et dom\u00e6ne');
  assert.equal(t('Add feed'), 'Tilf\u00f8j feed');
  assert.equal(t('Search releases'), 'S\u00f8g efter udgivelser');
  assert.equal(t('Create your first VPN connection.'), 'Opret din f\u00f8rste VPN-forbindelse.');
  const name = '\u00c6gir \u00d8rn \u00c5se \u00e6\u00f8\u00e5';
  assert.equal(t('Delete {name}', {name}), `Slet ${name}`);
  assert.equal(t('What would you like to remove?'), 'Hvad vil du fjerne?');
  setLanguage('en');
  assert.equal(t('F\u00e6lles VPN-gateway'), 'Shared VPN gateway');
});
