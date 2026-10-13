// Generated from brand-marks.json by sync-brand-icons.mjs.
(() => {
 const marks={"mediahub":[{"d":"M10 4H22L28 10V22L22 28H10L4 22V10Z"},{"d":"M10 21V11L16 17L22 11V21"}],"mediaseed":[{"d":"M16 4V19M10 13L16 19L22 13"},{"d":"M5 20V25L8 28H24L27 25V20"}],"mediavpn":[{"d":"M16 3L27 7V16C27 22 22 26 16 29C10 26 5 22 5 16V7Z"},{"d":"M19 14A3 3 0 1 1 13 14A3 3 0 1 1 19 14Z","fill":true},{"d":"M16 17V22"}]};
 const root=document.documentElement, ns='http://www.w3.org/2000/svg';
 let scheduled=false,previous='';
 function update() {
  scheduled=false;
  const link=document.querySelector('link[data-brand-favicon]');
  const kind=link?.dataset.brandFavicon;if(!marks[kind])return;
  const styles=getComputedStyle(root);
  const color=styles.getPropertyValue('--accent').trim() || '#25c9ed';
  const surface=styles.getPropertyValue('--surface').trim() || '#102030';
  const key=kind+color+surface;if(key===previous)return;previous=key;
  const svg=document.createElementNS(ns,'svg');
  svg.setAttribute('viewBox','0 0 32 32');
  const bg=document.createElementNS(ns,'rect');
  for(const [k,v] of Object.entries({x:'0',y:'0',width:'32',height:'32',rx:'7',fill:surface}))bg.setAttribute(k,v);
  svg.append(bg);
  const group=document.createElementNS(ns,'g');
  for(const [k,v] of Object.entries({transform:'translate(3 3) scale(.8125)',fill:'none',stroke:color,'stroke-width':'2.4','stroke-linecap':'round','stroke-linejoin':'round'}))group.setAttribute(k,v);
  for(const mark of marks[kind]) {
   const path=document.createElementNS(ns,'path');path.setAttribute('d',mark.d);
   if(mark.fill)path.setAttribute('fill',color);
   group.append(path);
  }
  svg.append(group);link.type='image/svg+xml';link.href='data:image/svg+xml,'+encodeURIComponent(new XMLSerializer().serializeToString(svg));
 }
 function schedule(){if(!scheduled){scheduled=true;requestAnimationFrame(update);}}
 new MutationObserver(schedule).observe(root,{attributes:true,attributeFilter:['style','class','data-look','data-appearance-mode','data-theme']});
 schedule();
 globalThis.MediaBrandIcons={update};
})();
