// DOM interaction tests; this is not a browser screenshot or visual-layout test.
const {JSDOM,VirtualConsole}=require('jsdom');
const {spawn}=require('node:child_process');
const fs=require('node:fs');const os=require('node:os');const path=require('node:path');const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');const data=fs.mkdtempSync(path.join(os.tmpdir(),'testagent-ui-'));const port=18769;const url=`http://127.0.0.1:${port}`;
const server=spawn(process.env.PYTHON||'python3',['-m','testagent.server','--port',String(port),'--data',data],{cwd:root,env:{...process.env,PYTHONPATH:path.join(root,'src')},stdio:['ignore','pipe','pipe']});
let logs='';server.stdout.on('data',d=>logs+=d);server.stderr.on('data',d=>logs+=d);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,message){for(let i=0;i<100;i++){if(await fn())return;await sleep(50);}throw Error(message+'\n'+logs);}
let dom;
(async()=>{await until(async()=>{try{return (await fetch(url+'/api/health')).ok;}catch{return false;}},'server did not start');
const errors=[];const console=new VirtualConsole();console.on('jsdomError',e=>errors.push(e.message));
dom=await JSDOM.fromURL(url,{runScripts:'dangerously',resources:'usable',pretendToBeVisual:true,virtualConsole:console,beforeParse(w){w.fetch=(u,o)=>fetch(new URL(u,url),o);w.confirm=()=>true;w.HTMLElement.prototype.scrollIntoView=function(){};w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};Object.defineProperty(w,'innerWidth',{value:1440});}});
const w=dom.window,d=w.document;
const button=text=>[...d.querySelectorAll('button')].find(e=>e.textContent===text);
const set=(id,v)=>{const e=d.getElementById(id);assert.ok(e,'missing '+id);e.value=v;e.dispatchEvent(new w.Event('change',{bubbles:true}));};
const submit=()=>d.getElementById('form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
const route=async hash=>{w.location.hash=hash;await sleep(100);};
await until(()=>button('新建任务'),'initial tasks');
await route('#devices');button('添加设备').click();await until(()=>d.getElementById('f-name'),'device form');set('f-name','控制器 A');set('f-address','sim://a');set('f-username','tester');submit();await until(()=>!d.getElementById('dialog').open,'device save');assert.ok(d.body.textContent.includes('控制器 A'));
await route('#environments');button('新建环境').click();await until(()=>d.getElementById('role-rows'),'environment form');set('f-name','环境 A');submit();await until(()=>!d.getElementById('dialog').open,'environment save');
await route('#skills');button('导入模拟样例').click();await until(()=>d.body.textContent.includes('诊断视图检查（模拟）'),'skill import');
await route('#tasks');button('新建任务').click();await until(()=>d.querySelector('[data-role]'),'dynamic role form');set('f-title','第一条任务');submit();await until(()=>button('授权此操作'),'approval visible');button('授权此操作').click();await until(()=>d.querySelector('.badge.succeeded'),'task success');const firstHash=w.location.hash;assert.ok(d.body.textContent.includes('flag-present'));
await route('#tasks');button('新建任务').click();await until(()=>d.querySelector('[data-role]'),'second task form');set('f-title','第二条任务');submit();await until(()=>button('授权此操作'),'second approval');button('授权此操作').click();await until(()=>d.querySelector('.badge.succeeded'),'second success');assert.ok(d.querySelector('#task-detail h2').textContent.includes('第二条任务'));
await route(firstHash);await until(()=>d.querySelector('#task-detail h2')?.textContent==='第一条任务','history task identity');
await route('#settings');button('添加后端').click();await until(()=>d.getElementById('f-base_url'),'profile form');set('f-name','内网 API');set('f-base_url','http://127.0.0.1:9/v1');set('f-api_key','ui-fixture-secret');submit();await until(()=>!d.getElementById('dialog').open,'profile save');assert.ok(d.body.textContent.includes('内网 API'));assert.ok(!d.body.textContent.includes('ui-fixture-secret'));
assert.deepEqual(errors,[]);process.stdout.write('UI DOM checks passed: CRUD, dynamic roles, authorization, completion, history isolation, backend configuration.\n');
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;}).finally(async()=>{if(dom)dom.window.close();server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));fs.rmSync(data,{recursive:true,force:true});});
