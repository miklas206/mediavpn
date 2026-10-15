/* Shared progressive forms. Hidden steps retain their values and enabled state. */
globalThis.MediaWizard = function ({form, dialog, titles, groups, onStep, validate}) {
  const make = (tag, className) => { const node = document.createElement(tag); node.className = className; return node; };
  dialog.classList.add('guided-dialog'); form.classList.add('guided-form'); form.noValidate = true;
  const progress = make('p','wizard-progress'); progress.setAttribute('role','status');
  const body = make('div','wizard-body'), footer = form.querySelector(':scope > .actions');
  const panels = groups.map(nodes => {const panel = make('section','wizard-step'); panel.append(...nodes.filter(Boolean)); body.append(panel); return panel;});
  const previous = make('button','wizard-back'), next = make('button','primary wizard-next'); previous.type = next.type = 'button';
  const submits = [...footer.querySelectorAll('button')].filter(button => button.type === 'submit');
  footer.prepend(previous); footer.append(next); footer.classList.add('wizard-footer'); form.prepend(progress,body); form.append(footer);
  let step = 0;
  function show(index, focus=true) {
    step = index; panels.forEach((panel,i) => {panel.hidden = i !== step;});
    progress.textContent = `${step+1} / ${panels.length} · ${t(titles[step])}`;
    previous.textContent = t('Back'); next.textContent = t('Next'); previous.hidden = step === 0; next.hidden = step === panels.length-1;
    for(const button of submits) button.hidden = step !== panels.length-1;
    body.scrollTop = 0;
    if(focus) (panels[step].querySelector('input:not([type=hidden]):not(:disabled),select:not(:disabled),button') || next).focus({preventScroll:true});
    onStep?.(step);
  }
  function valid(index) {
    for(const control of panels[index].querySelectorAll('input,select,textarea')) {
      if(!control.disabled && !control.checkValidity()) {show(index,false); control.reportValidity(); return false;}
    }
    return validate?.(index) !== false;
  }
  next.onclick = () => {if(valid(step)) show(step+1);}; previous.onclick = () => show(step-1);
  form.addEventListener('submit', event => {
    if(step < panels.length-1) {event.preventDefault(); event.stopImmediatePropagation(); if(valid(step))show(step+1); return;}
    for(let i=0;i<panels.length;i++) if(!valid(i)) {event.preventDefault();event.stopImmediatePropagation();return;}
  }, true);
  dialog.addEventListener('close',()=>show(0,false));
  form.addEventListener('reset',()=>show(0,false));
  new MutationObserver(()=>show(step,false)).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  show(0,false);
  return {show};
};
