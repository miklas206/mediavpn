(() => {
  const form=document.querySelector('#profile-form'), modal=document.querySelector('#profile-dialog');
  const inputs=[...form.children].filter(item=>!item.classList.contains('actions'));
  const usage=node('section'), review=node('section');
  usage.append(node('h3',t('Apps on this connection')),node('p',t('Selected apps will start this VPN when you save. Leave empty to save a stopped connection.'),'muted'));
  const apps=node('div'), selected=new Set(); usage.append(apps);
  let loading=false, created=null, submitting=false;
  modal.addEventListener('cancel',event=>{if(submitting)event.preventDefault();});
  async function loadApps(){
    if(loading)return;loading=true;
    apps.replaceChildren(node('p',t('Loading app status…'),'muted'));
    try{const state=await api('hub-apps');apps.replaceChildren();
      for(const app of state.apps||[]){const row=node('label'),box=document.createElement('input');box.type='checkbox';box.checked=selected.has(app.id);box.disabled=app.enabled==null;box.onchange=()=>box.checked?selected.add(app.id):selected.delete(app.id);row.append(box,document.createTextNode(app.name+(app.profileId?' · '+t('Already assigned'):'')));apps.append(row);}
    }catch{apps.replaceChildren(node('p',t('App status unavailable · retrying'),'muted'));selected.clear();}finally{loading=false;}
  }
  const purpose=node('select'); purpose.id='profile-purpose';
  for(const [value,label] of [['apps','VPN for apps'],['domains','VPN with app domains']])purpose.append(new Option(t(label),value));
  const label=node('label',t('Use this connection for')); label.append(purpose);usage.append(label);
  const hint=node('p',undefined,'muted');usage.append(hint);
  purpose.onchange=()=>{hint.textContent=t(purpose.value==='domains'?'After saving, choose this profile in the Cloudflare guide to configure app domains.':'No domain or Cloudflare account is needed for app traffic.');};purpose.onchange();
  MediaWizard({form,dialog:modal,titles:['VPN configuration','Connection use','Review connection'],groups:[inputs,[usage],[review]],onStep:step=>{
    if(step===1)void loadApps();
    if(step!==2)return;
    review.replaceChildren(node('h3',form.elements.namedItem('name').value),node('p',document.querySelector('#configuration').files[0]?.name||''),node('p',t(form.elements.namedItem('port_forwarding').checked?'Port forwarding enabled':'Port forwarding disabled')),node('p',selected.size?[...selected].join(', '):t('The connection is saved and has not started.')));
    form.querySelector('button[type=submit]').textContent=t(selected.size?'Save and connect apps':'Save connection');
  }});
  form.onsubmit=async event=>{event.preventDefault();if(submitting)return;submitting=true;form.inert=true;document.querySelector('#close-dialog').disabled=true;const domains=purpose.value==='domains';try{await action(form.querySelector('button[type=submit]'),async()=>{
    if(loading)throw Error(t('Loading app status…'));
    if(selected.size&&!form.elements.namedItem('port_forwarding').checked)throw Error(t('Enable port forwarding before connecting apps.'));
    const latest=selected.size?await api('hub-apps'):null;
    if(latest?.apps?.some(app=>selected.has(app.id)&&app.profileId&&app.profileId!==created?.id)&&!confirm(t('Move the selected apps to this VPN? Their connections will restart.')))return;
    if(!created){const file=document.querySelector('#configuration').files[0];if(!file||file.size>16384)throw Error(t('Choose a WireGuard file no larger than 16 KiB.'));created=await api('profiles',{name:form.elements.namedItem('name').value,configuration:await file.text(),port_forwarding:form.elements.namedItem('port_forwarding').checked});for(const field of inputs.flatMap(item=>[...item.querySelectorAll('input')]))field.disabled=true;}
    for(const id of [...selected]){await api('hub-apps/'+id,{enabled:true,profileId:created.id},'PUT');selected.delete(id);}
    const identifier=created.id;form.reset();modal.close();await refresh();notice(t('Connection saved.'),'success');if(domains){MediaVPNGateway.openProfile(identifier);selectVPNTab('cloudflare');}
  });}finally{submitting=false;form.inert=false;document.querySelector('#close-dialog').disabled=false;}};
  modal.addEventListener('close',()=>{created=null;selected.clear();for(const field of inputs.flatMap(item=>[...item.querySelectorAll('input')]))field.disabled=false;});
})();
