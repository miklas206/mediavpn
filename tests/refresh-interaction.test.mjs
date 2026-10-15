import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
const source = readFileSync(new URL('../mediavpn/static/app.js', import.meta.url), 'utf8');
test('refresh preserves an open menu and keyboard focus by row identity after reordering', () => {
  const document = {activeElement: null};
  function row(id, open=false) {
    const menu = {open};
    const controls = [{focus(){document.activeElement=this;}}, {focus(){document.activeElement=this;}}];
    return {dataset:{key:id},menu,controls,contains:node=>controls.includes(node),
      querySelector:selector=>selector==='details' || (selector==='details[open]' && menu.open) ? menu : null,
      querySelectorAll:()=>controls};
  }
  const first=row('one',true), second=row('two');
  const target={children:[first,second]};document.activeElement=first.controls[1];
  const context=vm.createContext({document});
  const begin=source.indexOf('function preserveListInteraction(');
  vm.runInContext(source.slice(begin,source.indexOf('\nasync function refreshProfiles',begin)>0?source.indexOf('\nasync function refreshProfiles',begin):source.indexOf('\nfunction renderTorrents',begin)),context);
  const restore=context.preserveListInteraction(target,'key');
  const fresh=row('one');target.children=[row('two'),fresh];restore();
  assert.equal(fresh.menu.open,true);assert.equal(document.activeElement,fresh.controls[1]);
  document.activeElement={outside:true};const outside=document.activeElement;
  const noSteal=context.preserveListInteraction(target,'key');target.children=[row('one')];noSteal();
  assert.equal(document.activeElement,outside);
  document.activeElement=target.children[0].controls[0];const removed=context.preserveListInteraction(target,'key');target.children=[];
  assert.doesNotThrow(removed);
});
