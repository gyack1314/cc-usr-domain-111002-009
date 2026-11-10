const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

let sequence=0;let items=JSON.parse(localStorage.getItem('uploads')||'[]');const draft='DRAFT-17';$('#draft').textContent=draft;
function fields(){$('#fields').innerHTML=$('#kind').value==='report'?'<label>检测日期<input id="report-date" type="date"></label>':'<label>许可编号<input id="license-id"></label>';}
function render(){localStorage.setItem('uploads',JSON.stringify(items));$('#uploads').innerHTML=items.map(item=>`<article class="panel" data-upload-id="${item.id}" data-project="${item.project}" data-digest="${item.digest}"><p>${html(item.name)} ${item.id}</p><p class="check-state">${item.state}</p><button data-remove="${item.id}">移除附件</button></article>`).join('');for(const button of $$('[data-remove]'))button.onclick=()=>{items=items.filter(i=>i.id!==button.dataset.remove);render();};}
$('#files').onchange=async event=>{for(const file of event.target.files){const id='UP-'+(++sequence)+'-'+Date.now();const bytes=await file.arrayBuffer();const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');const item={id,name:file.name,digest,project:$('#project').value,kind:$('#kind').value,state:'预检中'};items.push(item);render();await action('upload',{upload_id:id,name:file.name,digest});await tick();let state=await runtime();if(state.phase==='delayed-precheck'&&file.name==='hold.txt'){while(state.phase==='delayed-precheck'&&state.version===1){await tick();state=await runtime();}}const live=items.find(i=>i.id===id);if(live){live.state=file.size===0?'预检失败':'通过';render();}}event.target.value='';};
$('#back').onclick=()=>{items.reverse();fields();render();show('已回退并重建附件组件');};$('#kind').onchange=fields;$('#preview').onclick=()=>{$('#preview-result').textContent=JSON.stringify({draft_id:draft,project:$('#project').value,attachments:items.filter(i=>i.state==='通过')},null,2);};$('#publish').onclick=async()=>{await action('publish',{draft});show('草稿已发布');};fields();render();show('草稿可编辑，发布由工作人员决定');
