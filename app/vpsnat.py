"""VPSNAT server-side API adapter. Never sends credentials across redirects."""
import json
import ipaddress
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE = 'https://www.vpsnat.com/api/v1'
LIMIT = 2 * 1024 * 1024

class APIError(Exception):
    def __init__(self, message, status=502, code=None, port_range=None, http_status=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.port_range = port_range
        self.http_status = http_status

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def token():
    value = os.getenv('VPSNAT_API_KEY', '').strip()
    if not value:
        try:
            value = json.loads((Path(os.getenv('DATA_DIR', '/app/data')) / 'vpsnat.json').read_text()).get('api_key', '').strip()
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    if not re.fullmatch(r'vk_live_[A-Za-z0-9_-]+', value):
        raise APIError('未配置有效的 VPSNAT API Key', 503)
    return value

def status():
    try:
        token()
        return {'configured': True, 'base_url': BASE}
    except APIError:
        return {'configured': False, 'base_url': BASE}

def identifier(value):
    value = str(value)
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise APIError('平台实例或映射 ID 无效', 422)
    return value

def request(method, path, body=None, key=None):
    headers = {'Authorization': 'Bearer ' + token(), 'Accept': 'application/json', 'User-Agent': 'AutoNodeSimple/1.0', 'Content-Type': 'application/json'}
    if method != 'GET':
        if not key or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key):
            raise APIError('写操作需要 8–128 位 Idempotency-Key', 422)
        headers['Idempotency-Key'] = key
    req = urllib.request.Request(BASE + path, headers=headers, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=60 if path.endswith('/reset-password') else 20) as response:
            raw = response.read(LIMIT + 1)
            if method != 'GET' and not raw and response.status in (200, 202, 204):
                return {'success': True}
        if len(raw) > LIMIT:
            raise APIError('平台响应过大')
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Do not relay upstream response bodies: they may contain credentials.
        messages = {401: '平台密钥无效或已过期', 403: '平台密钥缺少所需权限', 404: '平台实例或映射不存在',
                    409: '平台操作冲突，请刷新状态', 422: '平台拒绝请求参数，请核对 API 文档', 429: '平台请求限流，请稍后重试'}
        code = None
        port_range = None
        try:
            problem = json.loads(exc.read(8192))
            if isinstance(problem, dict):
                candidate = problem.get('code')
                if isinstance(candidate, str) and re.fullmatch(r'[A-Z0-9_]{1,80}', candidate):code=candidate
                detail = str(problem.get('detail') or problem.get('message') or problem.get('error') or '')
                if code == 'PORT_OUT_OF_RANGE':
                    bounds = re.search(r'(\d{1,5})\s*[-–~至]\s*(\d{1,5})',detail)
                    if bounds and 1 <= int(bounds[1]) <= int(bounds[2]) <= 65535:port_range=(int(bounds[1]),int(bounds[2]))
                if exc.code in (400,409,422) and (re.search(r'port.{0,60}(already|in use|occupied|conflict|allocated|taken)',detail,re.I) or re.search(r'端口.{0,30}(占用|冲突|已存在|已被使用|重复)',detail)):
                    code='PORT_IN_USE'
        except (ValueError,OSError):pass
        raise APIError(('平台端口范围限制：%s–%s' % port_range) if port_range else messages.get(exc.code, '平台请求失败（HTTP %s）' % exc.code),
                       exc.code if exc.code in (404, 409, 422, 429) else 502, code, port_range, exc.code) from None
    except (OSError, ValueError):
        raise APIError('无法连接 VPSNAT 或响应格式无效；写操作结果可能未知，请使用原幂等键重试') from None
    if not isinstance(data, (dict, list)):
        raise APIError('平台响应格式无效')
    if isinstance(data, dict) and (data.get('success') is False or data.get('error')):
        raise APIError('平台未确认操作成功，请检查权限、实例状态和配额')
    return data

def collection(data, names):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for name in ('data', *names):
            if name in data:
                return collection(data[name], names)
    raise APIError('平台列表结构不匹配，需要核对实际 API 响应')

def object_data(data, name='vm'):
    if isinstance(data, dict):
        for key in ('data', name):
            if isinstance(data.get(key), dict):
                return object_data(data[key], name)
        return data
    raise APIError('平台详情结构不匹配')

def vm_view(row):
    if not isinstance(row, dict) or row.get('id') is None:
        raise APIError('平台实例缺少 ID')
    # Deliberately allowlist fields; never return root passwords or API keys.
    address = str(row.get('public_ip') or row.get('public_ipv4') or '')
    try:
        parsed = ipaddress.ip_address(address)
        if (parsed.version == 6 and parsed.is_private) or parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or any(parsed in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16') if parsed.version == 4):
            address = ''
    except ValueError:
        pass
    return {'id': identifier(row['id']), 'region': str(row.get('region') or ''), 'name': str(row.get('remark') or row.get('name') or row.get('hostname') or row['id']),
            'status': str(row.get('status', 'unknown')),
            'public_ip': address,
            'expires_at': str(row.get('expires_at') or row.get('expire_time') or '')}

def resolve_entries(rows):
    unresolved = [r for r in rows if not r['public_ip'] and r['region']]
    if not unresolved:
        return rows
    try:
        machines = collection(request('GET', '/machines/public'), ('machines', 'items'))
    except APIError:
        return rows
    for row in unresolved:
        candidates = sorted({str(m.get('entry_ip')) for m in machines
                             if isinstance(m, dict) and m.get('region') == row['region'] and m.get('entry_ip')})
        row['entry_candidates'] = candidates
        if len(candidates) == 1:
            row['public_ip'] = candidates[0]
    return rows

def vms():
    rows = []
    page = 1
    while page <= 100:
        data = request('GET', '/vms?page=%s&page_size=100' % page)
        batch = collection(data, ('vms', 'instances', 'items'))
        rows.extend(batch)
        total = data.get('total') if isinstance(data, dict) else None
        if total is None or not str(total).isdigit() or len(rows) >= int(total):
            return resolve_entries([vm_view(row) for row in rows])
        if not batch:
            raise APIError('平台分页未返回完整实例')
        page += 1
    raise APIError('平台实例列表超过分页上限')

def vm(ident):
    return resolve_entries([vm_view(object_data(request('GET', '/vms/' + identifier(ident))))])[0]

def forwards(ident):
    rows = collection(request('GET', '/vms/' + identifier(ident) + '/port-forwards'), ('port_forwards', 'forwards', 'items'))
    result = []
    for row in rows:
        try:
            internal, public = int(row.get('guest_port', row.get('internal_port'))), int(row.get('host_port', row.get('public_port')))
            protocol = str(row['protocol']).lower()
            if not (1 <= internal <= 65535 and 1 <= public <= 65535 and protocol in ('tcp', 'udp')):
                raise ValueError()
            result.append({'id': identifier(row['id']), 'internal_port': internal, 'public_port': public, 'protocol': protocol,
                           'public_ip': str(row.get('public_ip') or '')})
        except (KeyError, ValueError, TypeError):
            raise APIError('平台映射字段不匹配，需要核对实际 API 响应') from None
    return result


def mutate(method, path, body=None, key=None):
    """Persist deduplication locally; upstream creation currently ignores replay keys.

    A process crash or transport failure is deliberately not retried automatically:
    the operator must reconcile the platform state before issuing a new operation.
    """
    import hashlib
    import sqlite3
    import time
    if not key or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key):
        raise APIError('写操作需要 8–128 位 Idempotency-Key', 422)
    digest = hashlib.sha256(json.dumps([method, path, body], sort_keys=True).encode()).hexdigest()
    database = Path(os.getenv('DATA_DIR', '/app/data')) / 'vpsnat-operations.db'
    fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    with sqlite3.connect(database, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY,digest TEXT,status TEXT,result TEXT,created REAL)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT digest,status,result FROM operations WHERE key=?', (key,)).fetchone()
        if row:
            if row[0] != digest:
                raise APIError('此幂等键已用于其他操作', 409)
            if row[1] == 'done':
                return json.loads(row[2])
            if row[1] == 'rejected':
                error=json.loads(row[2])
                raise APIError(error['message'],error['status'],error.get('code'),error.get('port_range'),error.get('http_status'))
            raise APIError('此操作已提交但结果未确认；请先刷新平台核对结果，不会再次执行。确认后重新打开页面发起新操作', 409)
        db.execute('INSERT INTO operations VALUES (?,?,?,?,?)', (key,digest,'pending','',time.time()))
    try:
        result = request(method, path, body, key)
    except APIError as exc:
        if exc.http_status in (400,401,403,404,409,422,429):
            error={'message':str(exc),'status':exc.status,'code':exc.code,'port_range':exc.port_range,'http_status':exc.http_status}
            with sqlite3.connect(database,timeout=10) as db:
                db.execute("UPDATE operations SET status='rejected',result=? WHERE key=?",(json.dumps(error),key))
        raise
    with sqlite3.connect(database, timeout=10) as db:
        db.execute("UPDATE operations SET status='done',result=? WHERE key=?", (json.dumps(result),key))
    return result
