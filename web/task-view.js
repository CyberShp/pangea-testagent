// Stable task panes: append events and text without replacing the active editor.
const detailRequests=new Map();
let taskStream=null,streamTask=null;
function watchTask(id){
 if(streamTask===id)return;
 if(taskStream)taskStream.close();taskStream=null;streamTask=id;
 if(id&&typeof EventSource!=='undefined'){
  taskStream=new EventSource('/api/stream?task_id='+encodeURIComponent(id)+'&after='+(detailCache.get(id)?.cursor||0));
  taskStream.onmessage=event=>{renderDetail(id).then(()=>{if(selected===id&&Number(event.data)>(detailCache.get(id)?.cursor||0))return renderDetail(id);}).catch(error);};
 }
}
window.addEventListener('beforeunload',()=>taskStream?.close());
const panelMarkup=new WeakMap();
function panel(id,html){
 const target=$(id);if(panelMarkup.get(target)===html)return;
 panelMarkup.set(target,html);
 const opened=new Set([...target.querySelectorAll('details[open][data-key]')].map(el=>el.dataset.key));
 target.innerHTML=html;
 target.querySelectorAll('details[data-key]').forEach(el=>{if(opened.has(el.dataset.key))el.open=true;});
}
function operationText(op){
 const names={shell_open:'建立交互终端',shell_close:'关闭交互终端',wait_connected:'等待设备重新连接',remote_read:'读取文件',remote_write:'写入文件',upload:'上传文件',download:'下载文件',script:'执行脚本'};
 if(op.action==='exec'||op.action==='simulation')return op.command;
 if(op.action==='shell_send')return op.text;
 if(op.action==='remote_write')return `${names[op.action]}：${op.path}\n${op.text}`;
 if(op.action==='script')return `${names[op.action]}：${op.path||op.file_id} ${(op.args||[]).join(' ')}`;
 return (names[op.action]||op.action)+(op.path?'：'+op.path:'');
}
function previewHTML(data){
 const plans=data.previews||[];
 if(!plans.length)return `<p class="muted">${active(data.task.status)?'正在准备变更预览，确认后开始设备操作。':'此任务没有保存变更预览。'}</p>`;
 return plans.map(p=>{
  const pending=p.status==='pending'&&data.task.status==='waiting_user';
  const labels={pending:'待确认',approved:'已确认',superseded:'已更新',rejected:'已拒绝',cancelled:'已取消'};
  return `<details class="preview-plan" data-key="preview-${esc(p.id)}" ${pending?'open':''}><summary>${esc(labels[p.status]||p.status)} · ${esc(p.data.summary)}</summary><div class="preview-facts"><div><strong>影响范围</strong><p>${esc(p.data.impact)}</p></div><div><strong>验证方法</strong><p>${esc(p.data.verification)}</p></div></div><ol class="preview-operations">${p.data.operations.map((op,i)=>{
   const device=data.task.snapshot.devices?.[data.task.snapshot.roles[op.role]];
   return `<li><strong>${esc(device?.name||op.role)}</strong> <span class="muted">${esc(op.role)} · ${esc(device?.address||'')}</span><pre>${esc(operationText(op))}</pre><details data-key="operation-${esc(p.id)}-${i}"><summary>操作参数${op.capture_id?' · '+(op.capture_phase==='before'?'执行前采集':'执行后采集'):''}</summary><pre>${esc(JSON.stringify(op,null,2))}</pre>${op.action==='script'&&op.path?`<pre>${esc(data.task.snapshot.skill.files[op.path]||'')}</pre>`:''}</details></li>`;
  }).join('')||'<li>本阶段没有设备操作。</li>'}</ol><p class="muted">已开始 ${p.position} / ${p.data.operations.length} 项操作</p>${pending?`<div class="actions">${btn('确认预览并继续','approve-preview',p.id)}${btn('拒绝并结束任务','reject-preview',p.id,'secondary')}</div>`:''}</details>`;
 }).join('');
}
function topologyHTML(task,events){
 const snap=task.snapshot,contract=JSON.parse(snap.skill.files['contract.json']||'{}');
 const roles=Object.keys(snap.roles),operations=new Map(),devices=new Map();
 for(const event of events){
  const p=event.payload;
  if(['tool.started','approval.requested','operation.ready'].includes(event.kind)){
   operations.set(p.id,p.device_id);
   if(p.device_id)devices.set(p.device_id,{status:event.kind==='tool.started'?'running':event.kind==='approval.requested'?'waiting_user':'queued',operation:p.id});
  }else if(event.kind==='tool.finished'){
   const device=operations.get(p.id);if(device)devices.set(device,{status:p.output?.error?'failed':'succeeded',operation:p.id});
  }else if(event.kind==='check.result'&&!p.passed){
   const device=operations.get(p.evidence);if(device)devices.set(device,{status:'check_failed',operation:p.evidence});
  }else if(event.kind==='operation.superseded'){
   const device=operations.get(p.id);if(device)devices.set(device,{status:'idle',operation:p.id});
  }
 }
 const cols=Math.min(3,Math.max(1,roles.length)),rows=Math.ceil(roles.length/cols),width=cols*260,height=rows*140;
 const positions=new Map(roles.map((role,i)=>[role,{x:(i%cols)*260+130,y:Math.floor(i/cols)*140+70}]));
 const links=(contract.topology||[]).filter(l=>positions.has(l.from)&&positions.has(l.to));
 return `<p class="muted topology-caption">${links.length?'连线来自 Skill 声明；节点展示最近操作状态。':'Skill 未声明连线，展示绑定设备及最近操作状态。'}</p><div class="topology-scroll"><svg class="topology columns-${cols}" viewBox="0 0 ${width} ${height}" role="group" aria-label="任务设备拓扑">${links.map(l=>{
  const a=positions.get(l.from),b=positions.get(l.to);
  return `<g class="topology-link"><line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/><text x="${(a.x+b.x)/2}" y="${(a.y+b.y)/2-10}" text-anchor="middle">${esc(l.label||'')}</text></g>`;
 }).join('')}${roles.map(role=>{
  const device=snap.devices?.[snap.roles[role]],p=positions.get(role),last=devices.get(snap.roles[role])||{status:'idle'};
  let status=last.status;if(['failed','stopped'].includes(task.status)){if(status==='running')status='unknown';else if(['waiting_user','queued'].includes(status))status='cancelled';}
  const statusText=({idle:'尚未操作',unknown:'操作结果待核对',succeeded:'最近操作完成',failed:'最近操作失败',check_failed:'验证未通过',cancelled:'操作已取消'})[status]||names[status];
  return `<g class="device-node ${esc(status)}" transform="translate(${p.x-100},${p.y-40})" tabindex="0" role="button" data-device-role="${esc(role)}" aria-label="${esc((device?.name||role)+'：'+statusText)}"><title>${esc(device?.address||'')} · ${esc(role)}</title><rect width="200" height="80" rx="10"/><circle cx="16" cy="20" r="4"/><text x="30" y="25">${esc((device?.name||role).slice(0,18))}</text><text x="14" y="47" class="node-role">${esc(role.slice(0,24))}</text><text x="14" y="67" class="node-status">${esc(statusText)}</text></g>`;
 }).join('')}</svg></div>`;
}
function comparisonsHTML(changes){
 if(!changes.length)return '<p class="muted">Skill 未声明配置采集方法，暂无配置对比。</p>';
 return changes.map(c=>`<details class="comparison" data-key="comparison-${esc(c.id)}"><summary>${esc(c.name)} <span class="muted">${esc(c.role)} · ${!c.available?'无法对比':c.changed?'发现差异':'采集结果一致'}</span></summary>${!c.available?`<p>缺少${!c.before&&!c.after?'执行前及执行后':!c.before?'执行前':'执行后'}采集结果。</p>`:''}<div class="capture-pair">${['before','after'].map(phase=>`<section><h3>${phase==='before'?'执行前':'执行后'}</h3>${c[phase]?`<a href="#" data-evidence="${esc(c[phase].operation_id)}">查看采集依据</a><pre>${esc(c[phase].text)}</pre>`:'<p class="muted">尚未采集</p>'}</section>`).join('')}</div>${c.available?`<h3>逐行差异</h3><pre class="config-diff">${c.changed?c.diff.split('\n').map(line=>`<span class="${line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-remove':''}">${esc(line)}\n</span>`).join(''):'两次采集结果一致'}</pre>`:''}</details>`).join('');
}
function ensureTaskShell(task){
 if($('task-detail')?.dataset.taskId===task.id)return false;
 $('content').innerHTML=`<div id="task-detail" data-task-id="${esc(task.id)}"><div id="task-heading"></div><section class="card execution-overview"><div class="section-heading"><h2>变更预览</h2><span class="muted">确认后执行</span></div><div id="task-preview"></div></section><section class="card execution-overview"><h2>设备与执行状态</h2><div id="task-topology"></div></section><div class="detail"><div class="stack"><div class="card" id="task-progress"></div><div class="card"><h2>配置前后对比</h2><div id="task-comparisons"></div></div><div class="card" id="task-files-panel"></div><div class="card" id="task-recovery"></div><div class="card"><div class="section-heading"><h2>命令与执行日志</h2><button id="clear-device-filter" class="secondary" hidden>显示全部设备</button></div><p id="device-filter-label" class="muted" hidden></p><div id="task-events" class="execution-log" tabindex="0" aria-label="执行日志"></div><button id="latest-logs" class="secondary latest-button" hidden>回到最新日志</button></div></div><details id="chat" class="card chat" ${innerWidth>1250?'open':''}><summary>Agent 对话</summary><div class="chat-messages" id="chat-messages" tabindex="0" aria-label="Agent 对话"></div><button id="latest-chat" class="secondary latest-button" hidden>回到最新对话</button><form id="chat-form"><label for="task-message">补充信息或调整后续要求</label><textarea id="task-message" rows="4" required></textarea><button type="submit">发送</button></form></details></div></div>`;
 $('chat-form').addEventListener('submit',e=>{
  e.preventDefault();const editor=$('task-message'),text=editor.value;
  act(async()=>{await request('/api/tasks/message',{task_id:task.id,text});if(editor.value===text)editor.value='';},'消息已提交，后续设备操作需要重新预览');
 });
 for(const [pane,button] of [['task-events','latest-logs'],['chat-messages','latest-chat']]){
  const el=$(pane);$(button).onclick=()=>{el.scrollTop=el.scrollHeight;$(button).hidden=true;};
  el.addEventListener('scroll',()=>{$(button).hidden=el.scrollHeight-el.scrollTop-el.clientHeight<32;});
 }
 $('clear-device-filter').onclick=()=>filterDevice(null);
 $('task-topology').addEventListener('click',e=>{const node=e.target.closest('[data-device-role]');if(node)filterDevice(node.dataset.deviceRole);});
 $('task-topology').addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){const node=e.target.closest('[data-device-role]');if(node){e.preventDefault();filterDevice(node.dataset.deviceRole);}}});
 return true;
}
function filterDevice(role){
 const shell=$('task-detail');if(!shell)return;shell.dataset.filterRole=role||'';
 const cache=detailCache.get(shell.dataset.taskId);if(!cache)return;
 const device=role?cache.data.task.snapshot.roles[role]:null;
 $('task-events').querySelectorAll('[data-device-id]').forEach(el=>{el.hidden=!!device&&el.dataset.deviceId!==device;});
 $('clear-device-filter').hidden=!role;$('device-filter-label').hidden=!role;$('device-filter-label').textContent=role?'当前设备角色：'+role:'';
}
function appendEvents(cache,fresh){
 if(fresh){cache.rendered=0;cache.lastChatKind=null;cache.operationDevices=new Map();}
 const log=$('task-events'),chat=$('chat-messages');
 const followLog=log.scrollHeight-log.scrollTop-log.clientHeight<32,followChat=chat.scrollHeight-chat.scrollTop-chat.clientHeight<32;
 for(const event of cache.events.slice(cache.rendered||0)){
  const p=event.payload;
  if(p.device_id&&p.id)cache.operationDevices.set(p.id,p.device_id);
  const chatEvent=['agent.chunk','agent.message','user.message','agent.summary','input.requested'].includes(event.kind);
  if(chatEvent){
   if(event.kind==='agent.chunk'&&cache.lastChatKind==='agent.chunk'&&chat.lastElementChild){
    chat.lastElementChild.querySelector('.message-text').append(document.createTextNode(p.text||''));
   }else{
    const el=document.createElement('div');el.className='message'+(event.kind==='user.message'?' user':'');
    el.innerHTML=`<strong>${event.kind==='user.message'?'你':'Agent'}</strong><div class="message-text"></div>`;el.querySelector('.message-text').textContent=p.text||'';chat.append(el);
   }
  }
  cache.lastChatKind=event.kind;
  if(!['agent.chunk','agent.message','user.message'].includes(event.kind)){
   const el=document.createElement('div');el.className='event';el.id='event-seq-'+event.seq;
   el.dataset.deviceId=p.device_id||cache.operationDevices.get(p.id)||'';
   if(p.id)el.dataset.operation=p.id;
   el.innerHTML=`<time>${esc(date(event.at))}</time><strong>${esc(eventNames[event.kind]||({'preview.requested':'变更预览待确认','preview.approved':'预览已确认','preview.superseded':'预览已更新','config.captured':'配置已采集'})[event.kind]||event.kind)}</strong><details data-key="${event.seq}"><summary>查看详情</summary><pre></pre></details>`;
   el.querySelector('pre').textContent=typeof p.text==='string'?p.text:JSON.stringify(p,null,2);log.append(el);
  }
 }
 cache.rendered=cache.events.length;
 if(followLog)log.scrollTop=log.scrollHeight;else $('latest-logs').hidden=false;
 if(followChat)chat.scrollTop=chat.scrollHeight;else $('latest-chat').hidden=false;
 filterDevice($('task-detail').dataset.filterRole||null);
}
async function renderDetail(id){
 if(detailRequests.has(id))return detailRequests.get(id);
 const work=(async()=>{
  const cache=detailCache.get(id)||{events:[],cursor:0};
  let data;
  do{
   data=await request('/api/tasks/'+encodeURIComponent(id)+'?after='+cache.cursor);
   cache.events.push(...data.events);if(data.events.length)cache.cursor=data.events.at(-1).seq;
  }while(data.events.length===1000);
  cache.data=data;cache.fetchedAt=Date.now();detailCache.set(id,cache);
  if(selected!==id)return;
  const task=data.task,snap=task.snapshot,events=cache.events,contract=JSON.parse(snap.skill.files['contract.json']||'{}');
  const fresh=ensureTaskShell(task);
  const resolved=new Set(events.filter(e=>['approval.granted','operation.superseded'].includes(e.kind)).map(e=>e.payload.id));
  const pending=events.filter(e=>e.kind==='approval.requested'&&!resolved.has(e.payload.id)).at(-1);
  const waiting=events.filter(e=>e.kind==='input.requested').at(-1);
  const previewPending=(data.previews||[]).some(p=>p.status==='pending');
  panel('task-heading',`<div class="toolbar"><div class="actions">${link('返回任务','#tasks')}${badge(task.status)}<span>${esc(scenes[task.scene]||task.scene)}</span></div><div class="actions">${link('导出报告','/api/report?task_id='+id)}${active(task.status)&&task.status!=='stopping'?btn('强制停止','stop',id,'danger'):''}</div></div><h2>${esc(task.title)}</h2><p>${esc(snap.environment_name)} · ${esc(snap.skill.name)} / ${esc(snap.skill.version)} · ${esc(snap.profile?.name||'本地模拟')} ${esc(snap.model||'')}</p>${snap.backend==='simulation'?'<div class="notice">这是模拟任务，没有连接真实设备。</div>':''}${task.status==='waiting_user'&&!previewPending?`<div class="notice"><h3>需要你的处理</h3>${pending?`<p>${esc(pending.payload.description)}</p><pre>${esc(pending.payload.command)}</pre><div class="actions">${btn('授权此操作','approve',pending.payload.id)}${btn('拒绝并结束任务','reject',pending.payload.id,'secondary')}</div>`:`<p>${esc(waiting?.payload.text||'请在对话区补充信息')}</p>`}</div>`:''}`);
  panel('task-preview',previewHTML(data));panel('task-topology',topologyHTML(task,events));panel('task-comparisons',comparisonsHTML(data.comparisons||[]));
  const completed=new Set(events.filter(e=>e.kind==='step.completed').map(e=>e.payload.id));
  const checks=new Map(events.filter(e=>e.kind==='check.result').map(e=>[e.payload.id,e.payload]));
  panel('task-progress',`<h2>执行步骤</h2><ol class="steps">${(contract.steps||[]).map(s=>`<li><span class="step-dot ${completed.has(s.id)?'done':''}"></span>${esc(s.name||s.id)} <span>${completed.has(s.id)?'已完成':'待执行'}${s.required?' · 必要步骤':''}</span></li>`).join('')||'<li>普通 Skill 未定义结构化步骤。</li>'}</ol><h3>检查结果</h3>${[...checks.values()].map(c=>`<p>${c.passed?'✓':'×'} ${esc(c.id)}：${c.passed?'通过':'未通过'} <a href="#" data-evidence="${esc(c.evidence)}">查看依据</a></p>`).join('')||'<p>尚未产生检查结果。</p>'}`);
  panel('task-files-panel',`<div class="toolbar compact"><h2>任务文件</h2>${active(task.status)?btn('补充文件','file',id,'secondary'):''}</div>${table(['文件','类别','大小',''],data.files.map(f=>`<tr><td>${esc(f.name)}</td><td>${esc({input:'输入',output:'输出',backup:'变更前备份'}[f.category]||f.category)}</td><td>${Math.ceil(f.size/1024)} KiB</td><td>${link('下载',`/api/files/download?task_id=${id}&file_id=${f.id}`)}</td></tr>`),'暂无文件')}`);
  const recovery= ['failed','stopped'].includes(task.status)||data.recoveries.length;
  $('task-recovery').hidden=!recovery;
  if(recovery)panel('task-recovery',`<h2>环境恢复</h2><p>恢复需单独授权，执行前仍需确认具体操作预览。</p><div class="actions">${['failed','stopped'].includes(task.status)?btn('生成恢复方案','recover',id)+btn('已核对现场，释放占用','release',id,'secondary'):''}</div>${data.recoveries.map(r=>`<div class="recovery"><h3>${esc({planning:'正在生成方案',proposed:'方案待授权',running:'正在恢复',succeeded:'恢复成功',failed:'恢复失败',rejected:'已拒绝'}[r.status]||r.status)}</h3><pre>${esc(r.plan||'正在根据执行记录生成…')}</pre>${r.status==='proposed'?`<div class="actions">${btn('授权执行此恢复方案','approve-recovery',r.id)}${btn('拒绝方案','reject-recovery',r.id,'secondary')}</div>`:''}${r.child_id?link('查看恢复任务','#tasks/'+r.child_id):''}</div>`).join('')}`);
  $('chat-form').hidden=!active(task.status);
  appendEvents(cache,fresh);
 })();
 detailRequests.set(id,work);
 try{return await work;}finally{detailRequests.delete(id);}
}
