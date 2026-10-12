import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const source = readFileSync(new URL('../mediavpn/static/i18n.js', import.meta.url), 'utf8');

test('account language translates static text, attributes and title reversibly without touching data', () => {
  const label = {nodeValue: 'Fjern', isConnected: true};
  const detached = {nodeValue: 'Gem', isConnected: true};
  const attribute = {isConnected: true, values: {placeholder: 'Brugernavn'}, hasAttribute(k) {return k in this.values;}, getAttribute(k) {return this.values[k];}, setAttribute(k,v) {this.values[k] = v;}};
  const nodes = [label, detached]; let index = 0;
  const document = {title: 'MediaSeed', documentElement: {}, body: {}, createTreeWalker: () => ({nextNode: () => nodes[index++]}), querySelectorAll: () => [attribute]};
  const context = vm.createContext({document}); vm.runInContext(source, context);
  const {t, setLanguage, locale} = context.MediaAppI18n;
  detached.isConnected = false; detached.nodeValue = 'User.File.S01E01.mkv';
  setLanguage('en'); assert.equal(document.documentElement.lang, 'en'); assert.equal(locale(), 'en-GB');
  assert.equal(label.nodeValue, 'Remove'); assert.equal(attribute.values.placeholder, 'Username');
  assert.equal(detached.nodeValue, 'User.File.S01E01.mkv');
  setLanguage('da'); assert.equal(label.nodeValue, 'Fjern'); assert.equal(attribute.values.placeholder, 'Brugernavn');
  const filename = '<User & File>.torrent';
  assert.equal(t('Delete {name}', {name: filename}), `Slet ${filename}`);
  assert.equal(t(filename), filename);
  assert.equal(t('VPN port {port}', {port: 52848}), 'VPN-port 52848');
  setLanguage('en'); assert.equal(t('VPN-port {port}', {port: 52848}), 'VPN port 52848');
});

test('language engine works in the extension service worker without a document', () => {
  const context = vm.createContext({}); vm.runInContext(source, context);
  context.MediaAppI18n.setLanguage('en'); assert.equal(context.MediaAppI18n.t('Fjern'), 'Remove');
  context.MediaAppI18n.setLanguage('da'); assert.equal(context.MediaAppI18n.t('Remove'), 'Fjern');
});
