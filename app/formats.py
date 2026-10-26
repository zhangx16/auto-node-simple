"""Export only the three protocol configurations produced by this installer."""
import json
from urllib.parse import urlsplit,urlunsplit,parse_qs,unquote
from uuid import UUID

def parse(uri,name=None):
 u=urlsplit(uri);q=parse_qs(u.query);get=lambda k,default='':q.get(k,[default])[0]
 if u.scheme not in ('vless','hysteria2') or not u.hostname or not u.port:raise ValueError('节点链接格式无效')
 p={'name':name or unquote(u.fragment) or '节点','server':u.hostname,'port':u.port,'credential':unquote(u.username or ''),'sni':get('sni',u.hostname),'insecure':get('insecure')=='1'}
 if u.scheme=='hysteria2':
  if not p['credential']:raise ValueError('缺少 Hysteria2 密码')
  p['kind']='hysteria2'
 else:
  UUID(p['credential'])
  if get('security')=='reality' and get('type')=='tcp' and get('pbk') and get('sid'):
   p.update(kind='reality',public_key=get('pbk'),short_id=get('sid'),fingerprint=get('fp','chrome'))
  elif get('security')=='tls' and get('type')=='ws':p.update(kind='argo',host=get('host',p['sni']),path=get('path','/'))
  else:raise ValueError('仅支持 Argo WS TLS、Reality Vision 和 Hysteria2')
 if p['kind']=='argo':
  parts=urlsplit(p['path']);opts=parse_qs(parts.query);value=opts.get('ed',['0'])[0]
  if not value.isdigit() or int(value)>65535:raise ValueError('WS Early Data 参数无效')
  p['early_data']=int(value)
  p['path']=urlunsplit(('', '',parts.path,'&'.join(x for x in parts.query.split('&') if x and not x.startswith('ed=')),parts.fragment))
 return p

def clash(p):
 n={'name':p['name'],'type':'hysteria2' if p['kind']=='hysteria2' else 'vless','server':p['server'],'port':p['port'],'udp':True}
 if p['kind']=='hysteria2':n.update(password=p['credential'],sni=p['sni'],alpn=['h3'],**{'skip-cert-verify':p['insecure']})
 else:
  n.update(uuid=p['credential'],tls=True,servername=p['sni'],**{'client-fingerprint':p.get('fingerprint','chrome')})
  if p['kind']=='reality':n.update(flow='xtls-rprx-vision',network='tcp',**{'reality-opts':{'public-key':p['public_key'],'short-id':p['short_id']}})
  else:
   n.update(network='ws',**{'ws-opts':{'path':p['path'],'headers':{'Host':p['host']}}})
   if p.get('early_data'):n['ws-opts'].update({'max-early-data':p['early_data'],'early-data-header-name':'Sec-WebSocket-Protocol'})
 return n

def singbox(p):
 n={'tag':p['name'],'type':'hysteria2' if p['kind']=='hysteria2' else 'vless','server':p['server'],'server_port':p['port'],'tls':{'enabled':True,'server_name':p['sni']}}
 if p['kind']=='hysteria2':n.update(password=p['credential']);n['tls'].update(insecure=p['insecure'],alpn=['h3'])
 else:
  n['uuid']=p['credential'];n['tls']['utls']={'enabled':True,'fingerprint':p.get('fingerprint','chrome')}
  if p['kind']=='reality':n.update(flow='xtls-rprx-vision');n['tls']['reality']={'enabled':True,'public_key':p['public_key'],'short_id':p['short_id']}
  else:
   n['transport']={'type':'ws','path':p['path'],'headers':{'Host':p['host']}}
   if p.get('early_data'):n['transport'].update(max_early_data=p['early_data'],early_data_header_name='Sec-WebSocket-Protocol')
 return n

def configuration(nodes,fmt):
 used={'DIRECT','节点选择','direct','proxy'};parsed=[]
 for n in nodes:
  p=parse(n['uri'],n['name']);name=p['name'];i=1
  while name in used:name=p['name']+' '+str(i);i+=1
  p['name']=name;used.add(name);parsed.append(p)
 names=[p['name'] for p in parsed]
 if fmt=='clash':
  # JSON is valid YAML 1.2; avoid adding a YAML dependency for generated configs.
  d={'mixed-port':7890,'allow-lan':False,'mode':'rule','log-level':'info','proxies':[clash(p) for p in parsed],'proxy-groups':[{'name':'节点选择','type':'select','proxies':names+['DIRECT']}],'rules':['MATCH,节点选择']}
 else:
  d={'log':{'level':'warn'},'dns':{'servers':[{'type':'https','tag':'dns-direct','server':'1.1.1.1'}]},'inbounds':[{'type':'mixed','tag':'mixed-in','listen':'127.0.0.1','listen_port':7890}], 'outbounds':[{'type':'selector','tag':'proxy','outbounds':names+['direct'],'default':names[0]}]+[singbox(p) for p in parsed]+[{'type':'direct','tag':'direct'}],'route':{'final':'proxy','auto_detect_interface':True,'default_domain_resolver':{'server':'dns-direct','strategy':'prefer_ipv4'}}}
 return json.dumps(d,ensure_ascii=False,indent=2)+'\n'
