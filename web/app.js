const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={queued:'排队中',running:'执行中',waiting_user:'等待处理',stopping:'正在停止',succeeded:'成功',failed:'失败',stopped:'已停止'};
const scenes={clean:'无待处理进程',unknown:'现场需核对',needs_recovery:'待恢复',recovering:'恢复中',restored:'已恢复',recovery_failed:'恢复失败',user_released:'用户已释放'};
const eventNames={'workload.updated':'负载与采样更新','workload.cleanup':'负载停止核对','task.created':'任务创建','task.state':'任务状态','approval.requested':'请求操作授权','approval.granted':'操作已授权','tool.started':'操作开始','tool.output':'命令输出','tool.finished':'操作结果','step.completed':'步骤完成','check.result':'检查结果','task.failed':'执行失败','input.requested':'需要补充信息','user.message':'用户消息','task.stopped':'任务停止','file.added':'文件已添加','recovery.proposed':'恢复方案已生成','recovery.authorized':'恢复已授权','cleanup.result':'进程清理结果','agent.summary':'执行结论'};
let state,selected=null,loading=false,serial=0,detailCache=new Map(),modelCache=new Map(),pendingUpdate=null;
const active=s=>['queued','running','waiting_user','stopping'].includes(s);
const link=(label,url,cls='secondary')=>`<a class="button ${cls}" href="${esc(url)}">${label}</a>`;
const btn=(label,action,id='',cls='')=>`<button data-action="${action}" data-id="${esc(id)}" class="${cls}">${label}</button>`;
const badge=s=>`<span class="badge ${esc(s)}">${esc(names[s]||s)}</span>`;
const date=v=>new Date(v).toLocaleString('zh-CN',{hour12:false});
async function request(path,data,raw=false){const response=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':raw?'application/zip':'application/json','X-Testagent-Token':state.token},body:raw?data:JSON.stringify(data)});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;}
function error(e){$('error').hidden=false;$('error').textContent=e.message;}
function info(text){$('error').hidden=true;$('flash').textContent=text;$('flash').hidden=false;setTimeout(()=>$('flash').hidden=true,5500);}
async function act(fn,message){try{const result=await fn();if(message)info(message);await refresh(true);return result;}catch(e){error(e);}}
function table(headers,rows,empty='暂无记录'){return rows.length?`<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`:`<div class="empty"><h2>${empty}</h2><p>从右上角添加所需配置，数据仅保存在本机。</p></div>`;}
function input(name,label,value='',type='text',required=true){return `<label for="f-${name}">${label}</label><input id="f-${name}" name="${name}" type="${type}" value="${esc(value)}" ${required?'required':''} autocomplete="off">`;}
function select(name,label,options,value=''){return `<label for="f-${name}">${label}</label><select id="f-${name}" name="${name}" required>${options.map(([v,t])=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(t)}</option>`).join('')}</select>`;}
function textArea(name,label,value='',required=false){return `<label for="f-${name}">${label}</label><textarea id="f-${name}" name="${name}" rows="5" ${required?'required':''}>${esc(value)}</textarea>`;}
function form(title,html,submit,label='保存'){ $('dialog-title').textContent=title;$('fields').innerHTML=html;const button=$('save');button.hidden=!submit;button.disabled=false;button.textContent=label;$('form').onsubmit=async e=>{e.preventDefault();button.disabled=true;try{await submit(Object.fromEntries(new FormData(e.target)));$('dialog').close();await refresh(true);}catch(err){error(err);}finally{button.disabled=false;}};$('dialog').showModal();}
const authoringPrompt = `请读取并遵循 testagent-skill-author/SKILL.md，根据我提供的操作流程、命令和脚本，编写可导入 Pangea Testagent 的任务 Skill。

正确声明设备角色、输入参数、关键操作确认点、执行步骤、完成检查和恢复方法。设备密码由平台设备库提供，不写入 Skill。

性能场景先读取 references/performance.md，声明 workloads、工具依赖、默认方案、适用条件、测试目标映射、并发关系、指标字段与单位、达标标准及停止恢复方法。持续任务使用平台 load_start/load_wait/load_stop；iSCSI、NVMe TCP、NAS 的内部配置与连接命令从本次资料中生成。整卡测试必须同时运行各端口负载，保留分端口与整卡结果；未达标时提供有证据的下一步建议，确认后复测。

缺少必要业务信息时向我确认，不编造命令或配置。完成后启动本机 testagent，使用编写助手附带的校验脚本进行校验，修正错误并生成可导入 ZIP。

本次任务场景：〔填写，例如 VXLAN 环境配置〕
操作资料：〔提供内部流程、命令、脚本及预期结果〕`;
function authoringDialog(){
 form('Skill 编写助手',`<section><h3>1. 下载编写助手</h3><p>解压后将整个 testagent-skill-author 文件夹交给内部 Agent，保留 references 和 scripts。支持安装 Skill 的 Agent 可直接安装，也可让它读取 SKILL.md。</p>${link('下载编写助手 ZIP','/api/authoring-skill')}</section><section><h3>2. 下达编写任务</h3><p>复制下方提示词，填写任务场景并附上内部操作资料。</p><label for="authoring-prompt">任务下达提示词</label><textarea id="authoring-prompt" rows="12" readonly>${esc(authoringPrompt)}</textarea><div class="toolbar compact"><button type="button" id="copy-authoring-prompt">复制提示词</button><span id="authoring-copy-status" role="status" aria-live="polite"></span></div><p>校验需要本机 testagent 已启动。校验通过后，将生成的任务 Skill ZIP 导入本页，再在目标环境验收。</p></section>`,null);
 $('form').onsubmit=e=>e.preventDefault();
 $('copy-authoring-prompt').onclick=async()=>{
  const status=$('authoring-copy-status');
  try{await navigator.clipboard.writeText($('authoring-prompt').value);status.textContent='已复制';}
  catch{$('authoring-prompt').focus();$('authoring-prompt').select();status.textContent='请按 Ctrl+C 复制已选中的提示词';}
 };
}
async function readBase64(file){if(file.size>64*1024**2)throw Error('单文件超过 64 MiB');return await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});}
function chooseFile(accept,callback,multiple=false,directory=false){const el=document.createElement('input');el.type='file';el.accept=accept;el.multiple=multiple;if(directory)el.webkitdirectory=true;el.onchange=()=>act(()=>callback([...el.files]));el.click();}
function render(){const route=location.hash.slice(1)||'tasks';selected=route.startsWith('tasks/')?route.split('/')[1]:null;const section=route.split('/')[0];watchTask(selected);document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash==='#'+section));$('title').textContent=({tasks:'任务中心',devices:'设备管理',environments:'环境组合',skills:'Skill 管理',settings:'设置与后端'})[section]||'任务中心';if(selected){renderDetail(selected).catch(error);return;}serial++;
 if(section==='devices')$('content').innerHTML=`<div class="toolbar"><p>统一保存 SSH 连接信息。密码无需重复填写。</p>${btn('添加设备','device')}</div>`+table(['设备名称','连接信息','凭据','操作'],state.devices.map(d=>`<tr><td><strong>${esc(d.name)}</strong></td><td>${esc(d.username||'模拟')}@${esc(d.address)}:${d.port||22}</td><td>${d.has_password?'已保存':'尚未填写'}</td><td class="actions">${btn('编辑','device',d.id,'secondary')}${btn('测试连接','probe',d.id,'secondary')}${btn('删除','delete-device',d.id,'quiet')}</td></tr>`));
 else if(section==='environments')$('content').innerHTML=`<div class="toolbar"><p>组合设备，保存角色绑定与操作权限。</p><div class="actions">${link('导出','/api/environments/export')}${btn('导入','import-env','','secondary')}${btn('新建环境','environment')}</div></div>`+table(['环境名称','设备角色','权限','操作'],state.environments.map(e=>`<tr><td><strong>${esc(e.name)}</strong></td><td>${Object.entries(JSON.parse(e.roles)).map(([r,id])=>`${esc(r)} → ${esc(state.devices.find(d=>d.id===id)?.name||id)}`).join('<br>')}</td><td>${e.policy==='automatic'?'全程自动':'关键操作确认'}</td><td class="actions">${btn('编辑','environment',e.id,'secondary')}${btn('删除','delete-env',e.id,'quiet')}</td></tr>`));
 else if(section==='skills')$('content').innerHTML=`<div class="toolbar"><p>导入后固定保存版本，更新请增加版本号。</p><div class="actions">${btn('编写助手','authoring','','secondary')}${btn('导入文件夹','import-folder','','secondary')}${btn('导入 ZIP','import-skill')}</div></div>`+table(['能力','版本','校验模式','操作'],state.skills.map(s=>`<tr><td><strong>${esc(s.name)}</strong><div>${esc(s.id)}</div></td><td>${esc(s.version)}</td><td>${s.mode==='structured'?'符合框架约定':'普通 Skill'}</td><td class="actions">${btn('查看','skill-view',JSON.stringify([s.id,s.version]),'secondary')}${link('导出',`/api/skills/export?id=${encodeURIComponent(s.id)}&version=${encodeURIComponent(s.version)}`)}${btn('删除','delete-skill',JSON.stringify([s.id,s.version]),'quiet')}</td></tr>`))+`<details class="card footer-card"><summary>框架验收样例</summary><p>只在本机模拟诊断交互，不连接任何真实设备。</p>${btn('导入模拟样例','example','','secondary')}</details>`;
 else if(section==='settings')$('content').innerHTML=`<div class="toolbar"><p>每次新建任务可独立选择后端与模型。</p><div class="actions">${btn('离线工具库','tools','','secondary')}${btn('发现本机 Agent','discover','','secondary')}${btn('添加后端','profile')}</div></div>`+table(['名称','类型','地址 / 启动命令','操作'],state.profiles.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc(p.kind)}</td><td>${esc(p.config.base_url||p.config.command||p.kind)}</td><td class="actions">${btn('编辑','profile',p.id,'secondary')}${btn('查询模型','models',p.id,'secondary')}${btn('删除','delete-profile',p.id,'quiet')}</td></tr>`))+`<div class="grid footer-card"><div class="card"><h2>离线升级</h2><p>当前版本 ${esc(state.version)}。升级保留设备、凭据、环境和任务数据。</p><p>直接选择下载的完整包或补丁 ZIP，无需解压或运行脚本。补丁复用已安装的运行时，版本不兼容时请用完整包。</p>${btn('导入升级包','update','','secondary')}${pendingUpdate?`<p>待升级：${esc(pendingUpdate.version)}</p>${btn('确认升级并重启','apply-update')}`:''}</div><div class="card"><h2>本机后台与通知</h2><p>关闭网页后任务继续执行。Windows 桌面会提示完成、失败及待处理事项。</p><p>等待确认没有自动超时；结束后台进程会使活动任务失败。</p></div></div>`;
 else $('content').innerHTML=`<div class="metrics">${[['执行与排队',state.tasks.filter(t=>['running','queued','stopping'].includes(t.status)).length],['等待处理',state.tasks.filter(t=>t.status==='waiting_user'||['unknown','needs_recovery'].includes(t.scene)).length],['已完成',state.tasks.filter(t=>t.status==='succeeded').length]].map(([t,n])=>`<div class="metric">${t}<strong>${n}</strong></div>`).join('')}</div><div class="toolbar"><p>配置设备与环境后，选择预设场景或自定义任务。</p><div class="actions">${btn('网卡极限带宽测试','nic-scenario','','secondary')}${btn('新建任务','task')}</div></div>`+table(['任务','状态','现场','创建时间',''],state.tasks.map(t=>`<tr><td><a href="#tasks/${t.id}">${esc(t.title)}</a></td><td>${badge(t.status)}</td><td>${esc(scenes[t.scene]||t.scene)}</td><td>${date(t.created)}</td><td>${!active(t.status)?btn('删除','delete-task',t.id,'quiet'):''}</td></tr>`));
}

function deviceForm(id){const d=state.devices.find(x=>x.id===id)||{};form(id?'编辑设备':'添加设备',input('name','设备名称',d.name)+input('address','IP / 主机地址',d.address)+input('port','SSH 端口',d.port||22,'number')+input('username','登录账号',d.username||'')+input('password',d.has_password?'密码（留空保留原密码）':'密码', '', 'password',false)+input('fingerprint','SSH 指纹（可留空，首次连接后保存）',d.fingerprint||'','text',false),v=>request('/api/devices',{...v,id:id||undefined,port:Number(v.port)}));}
function environmentForm(id){
 const env=state.environments.find(e=>e.id===id)||{};
 const bindings=env.roles?Object.entries(JSON.parse(env.roles)):[];
 const options=new Map([...(state.scenarios||[]).flatMap(s=>s.contract.roles),...(state.skill_roles||[])].map(r=>[r.id,r.name?`${r.name}（${r.id}）`:r.id]));
 for(const [role] of bindings)if(!options.has(role))options.set(role,`${role}（已有角色）`);
 form(id?'编辑环境组合':'新建环境组合',input('name','环境名称',env.name)+select('policy','操作权限',[['confirm','关键操作需要确认'],['automatic','全程自动']],env.policy||'confirm')+'<label>设备角色（来自预设场景和已导入 Skill）</label><div id="role-rows"></div><p id="role-hint" class="muted"></p><button type="button" id="add-role" class="secondary">添加角色</button>',v=>{
  const roles={};
  for(const row of $('role-rows').children){
   const role=row.querySelector('[data-environment-role]').value;
   if(!options.has(role)||Object.hasOwn(roles,role))throw Error('请选择角色，且角色不能重复');
   Object.defineProperty(roles,role,{value:row.querySelector('[data-environment-device]').value,enumerable:true});
  }
  if(!Object.keys(roles).length)throw Error('请至少添加一个设备角色');
  return request('/api/environments',{id:id||undefined,name:v.name,policy:v.policy,roles});
 });
 const sync=()=>{
  const rows=[...$('role-rows').children];
  const used=new Set(rows.map(row=>row.querySelector('[data-environment-role]').value));
  for(const row of rows){
   const select=row.querySelector('[data-environment-role]');
   for(const option of select.options)option.disabled=used.has(option.value)&&option.value!==select.value;
  }
  $('add-role').disabled=used.size>=options.size||!state.devices.length;
  $('save').disabled=!rows.length||!state.devices.length;
  $('role-hint').textContent=!options.size?'请先导入声明了设备角色的 Skill，再创建环境组合。':!state.devices.length?'请先在设备管理中添加设备。':'';
 };
 const add=(role,device)=>{
  const used=new Set([...$('role-rows').querySelectorAll('[data-environment-role]')].map(el=>el.value));
  role=role??[...options.keys()].find(key=>!used.has(key));
  if(role===undefined)return;
  const row=document.createElement('div');row.className='role-row';
  row.innerHTML=`<select aria-label="角色名称" data-environment-role required>${[...options].map(([key,label])=>`<option value="${esc(key)}" ${key===role?'selected':''}>${esc(label)}</option>`).join('')}</select><select aria-label="设备" data-environment-device required>${state.devices.map(d=>`<option value="${esc(d.id)}" ${d.id===device?'selected':''}>${esc(d.name)}</option>`).join('')}</select><button type="button" class="quiet">移除</button>`;
  row.querySelector('[data-environment-role]').onchange=sync;
  row.querySelector('button').onclick=()=>{row.remove();sync();};
  $('role-rows').append(row);sync();
 };
 if(bindings.length)bindings.forEach(([role,device])=>add(role,device));else if(options.size&&state.devices.length)add();
 $('add-role').onclick=()=>add();sync();
}
function profileForm(id){const p=state.profiles.find(p=>p.id===id)||{config:{},kind:'openai'};form(id?'编辑执行后端':'添加执行后端',input('name','显示名称',p.name)+select('kind','后端类型',[['openai','OpenAI 兼容 API'],['nga','nga / ACP'],['opencode','opencode / ACP'],['codeagent','codeagent / ACP']],p.kind)+'<div id="profile-config"></div>',v=>{let config;if(v.kind==='openai')config={base_url:v.base_url,ca_file:v.ca_file||undefined,timeout:Number(v.timeout||120)};else config={command:v.command,args:JSON.parse(v.args)};return request('/api/profiles',{id:id||undefined,name:v.name,kind:v.kind,config,api_key:v.api_key});});const renderConfig=()=>{const kind=$('f-kind').value;$('profile-config').innerHTML=kind==='openai'?input('base_url','服务地址（例如 https://server/v1）',p.config.base_url||'')+input('api_key',p.has_key?'API Key（留空保留）':'API Key','','password',false)+input('ca_file','内网 CA 文件路径（可选）',p.config.ca_file||'','text',false)+input('timeout','单次 API 请求超时（秒）',p.config.timeout||120,'number'):input('command','启动命令或程序路径',p.config.command||kind)+textArea('args','启动参数（JSON 数组）',JSON.stringify(p.config.args||['acp']),true);};$('f-kind').onchange=renderConfig;renderConfig();}
async function taskForm(scenarioId=null){if(!state.environments.length)throw Error('请先创建环境组合');if(!scenarioId&&!state.skills.length)throw Error('请先导入任务 Skill');const scenario=state.scenarios?.find(s=>s.id===scenarioId);if(scenarioId&&!scenario)throw Error('预设场景不存在');let selectedPackage=null;form(scenario?scenario.name:'新建任务',input('title','任务名称',scenario?.name||'')+select('environment_id','环境组合',state.environments.map(e=>[e.id,e.name]))+select('skill',scenario?'测试场景':'任务 Skill',(scenario?[scenario]:state.skills).map(s=>[JSON.stringify([s.id,s.version]),s.name+' / '+s.version]))+'<div id="task-roles"></div><div id="task-parameters"></div>'+select('backend','执行后端',state.profiles.map(p=>[p.id,p.name]).concat(scenario?[]:[['simulation','本地模拟（仅示例 Skill）']]),state.settings.last_backend?.backend||state.profiles[0]?.id||'simulation')+input('model','模型（ACP 可使用默认模型）',state.settings.last_backend?.model||'','text',false)+'<datalist id="model-options"></datalist><p id="model-status" class="muted"></p><button type="button" id="query-models" class="secondary">查询可用模型</button><label>输入文件（可选）</label><input id="task-files" type="file" multiple>',async v=>{const [skill_id,version]=JSON.parse(v.skill);const parameters={};for(const el of document.querySelectorAll('[data-param]')){const key=el.dataset.param;if(el.value==='')continue;const type=el.dataset.type;parameters[key]=type==='boolean'?el.value==='true':['integer','number'].includes(type)?Number(el.value):['object','array'].includes(type)?JSON.parse(el.value):el.value;}const roles={};document.querySelectorAll('[data-role]').forEach(el=>roles[el.dataset.role]=el.value);const files=await Promise.all([...$('task-files').files].map(async f=>({name:f.name,data:await readBase64(f)})));const result=await request('/api/tasks',{title:v.title,environment_id:v.environment_id,skill_id,version,...(scenario?{scenario_id:scenario.id}:{}),backend:v.backend,model:v.model,roles,parameters,files});location.hash='#tasks/'+result.result;},'启动任务');$('f-model').setAttribute('list','model-options');let generation=0;
 const refreshSkill=async()=>{const gen=++generation;const [id,version]=JSON.parse($('f-skill').value);const pkg=await request(scenario?'/api/scenarios/detail?id='+encodeURIComponent(scenario.id):'/api/skills/detail?id='+encodeURIComponent(id)+'&version='+encodeURIComponent(version));if(gen!==generation)return;selectedPackage=pkg;const contract=JSON.parse(pkg.files['contract.json']||'{}');const env=state.environments.find(e=>e.id===$('f-environment_id').value);const bindings=JSON.parse(env.roles);const needed=contract.roles||Object.keys(bindings).map(id=>({id}));$('task-roles').innerHTML='<h3>设备角色绑定</h3>'+needed.map(r=>`<label>${esc(r.name||r.id)}</label><select data-role="${esc(r.id)}" required>${[...new Set(Object.values(bindings))].map(d=>`<option value="${d}" ${bindings[r.id]===d?'selected':''}>${esc(state.devices.find(x=>x.id===d)?.name||d)}</option>`).join('')}</select>`).join('');const schema=contract.parameters||{};$('task-parameters').innerHTML=Object.entries(schema.properties||{}).map(([key,d])=>{let html=`<label>${esc(d.title||key)}${schema.required?.includes(key)?' *':''}</label>`;const attrs=`data-param="${esc(key)}" data-type="${esc(d.type||'string')}" ${schema.required?.includes(key)?'required':''}`;if(d.enum||d.type==='boolean')html+=`<select ${attrs}>${(d.enum||[true,false]).map(x=>`<option value="${esc(x)}" ${d.default===x?'selected':''}>${esc(x)}</option>`).join('')}</select>`;else if(['object','array'].includes(d.type))html+=`<textarea ${attrs}>${esc(d.default?JSON.stringify(d.default):'')}</textarea>`;else html+=`<input ${attrs} type="${['number','integer'].includes(d.type)?'number':'text'}" ${d.type==='number'?'step="any"':''} value="${esc(d.default??'')}">`;return html+(d.description?`<p class="muted">${esc(d.description)}</p>`:'');}).join('');};$('f-skill').onchange=()=>refreshSkill().catch(error);$('f-environment_id').onchange=()=>refreshSkill().catch(error);$('f-backend').onchange=()=>{$('f-model').value='';$('model-options').innerHTML='';$('model-status').textContent='选择后点击查询模型。';};$('query-models').onclick=async()=>{const id=$('f-backend').value;if(id==='simulation'){$('model-status').textContent='模拟器不使用模型';return;}const b=$('query-models');b.disabled=true;$('model-status').textContent='正在初始化后端并查询模型…';try{const r=await request('/api/profiles/models',{id});if($('f-backend').value!==id)return;modelCache.set(id,r.result.models);$('model-options').innerHTML=r.result.models.map(m=>`<option value="${esc(m)}"></option>`).join('');$('model-status').textContent=r.result.models.length?`已获取 ${r.result.models.length} 个模型，点击输入框选择。`:'后端未返回模型列表，可填写模型 ID 或使用 ACP 默认模型。';if(r.result.current_model)$('f-model').value=r.result.current_model;}catch(e){$('model-status').textContent=e.message;}finally{b.disabled=false;}};await refreshSkill();}
async function refresh(force=false){if(loading)return;loading=true;try{state=await request('/api/state');if(!$('dialog').open||force)render();}catch(e){error(e);}finally{loading=false;}}
$('close').onclick=()=>$('dialog').close();
$('content').addEventListener('click',event=>{const element=event.target.closest('[data-action]');if(!element)return;const action=element.dataset.action,id=element.dataset.id;const dispatch=async()=>{
 if(action==='tools')return toolsDialog();
 if(action==='load-refresh'||action==='load-stop'){const [task_id,name]=JSON.parse(id);return request(action==='load-stop'?'/api/workloads/stop':'/api/workloads/refresh',{task_id,name});}
 if(action==='authoring')return authoringDialog();
 if(action==='device')return deviceForm(id);if(action==='environment')return environmentForm(id);if(action==='profile')return profileForm(id);if(action==='task')return taskForm();
 if(action==='probe'){element.disabled=true;try{const r=await request('/api/devices/probe',{id});info('SSH 连接成功 · '+r.result.fingerprint);}finally{element.disabled=false;}return;}
 if(action==='models'){element.disabled=true;try{const r=await request('/api/profiles/models',{id});form('后端模型',`<pre>${esc(JSON.stringify(r.result,null,2))}</pre>`,null);}finally{element.disabled=false;}return;}
 if(action==='discover'){const r=await request('/api/profiles/discover',{});form('本机 Agent 发现结果',`<pre>${esc(JSON.stringify(r.result,null,2))}</pre><p>可将发现的路径填入执行后端配置。</p>`,null);return;}
 if(action==='import-env')return chooseFile('.json',async files=>request('/api/environments/import',JSON.parse(await files[0].text())));
 if(action==='import-skill')return chooseFile('.zip',async files=>request('/api/skills/import',await files[0].arrayBuffer(),true));
 if(action==='import-folder')return chooseFile('',async files=>{const map={};for(const f of files){const path=f.webkitRelativePath.split('/').slice(1).join('/');map[path]=await f.text();}return request('/api/skills/folder',{files:map});},true,true);
 if(action==='nic-scenario')return taskForm('nic-bandwidth');
 if(action==='example')return request('/api/skills/example',{});
 if(action==='skill-view'){const [sid,version]=JSON.parse(id);const pkg=await request('/api/skills/detail?id='+encodeURIComponent(sid)+'&version='+encodeURIComponent(version));form(pkg.name,select('skillfile','包内文件',Object.keys(pkg.files).map(n=>[n,n]))+'<pre id="skill-text"></pre>',null);const show=()=>$('skill-text').textContent=pkg.files[$('f-skillfile').value];$('f-skillfile').onchange=show;show();return;}
 if(action==='file')return chooseFile('',async files=>{for(const f of files)await request('/api/files',{task_id:id,name:f.name,data:await readBase64(f)});},true);
 if(action==='reject-preview')return request('/api/preview/reject',{task_id:selected,preview_id:id});
 if(action==='approve-preview')return request('/api/preview/approve',{task_id:selected,preview_id:id});
 if(action==='approve')return request('/api/approve',{task_id:selected,approval_id:id});
 if(action==='reject')return request('/api/reject',{task_id:selected,approval_id:id});
 if(action==='stop'&&confirm('将尝试强制终止当前命令和远端脚本。不能确认停止时会保留占用；环境恢复需要另行授权。继续？'))return request('/api/stop',{task_id:id});
 if(action==='recover')return request('/api/recovery/propose',{task_id:id});
 if(action==='approve-recovery'&&confirm('授权执行当前显示的恢复方案？'))return request('/api/recovery/approve',{id});
 if(action==='reject-recovery')return request('/api/recovery/reject',{id});
 if(action==='release'&&confirm('确认已经核对现场及残留进程？释放后其他任务可操作这些设备，此操作不会恢复环境。'))return request('/api/scene/release',{task_id:id});
 if(action.startsWith('delete-')&&confirm('确认删除？此操作不会撤销远端环境变更。')){const paths={'delete-device':'/api/devices/delete','delete-env':'/api/environments/delete','delete-profile':'/api/profiles/delete','delete-task':'/api/tasks/delete','delete-skill':'/api/skills/delete'};let value={id};if(action==='delete-task')value={task_id:id};if(action==='delete-skill'){const [sid,version]=JSON.parse(id);value={id:sid,version};}return request(paths[action],value);}
 if(action==='update')return chooseFile('.zip',async files=>{pendingUpdate=null;const r=await request('/api/updates/import',await files[0].arrayBuffer(),true);pendingUpdate=r.result;});
 if(action==='apply-update'&&confirm('确认升级并重启后台？')){await request('/api/updates/apply',{});info('后台正在升级，稍后刷新页面。');return;}
 };act(dispatch);
});
$('content').addEventListener('click',e=>{const a=e.target.closest('[data-evidence]');if(a){e.preventDefault();const event=[...document.querySelectorAll('[data-operation]')].filter(el=>el.dataset.operation===a.dataset.evidence).at(-1);if(event){filterDevice(null);event.querySelector('details').open=true;event.scrollIntoView({behavior:'smooth'});}}});
window.addEventListener('hashchange',()=>{if(state)render();});
setInterval(()=>{if(document.hidden||$('dialog').open)return;if(selected){if(taskStream?.readyState===1&&Date.now()-(detailCache.get(selected)?.fetchedAt||0)<15000)return;renderDetail(selected).catch(error);}else refresh();},1600);
refresh();

async function toolsDialog(){
 const tools=await request('/api/tools');
 form('离线工具库',`<p>工具按版本和 Linux 架构保存；部署时核对架构和文件校验值。Vdbench 与自研工具可通过内部工具包导入。</p><button type="button" id="import-tool">导入工具包 ZIP</button>${table(['工具','版本','架构','校验标识'],tools.map(t=>`<tr><td>${esc(t.name)}</td><td>${esc(t.version)}</td><td>${esc(t.architecture)}</td><td><code>${esc(t.id.slice(0,16))}</code></td></tr>`),'暂无离线工具包')}<p>工具包包含 tool.json、入口及依赖文件，格式见编写助手的性能场景约定。</p>`,null);
 $('form').onsubmit=e=>e.preventDefault();
 $('import-tool').onclick=()=>chooseFile('.zip',async files=>{await request('/api/tools/import',await files[0].arrayBuffer(),true);await toolsDialog();});
}
