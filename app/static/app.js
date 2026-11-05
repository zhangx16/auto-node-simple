'use strict';
const el=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let watchState={enabled:false};
let installPlatformVM=null,installPlatformPorts=[],remoteInstallId='';
let timer=null,clientEdited=false,installed=[],subscriptions=[],editingSub=null,editingNode=null,nodePortEdited=false,cfState={configured:false,zone_name:''};const formatChoices=new Map();
async function api(path,body,method){const r=await fetch(path,{method:method||(body?'POST':'GET'),headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const text=await r.text();let d;try{d=JSON.parse(text);}catch{d=text;}if(!r.ok){if(r.status===401)show(false);throw Error(d.detail||'请求失败');}return d;}
function error(e){el('error').textContent=e.message;el('error').hidden=false;}
function show(authed){el('login').hidden=authed;el('panel').hidden=!authed;el('logout').hidden=!authed;el('sidebar').hidden=!authed;el('app-shell').classList.toggle('is-authed',authed);if(authed)switchView(currentView,false);else{clearSSHSecrets();el('workspace-title').textContent='Auto Node Simple';el('workspace-subtitle').textContent='三种协议，一条安装命令。';}if(timer)clearInterval(timer);if(authed){load().catch(error);loadPlatformStatus().catch(error);timer=setInterval(()=>load().catch(()=>{}),5000);}}
function fields(){syncPlatformPorts();const direct=el('protocol').value!=='argo',fixed=!direct,manual=fixed&&el('cf-mode').value==='manual';el('cf-mode-field').hidden=direct;el('sni-field').hidden=!direct;el('fixed-domain-field').hidden=!fixed;el('fixed-token-field').hidden=!manual;el('fixed-hint').hidden=!fixed;el('fixed-domain').required=manual;el('fixed-token').required=manual;el('fixed-domain').placeholder=manual?'例如 hk.example.com':cfState.configured?'留空自动生成 node-随机值.'+cfState.zone_name:'先配置默认账户或选择手动模式';el('address').required=direct;el('address-label').textContent=direct?'服务器公网地址 / DDNS 域名':'优选域名 / IP（留空使用 www.cloudflare.com）';el('address').placeholder=direct?'例如 203.0.113.10 或 node.example.com':'默认 www.cloudflare.com';el('protocol-hint').textContent=direct?(el('protocol').value==='hysteria2'?'请放行并映射 UDP 端口。默认使用自签证书，导出链接启用 insecure。':'请放行并映射 TCP 端口。Reality SNI 对应的服务器需支持 TLS 1.3。'):'Argo 通过出站隧道连接，无需公网映射监听端口。仅支持 Cloudflare 固定隧道。';if(!clientEdited)el('client-port').value=direct?el('port').value:443;syncDeploymentFields();}
el('cf-mode').onchange=()=>{el('fixed-token').value='';fields();};
el('protocol').onchange=()=>{clientEdited=false;fields();};el('port').oninput=fields;el('client-port').oninput=()=>clientEdited=true;
el('login-form').onsubmit=async e=>{e.preventDefault();el('error').hidden=true;const b=e.submitter;b.disabled=true;try{await api('/api/login',Object.fromEntries(new FormData(e.target)));e.target.reset();show(true);}catch(x){error(x);}finally{b.disabled=false;}};
el('logout').onclick=async()=>{await api('/api/logout',{});show(false);};
el('install-form').onsubmit=async e=>{e.preventDefault();el('error').hidden=true;el('generate').disabled=true;el('command-section').hidden=true;try{const d=Object.fromEntries(new FormData(e.target));d.port=Number(d.port);d.client_port=Number(d.client_port);const automatic=el('deployment-mode').value==='ssh'&&!!d.platform_vm_id;
if(automatic){
 if(!el('ssh-fingerprint').value)throw Error('请先读取并核对 SSH 主机指纹');
 const ssh={host:el('ssh-host').value.trim(),forward_id:el('ssh-forward').value,fingerprint:el('ssh-fingerprint').value,password:el('ssh-auth-mode').value==='password'?el('ssh-password').value:'',private_key:el('ssh-auth-mode').value==='key'?el('ssh-key').value:'',passphrase:el('ssh-passphrase').value};
 try{const r=await api('/api/vpsnat/vms/'+d.platform_vm_id+'/deploy',{install:d,ssh});remoteInstallId=r.id;el('remote-install-status').hidden=false;el('remote-install-status').textContent='SSH 自动安装已开始，正在等待回传；可切换到安装记录查看。';await load();}finally{clearSSHSecrets();}return;
}
const r=await api('/api/installs',d);el('command').value=r.command;el('command-hostname').textContent=(el('install-vm-id').value?'请在 VPSNAT 实例 #'+el('install-vm-id').value+' 内执行。':'')+(r.hostname?'固定域名：'+r.hostname:'');switchView('install',false);el('command-section').hidden=false;el('fixed-token').value='';await load();}catch(x){error(x);}finally{el('generate').disabled=false;}};
async function copy(text,button){try{await navigator.clipboard.writeText(text);button.textContent='已复制';setTimeout(()=>button.textContent='复制',1500);}catch{error(Error('复制失败，请选中文本手动复制'));}}
el('copy-command').onclick=e=>copy(el('command').value,e.target);
async function load(){const [rows,ns,ss,cf,automation,watch]=await Promise.all([api('/api/installs'),api('/api/nodes'),api('/api/subscriptions'),api('/api/cloudflare'),api('/api/vpsnat/automation/argo'),api('/api/vpsnat/watch')]);drawAutomation(automation);drawWatch(watch);if(remoteInstallId){const job=rows.find(r=>r.id===remoteInstallId);if(job){el('remote-install-status').hidden=false;el('remote-install-status').textContent=job.status==='done'?'自动安装成功，节点已保存。':job.status==='failed'?'自动安装未完成，请查看安装记录。':job.remote?.message||'SSH 自动安装中，请等待回传。';}}cfState=cf;el('cf-default-status').textContent=cf.configured?'已加载默认配置：'+cf.zone_name+'。API 凭据保存在服务器端。':'尚未配置默认账户，可选择手动填写隧道 Token。';fields();installed=ns;subscriptions=ss;drawSubscriptions();drawNodes();el('result-count').textContent=' · '+rows.length+' 条';el('results').innerHTML=rows.length?rows.map(r=>`<article class="result"><strong>安装 ${esc(r.id)}</strong><p class="status">${new Date(r.created*1000).toLocaleString('zh-CN')} · ${r.remote?.status==='running'?'SSH 自动安装中':{ready:'等待执行命令',done:'安装完成',failed:'安装未完成'}[r.status]||r.status}</p>${r.remote?`<p class="hint">${esc(r.remote.message||'SSH 任务执行中')}</p>${r.remote.status==='unknown'?`<button class="secondary" data-remote-ack="${esc(r.id)}">已核对服务器状态，解除安装锁定</button>`:''}`:''}${r.error?`<p>${esc(r.error)}</p>`:''}${r.result?`<textarea readonly rows="3" aria-label="节点链接">${esc(r.result)}</textarea><button class="secondary" data-copy="${esc(r.id)}">复制节点链接</button>`:''}</article>`).join(''):'<p class="hint">暂无安装记录。</p>';el('results').querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>copy(rows.find(r=>r.id===b.dataset.copy).result,b));}
el('results').addEventListener('click',async e=>{const b=e.target.closest('[data-remote-ack]');if(!b)return;if(!confirm('请确认已在服务器核对安装是否仍在执行及节点实际状态。解除锁定不会停止或回滚远端安装，继续？'))return;try{await api('/api/installs/'+b.dataset.remoteAck+'/remote-acknowledge',{});await load();}catch(x){error(x);}});
el('refresh').onclick=()=>load().catch(error);

const formats={base64:'通用 Base64',raw:'原始节点链接',clash:'Clash / Mihomo',singbox:'sing-box'};
function drawSubscriptions(){el('subs').innerHTML=subscriptions.length?subscriptions.map(s=>`<article class="result" data-sub="${s.id}"><div class="heading"><strong>${esc(s.name)}</strong><span class="status">${s.node_ids.length} 个节点 · ${s.enabled?'已启用':'已停用'}</span></div><label>客户端格式<select data-sub-format="${s.id}">${Object.entries(formats).map(([k,v])=>`<option value="${k}" ${k===(formatChoices.get(s.id)||'base64')?'selected':''}>${v}</option>`).join('')}</select></label><input readonly data-sub-url="${s.id}" value="${esc(s.urls[formatChoices.get(s.id)||'base64'])}" aria-label="订阅地址"><div class="actions"><button class="secondary" data-sub-action="copy" data-id="${s.id}">复制地址</button><button class="secondary" data-sub-action="edit" data-id="${s.id}">编辑节点</button><button class="secondary" data-sub-action="toggle" data-id="${s.id}">${s.enabled?'停用':'启用'}</button><button class="secondary" data-sub-action="delete" data-id="${s.id}">删除订阅</button></div></article>`).join(''):'<p class="hint">暂无订阅。安装完成后，创建订阅并选择节点。</p>';}
function openSubscription(s=null){editingSub=s;el('sub-title').textContent=s?'编辑订阅':'创建订阅';el('sub-name').value=s?.name||'';el('sub-error').hidden=true;const selected=new Set(s?.node_ids||[]);el('node-picker').innerHTML=installed.length?installed.map(n=>`<label class="node-choice"><input type="checkbox" value="${esc(n.id)}" ${selected.has(n.id)?'checked':''}><span><strong>${esc(n.name)}</strong><small>${esc({argo:'Argo 固定隧道',reality:'Reality Vision',hysteria2:'Hysteria2'}[n.protocol]||n.protocol)}</small></span></label>`).join(''):'<p class="hint">尚无成功安装的节点。可以先保存空订阅，安装后再编辑选择。</p>';el('sub-dialog').showModal();}
el('new-sub').onclick=()=>openSubscription();el('sub-close').onclick=()=>el('sub-dialog').close();
el('sub-form').onsubmit=async e=>{e.preventDefault();el('sub-save').disabled=true;el('sub-error').hidden=true;try{const b={name:el('sub-name').value.trim(),node_ids:[...el('node-picker').querySelectorAll('input:checked')].map(i=>i.value),enabled:editingSub?editingSub.enabled:true};await api(editingSub?'/api/subscriptions/'+editingSub.id:'/api/subscriptions',b,editingSub?'PUT':'POST');el('sub-dialog').close();await load();}catch(e){el('sub-error').textContent=e.message;el('sub-error').hidden=false;}finally{el('sub-save').disabled=false;}};
el('subs').onchange=e=>{const id=e.target.dataset.subFormat;if(id){formatChoices.set(id,e.target.value);el('subs').querySelector(`[data-sub-url="${id}"]`).value=subscriptions.find(s=>s.id===id).urls[e.target.value];}};
el('subs').onclick=async e=>{const b=e.target.closest('[data-sub-action]');if(!b)return;const s=subscriptions.find(s=>s.id===b.dataset.id);b.disabled=true;try{switch(b.dataset.subAction){case 'copy':await copy(el('subs').querySelector(`[data-sub-url="${s.id}"]`).value,b);break;case 'edit':openSubscription(s);break;case 'toggle':await api('/api/subscriptions/'+s.id,{name:s.name,node_ids:s.node_ids,enabled:!s.enabled},'PUT');await load();break;case 'delete':if(confirm('删除此订阅？原节点仍保留。')){await api('/api/subscriptions/'+s.id,undefined,'DELETE');await load();}break;}}catch(e){error(e);}finally{b.disabled=false;}};



const nodeOpened=new Set();
function repairInfo(n){
 const r=n.repair||{};if(!r.configured)return '<p class="hint">尚未收到巡检配置；新安装会自动部署，旧节点可在下方补装。</p>';
 const state=!r.enabled?'已停用':r.paused?'达到上限，已暂停':!r.last_report?'等待安装 / 首次巡检':Date.now()/1000-r.last_report>180?'巡检超时':'巡检运行中';
 const probe={ok:'转发正常',failed:'转发失败',unavailable:'探测组件不可用',unknown:'等待测活'}[r.probe_status]||'等待测活';
 const result={core_restarted:'核心已重启',tunnel_restarted:'隧道已重启',core_and_tunnel_restarted:'核心及隧道已重启',restart_failed:'重启失败',shared_core_cooldown:'共享核心冷却中'}[r.last_result]||'';
 return `<p class="hint">自动修复：${state} · 安装脚本 v${r.script_version||1} · ${probe}${r.latency_ms!=null?' · '+r.latency_ms+' ms':''} · 已授权 ${r.repair_count} 次 · 本地连续失败 ${r.local_failures} 次${n.protocol==='argo'?' · 公网 WS 连续失败 '+r.public_failures+' 次':''}${result?' · '+result:''}<br>最后巡检：${r.last_report?new Date(r.last_report*1000).toLocaleString('zh-CN'):'尚未收到'}${r.last_repair?' · 最近修复：'+new Date(r.last_repair*1000).toLocaleString('zh-CN'):''}</p>${r.probe_error?`<p class="node-warning">${esc(r.probe_error)}</p>`:''}${upgradeInfo(r)}`;
}
function upgradeInfo(r){
 if((r.script_version||1)<3)return '<p class="hint">自动升级：需手动执行一次升级安装脚本，之后自动维护完整节点安装流程。</p>';
 const state=!r.auto_upgrade?'已停用':r.upgrade_status==='running'?'升级中':r.upgrade_attempts>=3&&r.installed_release!==r.target_release?'同版失败已达上限':r.installed_release===r.target_release?'当前版本':'等待自动升级';
 return `<p class="hint">完整安装脚本自动升级：${state} · 当前 ${(r.installed_release||'未登记').slice(0,8)} · 目标 ${(r.target_release||'暂不可用').slice(0,8)}</p>${r.upgrade_error?`<p class="node-warning">${esc(r.upgrade_error)}</p>`:''}`;
}
function repairButtons(n){
 const r=n.repair||{};
 return `<button class="secondary" data-node-action="repair-setup" data-id="${n.id}">${r.configured?(r.script_version<3?'升级安装脚本':'重装安装脚本'):'开启自动修复'}</button>${r.configured?`<button class="secondary" data-node-action="repair-${r.enabled&&!r.paused?'disable':'resume'}" data-id="${n.id}">${r.enabled&&!r.paused?'停用修复':r.paused?'解除暂停 / 重新计数':'启用修复'}</button><button class="secondary" data-node-action="upgrade-${r.auto_upgrade?'disable':'enable'}" data-id="${n.id}">${r.auto_upgrade?'停用自动升级':'启用自动升级'}</button>${r.upgrade_attempts>=3&&r.installed_release!==r.target_release?`<button class="secondary" data-node-action="upgrade-retry" data-id="${n.id}">重新尝试升级</button>`:''}`:''}`;
}
function drawNodes(){
 document.querySelectorAll('[data-managed-node]').forEach(d=>{if(d.open)nodeOpened.add(d.dataset.managedNode);else nodeOpened.delete(d.dataset.managedNode);});
 el('managed-count').textContent=' · '+installed.length+' 个';
 el('managed-nodes').innerHTML=installed.length?installed.map(n=>`<details class="managed-node" data-managed-node="${n.id}" ${nodeOpened.has(n.id)?'open':''}><summary><strong>${esc(n.name)}</strong><span class="status">${esc({argo:'Argo',reality:'Reality Vision',hysteria2:'Hysteria2'}[n.protocol])}</span></summary><p class="hint">${esc(n.address)}:${n.client_port} · 监听 ${n.port||'首次编辑时读取'}${n.pending_update?' · 更新'+(n.pending_update.expired?'已过期':n.pending_update.status==='failed'?'执行失败':'待执行')+'，目标监听 '+n.pending_update.port:''}</p>${repairInfo(n)}<textarea readonly rows="3" aria-label="节点链接">${esc(n.uri)}</textarea><div class="actions"><button class="secondary" data-node-action="copy" data-id="${n.id}">复制链接</button><button class="secondary" data-node-action="edit" data-id="${n.id}" ${n.pending_update?'disabled':''}>编辑</button>${n.pending_update?`<button class="secondary" data-node-action="cancel" data-id="${n.id}">取消更新</button>`:''}<button class="secondary" data-node-action="delete" data-id="${n.id}">删除节点</button>${repairButtons(n)}</div>${n.pending_update?.error?`<p class="node-warning">${esc(n.pending_update.error)}</p>`:''}${n.pending_update?'<p class="hint">请执行生成的更新命令，成功回传后更新节点和订阅。如命令丢失，确认未执行后取消，再重新编辑。</p>':''}</details>`).join(''):'<p class="hint">暂无节点，安装成功后自动显示。</p>';
}
el('node-close').onclick=()=>el('node-dialog').close();
el('node-client-port').oninput=()=>nodePortEdited=true;
el('node-port').oninput=()=>{if(editingNode?.protocol!=='argo'&&!nodePortEdited)el('node-client-port').value=el('node-port').value;};
el('managed-nodes').onclick=async e=>{
 const b=e.target.closest('[data-node-action]');if(!b)return;const n=installed.find(n=>n.id===b.dataset.id);b.disabled=true;
 try{if(b.dataset.nodeAction==='copy'){await copy(n.uri,b);return;}
 if(b.dataset.nodeAction?.startsWith('upgrade-')){await api('/api/nodes/'+n.id+'/upgrade/'+b.dataset.nodeAction.slice(8),{});await load();return;}
 if(b.dataset.nodeAction==='repair-setup'){
 if(n.repair?.configured&&!confirm('重新生成会立即撤销旧巡检凭据，需重新在目标服务器安装。继续？'))return;
 const r=await api('/api/nodes/'+n.id+'/repair/setup',{});el('command').value=r.command;el('command-hostname').textContent='请在此节点服务器以 root 执行一次升级安装脚本。将安装自动升级组件，并自动运行完整节点安装流程；以后跟随面板发布版本自动升级。连续 3 次失败后重启原服务，5 分钟冷却，1 小时最多 3 次后暂停。仅支持本项目安装的协议服务；旧项目迁入节点需先迁移服务。';switchView('install',false);el('command-section').hidden=false;el('command-section').scrollIntoView({block:'nearest'});await load();return;}
 if(['repair-disable','repair-resume'].includes(b.dataset.nodeAction)){await api('/api/nodes/'+n.id+'/repair/'+b.dataset.nodeAction.slice(7),{});await load();return;}
 if(b.dataset.nodeAction==='delete'){if(confirm('删除节点“'+n.name+'”？将从面板及所有订阅中移除，并使相关安装和更新命令失效。巡检凭据也会失效。服务器上的服务、巡检任务、Cloudflare 隧道和 DNS 会保留，需另行清理。')){await api('/api/nodes/'+n.id,undefined,'DELETE');nodeOpened.delete(n.id);await load();}return;}
 if(b.dataset.nodeAction==='cancel'){if(confirm('仅在更新命令尚未执行时取消。若已在服务器执行，请先核对实际端口；取消不会回滚远端。继续？')){await api('/api/nodes/'+n.id+'/cancel-update',{});await load();}return;}
 editingNode=await api('/api/nodes/'+n.id);nodePortEdited=editingNode.client_port!==editingNode.port;
 el('node-name').value=editingNode.edit_name;el('node-port').value=editingNode.port;el('node-client-port').value=editingNode.client_port;el('node-address').value=editingNode.address;el('node-sni').value=editingNode.sni;el('node-sni').readOnly=editingNode.protocol==='argo';el('node-address').required=editingNode.protocol!=='argo';el('node-address-label').textContent=editingNode.protocol==='argo'?'优选域名 / IP（留空使用 www.cloudflare.com）':'公网连接地址 / DDNS';
 el('node-edit-hint').textContent=editingNode.protocol==='argo'?'优选地址不改变隧道域名与 SNI。监听端口修改需在服务器执行命令；自动隧道路由在成功回传后更新。':'客户端端口对应供应商开放或映射的端口。修改监听或 SNI 后，请在服务器执行更新命令；原认证信息保持不变。';el('node-error').hidden=true;el('node-dialog').showModal();
 }catch(x){error(x);}finally{if(b.isConnected)b.disabled=false;}
};
el('node-form').onsubmit=async e=>{
 e.preventDefault();el('node-save').disabled=true;el('node-error').hidden=true;
 try{const r=await api('/api/nodes/'+editingNode.id,{name:el('node-name').value.trim(),port:Number(el('node-port').value),client_port:Number(el('node-client-port').value),address:el('node-address').value.trim(),sni:el('node-sni').value.trim()},'PUT');
 el('node-dialog').close();if(r.pending){el('command').value=r.command;el('command-hostname').textContent=r.manual_route?'更新完成后还需在 Cloudflare 手动将隧道指向新监听端口。':'执行此命令更新原节点，成功回传后同步订阅。';switchView('install',false);el('command-section').hidden=false;el('command-section').scrollIntoView({block:'nearest'});}await load();
 }catch(x){el('node-error').textContent=x.message;el('node-error').hidden=false;}finally{el('node-save').disabled=false;}
};

const workspaceViews={install:['安装节点','选择协议，配置参数，生成安装命令。'],instances:['平台实例','管理 VPSNAT 实例、电源与端口映射。'],nodes:['节点管理','管理连接参数、巡检与自动升级。'],subscriptions:['订阅管理','选择节点，管理客户端订阅。'],results:['安装记录','查看安装进度与节点回传结果。']};
let currentView='install';
try{const saved=sessionStorage.getItem('auto-node-view');if(workspaceViews[saved])currentView=saved;}catch{}
function switchView(view,scroll=true){
 if(!workspaceViews[view])return;currentView=view;
 document.querySelectorAll('[data-panel-view]').forEach(p=>p.hidden=p.dataset.panelView!==view);
 document.querySelectorAll('[data-view]').forEach(b=>{const active=b.dataset.view===view;b.classList.toggle('active',active);if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
 el('workspace-title').textContent=workspaceViews[view][0];el('workspace-subtitle').textContent=workspaceViews[view][1];
 const detail={instances:'vpsnat-panel',nodes:'node-management',results:'installation-results'}[view];if(detail)el(detail).open=true;
 try{sessionStorage.setItem('auto-node-view',view);}catch{}
 if(scroll)window.scrollTo({top:0,behavior:'instant'});
}
el('workspace-nav').onclick=e=>{const b=e.target.closest('[data-view]');if(b)switchView(b.dataset.view);};

fields();api('/api/session').then(()=>show(true)).catch(()=>show(false));

// Platform lists are fetched on demand to avoid consuming gateway rate limits.
let platformVMs=[], platformVM=null, platformPorts=[], platformBusy=false;

const platformKeys=new Map();
try{for(const [k,v] of JSON.parse(sessionStorage.getItem('vpsnat-pending')||'[]')){if(typeof k==='string'&&typeof v?.key==='string'&&typeof v?.created==='number')platformKeys.set(k,v);}}catch{}
function savePlatformKeys(){sessionStorage.setItem('vpsnat-pending',JSON.stringify([...platformKeys]));}
async function loadPlatformStatus(){const s=await api('/api/vpsnat/status');el('vpsnat-status').textContent=s.configured?' · 密钥已配置':' · 未配置密钥';if(s.configured){try{await refreshPlatform();}catch(e){platformMessage(e.message);}}}
async function platformWrite(path,body,method='POST'){
 const signature=method+path+JSON.stringify(body||null);let pending=platformKeys.get(signature);
 if(pending&&Date.now()-pending.created>23*3600000)throw Error('原操作已超过安全重试期限，请先在平台核对结果，再刷新页面开始新操作');
 if(!pending){pending={key:crypto.randomUUID(),created:Date.now()};platformKeys.set(signature,pending);savePlatformKeys();}
 const r=await fetch(path,{method,headers:{'Content-Type':'application/json','Idempotency-Key':pending.key},body:body?JSON.stringify(body):undefined});
 let d;try{d=await r.json();}catch{throw Error('平台操作结果未知，请重试原操作');}
 if(!r.ok){if(r.status===401)show(false);if(r.status===409&&d.detail?.includes('结果未确认')){throw Error(d.detail+'；下一次操作前务必核对实际状态。');}throw Error(d.detail||'平台请求失败，请核对平台状态');}
 platformKeys.delete(signature);savePlatformKeys();return d;
}
function platformMessage(text){el('vpsnat-message').textContent=text;}
async function refreshPlatform(){
 platformVMs=await api('/api/vpsnat/vms');
 el('vpsnat-vms').innerHTML=platformVMs.length?platformVMs.map(v=>`<article class="result"><strong>${esc(v.name)} #${esc(v.id)}</strong><p class="hint">${esc(v.status)} · ${esc(v.public_ip||'公网地址待核对')}${v.expires_at?' · 到期 '+esc(v.expires_at):''}</p><button type="button" class="secondary" data-vm-id="${esc(v.id)}">选择实例 / 查看端口</button></article>`).join(''):'<p class="hint">平台未返回实例。</p>';
 platformMessage('已同步 '+platformVMs.length+' 个实例 · '+new Date().toLocaleTimeString());
}
function drawPlatformPorts(){el('vpsnat-forwards').innerHTML=platformPorts.length?platformPorts.map(p=>`<article class="result"><strong>${esc(p.protocol.toUpperCase())} ${esc(p.public_ip||platformVM.public_ip||'公网地址')} : ${p.public_port} → ${p.internal_port}</strong><div class="actions"><button type="button" class="secondary" data-port-id="${p.id}" data-port-action="install">用此映射配置 ${p.protocol==='udp'?'Hysteria2':'Reality'}</button><button type="button" class="secondary" data-port-id="${p.id}" data-port-action="change">更换公网端口</button><button type="button" class="secondary" data-port-id="${p.id}" data-port-action="delete">删除映射</button></div></article>`).join(''):'<p class="hint">暂无端口映射；Argo 无需业务端口映射。</p>';}
async function selectPlatformVM(id){
 const [v,ports]=await Promise.all([api('/api/vpsnat/vms/'+id),api('/api/vpsnat/vms/'+id+'/port-forwards')]);platformVM=v;platformPorts=ports;
 el('automation-start-one').disabled=false;el('vpsnat-title').textContent=v.name+' #'+v.id+' · '+v.status;el('vpsnat-detail').hidden=false;drawPlatformPorts();if(el('install-vm-id').value===id){installPlatformVM=v;installPlatformPorts=[...ports];fields();}
}
async function platformRun(button,work){if(platformBusy)return;platformBusy=true;button.disabled=true;try{await work();}catch(e){platformMessage(e.message);}finally{platformBusy=false;if(button.isConnected)button.disabled=false;}}
el('vpsnat-refresh').onclick=e=>platformRun(e.target,async()=>{await refreshPlatform();if(platformVM)await selectPlatformVM(platformVM.id);});
el('vpsnat-vms').onclick=e=>{const b=e.target.closest('[data-vm-id]');if(b)platformRun(b,()=>selectPlatformVM(b.dataset.vmId));};
function configurePlatformInstall(mapping){
 if(!platformVM)return;installPlatformVM=platformVM;installPlatformPorts=[...platformPorts];clearSSHSecrets();el('ssh-fingerprint').value='';el('ssh-fingerprint-status').textContent='尚未读取指纹';el('ssh-host').value=platformVM.public_ip||'';el('ssh-forward').innerHTML=platformPorts.filter(p=>p.protocol==='tcp'&&p.internal_port===22).map(p=>`<option value="${p.id}">TCP ${p.public_port} → 22</option>`).join('');el('deployment-mode').value=platformPorts.some(p=>p.protocol==='tcp'&&p.internal_port===22)?'ssh':'manual';el('install-vm-id').value=platformVM.id;el('install-forward-id').value=mapping?.id||'';
 el('protocol').value=mapping?(mapping.protocol==='udp'?'hysteria2':'reality'):'argo';
 el('install-form').elements.name.value=platformVM.name;clientEdited=!!mapping;
 el('platform-address-options').innerHTML=(platformVM.entry_candidates||[]).map(a=>`<option value="${esc(a)}"></option>`).join('');
 if(mapping){el('port').value=mapping.internal_port;el('client-port').value=mapping.public_port;el('address').value=mapping.public_ip||platformVM.public_ip||'';}else{el('address').value='';el('port').value=18080;}
 switchView('install',false);fields();el('install-vm-hint').textContent='目标：'+platformVM.name+' #'+platformVM.id+'。可生成命令，或填写 SSH 凭据自动安装。'+(mapping?'创建前会重新校验映射；更换映射后请重新选择。'+(!platformVM.public_ip?'平台返回内网地址，请在连接地址中填写实际入口；候选：'+(platformVM.entry_candidates||[]).join('、'):''):'Argo 不创建业务端口映射。');
 el('install-vm-hint').hidden=false;el('install-vm-clear').hidden=false;el('install-form').scrollIntoView({block:'start'});
}
el('install-vm-clear').onclick=()=>{el('install-vm-id').value='';el('install-forward-id').value='';el('install-vm-hint').hidden=true;el('install-vm-clear').hidden=true;installPlatformVM=null;installPlatformPorts=[];el('deployment-mode').value='manual';clearSSHSecrets();fields();};
el('vpsnat-detail').onclick=e=>{const b=e.target.closest('[data-vm-action]');if(!b||!platformVM)return;
 if(b.dataset.vmAction==='argo'){configurePlatformInstall();return;}
 platformRun(b,async()=>{const action=b.dataset.vmAction;if(!confirm('确认对实例 #'+platformVM.id+' 执行'+b.textContent+'？'))return;const d=await platformWrite('/api/vpsnat/vms/'+platformVM.id+'/actions/'+action);platformMessage(d.message);await selectPlatformVM(platformVM.id);});
};
el('vpsnat-forwards').onclick=e=>{const b=e.target.closest('[data-port-action]');if(!b)return;const p=platformPorts.find(p=>p.id===b.dataset.portId);if(!p)return;
 if(b.dataset.portAction==='install'){configurePlatformInstall(p);return;}
 platformRun(b,async()=>{const change=b.dataset.portAction==='change';if(!confirm((change?'更换':'删除')+'公网端口 '+p.public_port+'？已有 SSH 连接或直连节点可能中断；请在操作后核对节点客户端端口。'))return;
 await platformWrite('/api/vpsnat/vms/'+platformVM.id+'/port-forwards/'+p.id+(change?'/change':''),null,change?'POST':'DELETE');platformMessage('操作已提交；请核对新的映射及已有节点端口。');await selectPlatformVM(platformVM.id);});
};
el('vpsnat-forward-form').onsubmit=e=>{e.preventDefault();if(!platformVM)return;platformRun(e.submitter,async()=>{const d=Object.fromEntries(new FormData(e.target));d.internal_port=Number(d.internal_port);if(d.public_port)d.public_port=Number(d.public_port);else delete d.public_port;await platformWrite('/api/vpsnat/vms/'+platformVM.id+'/port-forwards',d);platformMessage('映射创建请求已提交。');await selectPlatformVM(platformVM.id);});};

function syncPlatformPorts(){
 const associated=!!el('install-vm-id').value,argo=el('protocol').value==='argo',select=el('platform-port-select');
 const ports=associated?installPlatformPorts.filter(p=>p.internal_port>=1024&&p.protocol===(el('protocol').value==='hysteria2'?'udp':'tcp')):[];
 const previous=el('install-forward-id').value;
 select.innerHTML=(argo?'<option value="">自定义本地端口（无需公网映射）</option>':'<option value="">请选择已创建的映射</option>')+ports.map(p=>`<option value="${p.id}">内部 ${p.internal_port} ← 公网 ${p.public_port} / ${p.protocol.toUpperCase()}</option>`).join('');
 const selected=ports.find(p=>p.id===previous);select.value=selected?selected.id:'';
 select.hidden=!associated;select.required=associated&&!argo;select.disabled=!associated;
 el('port').hidden=associated&&(!argo||!!selected);el('port').readOnly=associated&&!!selected;
 el('port').required=!associated||argo||!!selected;
 el('install-forward-id').value=selected?.id||'';
 el('client-port').readOnly=associated&&!argo;
 if(selected){el('port').value=selected.internal_port;if(!argo){clientEdited=true;el('client-port').value=selected.public_port;el('address').value=selected.public_ip||installPlatformVM?.public_ip||el('address').value;}}
 el('platform-port-hint').hidden=!associated;
 el('platform-port-hint').textContent=argo?'Argo 可选择已有 TCP 端口，也可使用无需映射的本地端口。':ports.length?'选择后自动同步内部监听端口与客户端公网端口。':'暂无兼容映射，请先到平台实例创建对应 TCP / UDP 业务映射（内部端口至少 1024）。';
}
el('platform-port-select').onchange=()=>{el('install-forward-id').value=el('platform-port-select').value;clientEdited=false;fields();};
function clearSSHSecrets(){for(const id of ['ssh-password','ssh-key','ssh-passphrase'])el(id).value='';}
function syncDeploymentFields(){
 const associated=!!el('install-vm-id').value,automatic=associated&&el('deployment-mode').value==='ssh',key=el('ssh-auth-mode').value==='key';
 el('deployment-mode-field').hidden=!associated;el('ssh-fields').hidden=!automatic;
 el('ssh-host').required=automatic;el('ssh-forward').required=automatic;
 el('ssh-password-field').hidden=key;el('ssh-key-field').hidden=!key;el('ssh-passphrase-field').hidden=!key;
 el('ssh-password').required=automatic&&!key;el('ssh-key').required=automatic&&key;
 el('generate').textContent=automatic?'开始自动安装':'生成安装命令';
}
el('deployment-mode').onchange=()=>{clearSSHSecrets();syncDeploymentFields();};
el('ssh-auth-mode').onchange=()=>{clearSSHSecrets();syncDeploymentFields();};
for(const id of ['ssh-host','ssh-forward'])el(id).addEventListener('change',()=>{el('ssh-fingerprint').value='';el('ssh-fingerprint-status').textContent='入口已改变，请重新读取指纹';});
el('ssh-probe').onclick=async()=>{const b=el('ssh-probe');b.disabled=true;try{const r=await api('/api/vpsnat/vms/'+el('install-vm-id').value+'/ssh-probe',{host:el('ssh-host').value.trim(),forward_id:el('ssh-forward').value});el('ssh-fingerprint').value=r.fingerprint;el('ssh-fingerprint-status').textContent='请与服务器控制台核对：'+r.fingerprint+'（SSH 公网端口 '+r.port+'）';}catch(e){el('ssh-fingerprint').value='';el('ssh-fingerprint-status').textContent=e.message;}finally{b.disabled=false;}};

function drawAutomation(batches){
 const running=batches.some(b=>b.status==='running');el('automation-start-all').disabled=running;el('automation-start-one').disabled=running||!platformVM;
 const states={queued:'排队中',running:'执行中',done:'已完成',failed:'未完成',unknown:'结果待核对'};
 el('automation-batches').innerHTML=batches.slice(0,3).map((b,i)=>`<details class="automation-batch" ${i===0?'open':''}><summary>${esc(new Date(b.created*1000).toLocaleString('zh-CN'))} · 起始 ${b.base_port} · ${b.status==='running'?'运行中':b.status==='finished'?'批次结束':'已中断'} · ${b.items.filter(x=>x.state==='done').length}/${b.items.length} 完成</summary><ol>${b.items.map(item=>`<li><strong>${esc(item.name)} #${esc(item.vm_id)}</strong><span class="status"> · ${esc(states[item.state]||item.state)}${item.port?' · 内部 '+item.port+' / 公网 '+item.public_port:''}</span><p class="hint">${esc(item.message||'等待处理')}${item.state==='done'&&!item.verified?' · 实际转发尚未验证':''}</p></li>`).join('')}</ol></details>`).join('');
}
async function startAutomation(button,ids){
 await platformRun(button,async()=>{const base=Number(el('automation-base-port').value);if(!Number.isInteger(base)||base<1024||base>65535)throw Error('起始端口需为 1024–65535');
 const result=await platformWrite('/api/vpsnat/automation/argo',{vm_ids:ids,base_port:base});
 el('automation-message').textContent='自动化批次已提交：'+result.id+'。请在下方查看进度，无需填写 SSH 密码。';await load();});
}
el('automation-start-all').onclick=e=>startAutomation(e.target,[]);
el('automation-start-one').onclick=e=>{if(platformVM)startAutomation(e.target,[platformVM.id]);};

function drawWatch(s){
 watchState=s;el('watch-toggle').textContent=s.enabled?'暂停每小时自动发现':'启用每小时自动发现';el('watch-check').disabled=!s.enabled||s.checking;
 const time=t=>t?new Date(t*1000).toLocaleString('zh-CN'):'尚未执行';
 el('watch-status').textContent=(s.enabled?'已启用，每小时检查一次':'已暂停')+' · 目标订阅：'+(s.subscription_name||'首次启用时使用 / 创建 CF')+' · 起始端口 '+s.base_port+'\n上次检查：'+time(s.last_check)+' · 下次检查：'+(s.enabled?time(s.next_check):'暂停中')+'\n'+(s.last_message||'首次启用后建立当前实例基线，后续新实例自动接入。');
 el('watch-error').textContent=s.last_error||'';el('watch-error').hidden=!s.last_error;
 const pending=s.instances.filter(x=>['pending','retry','blocked','installing'].includes(x.state));
 el('watch-pending').innerHTML=pending.length?'<details><summary>待处理实例（'+pending.length+'）</summary>'+pending.map(x=>`<p class="hint"><strong>${esc(x.name)} #${esc(x.vm_id)}</strong> · ${esc({pending:'等待实例运行 / 下一次巡检',retry:'下小时重试',blocked:'需要核对',installing:'自动安装中'}[x.state])} · 已尝试 ${x.attempts} 次${x.last_error?' · '+esc(x.last_error):''}</p>`).join('')+'</details>':'';
}
el('watch-toggle').onclick=e=>platformRun(e.target,async()=>{await api('/api/vpsnat/watch',{enabled:!watchState.enabled});await load();});
el('watch-check').onclick=e=>platformRun(e.target,async()=>{await api('/api/vpsnat/watch/check',{});await load();});
