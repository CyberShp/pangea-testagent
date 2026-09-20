"""Product CRUD and artifacts; no remote side effects."""
import base64
import csv
import io
import json
from pathlib import Path
import shutil
import zipfile
from .core import DomainError, new_id, now
from .paths import contained, safe_relative
from .vault import Vault

ACTIVE = ('queued', 'running', 'waiting_user', 'stopping')


class Catalog:
    def __init__(self, core, root):
        self.core, self.root = core, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.vault = Vault(self.root / 'credentials')
        self.core.redact = self.vault.redact
        self.core.sanitize = self.vault.redact_tree

    def task_dir(self, task):
        self.core.task(task)
        path = self.root / 'tasks' / task
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_device(self, value):
        name, address = str(value.get('name', '')).strip(), str(value.get('address', '')).strip().lower()
        username = str(value.get('username', '')).strip()
        port = int(value.get('port', 22))
        ident = value.get('id') or new_id()
        if not name or not address or not 1 <= port <= 65535 or (not address.startswith('sim://') and not username):
            raise DomainError('设备需要名称、地址、有效 SSH 端口与登录账号')
        if any(c in address for c in '\r\n\t ') or len(address) > 253:
            raise DomainError('设备地址无效')
        with self.core.tx():
            if self.core.db.execute('SELECT 1 FROM reservations WHERE device_id=?', (ident,)).fetchone():
                # Credentials can be corrected without changing target identity.
                old = self.core.db.execute('SELECT * FROM devices WHERE id=?', (ident,)).fetchone()
                settings = self.core.db.execute('SELECT * FROM device_settings WHERE id=?', (ident,)).fetchone()
                if not old or old['address'] != address or (settings and (settings['port'] != port or settings['username'] != username)):
                    raise DomainError('设备被占用时只能更新名称或密码，不能改变连接目标')
            duplicate = self.core.db.execute('SELECT id FROM devices WHERE address=? AND id<>?', (address, ident)).fetchone()
            if duplicate:
                raise DomainError('此地址已登记，请复用该设备')
            old = self.core.db.execute('SELECT credential,fingerprint FROM device_settings WHERE id=?', (ident,)).fetchone()
            credential = old['credential'] if old else 'device-' + ident
            fingerprint = str(value.get('fingerprint', old['fingerprint'] if old else ''))
            self.core.db.execute('INSERT INTO devices VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,address=excluded.address', (ident, name, address))
            self.core.db.execute('INSERT OR REPLACE INTO device_settings VALUES(?,?,?,?,?)', (ident, port, username, credential, fingerprint))
            if value.get('password'):
                self.vault.put(credential, value['password'])
        return ident

    def delete_device(self, ident):
        with self.core.tx():
            if self.core.db.execute('SELECT 1 FROM reservations WHERE device_id=?', (ident,)).fetchone():
                raise DomainError('设备正在被任务占用')
            for row in self.core.db.execute('SELECT roles FROM environments'):
                if ident in json.loads(row[0]).values():
                    raise DomainError('请先从环境组合中移除此设备')
            row = self.core.db.execute('SELECT credential FROM device_settings WHERE id=?', (ident,)).fetchone()
            self.core.db.execute('DELETE FROM device_settings WHERE id=?', (ident,))
            self.core.db.execute('DELETE FROM devices WHERE id=?', (ident,))
            # Keep referenced credentials for historical recovery; do not expose them.

    def save_environment(self, value):
        roles = value.get('roles')
        name, policy = value.get('name', '').strip(), value.get('policy', 'confirm')
        ident = value.get('id')
        if not ident:
            return self.core.environment(name, roles, policy)
        with self.core.tx():
            if not name or policy not in ('automatic', 'confirm') or not isinstance(roles, dict) or not roles:
                raise DomainError('环境名称、角色或权限策略无效')
            for role, device in roles.items():
                if not role or not self.core.db.execute('SELECT 1 FROM devices WHERE id=?', (device,)).fetchone():
                    raise DomainError('环境角色引用了不存在的设备')
            if not self.core.db.execute('SELECT 1 FROM environments WHERE id=?', (ident,)).fetchone():
                raise DomainError('环境不存在')
            self.core.db.execute('UPDATE environments SET name=?,policy=?,roles=? WHERE id=?', (name, policy, json.dumps(roles), ident))
        return ident

    def delete_environment(self, ident):
        with self.core.tx():
            for row in self.core.db.execute("SELECT snapshot FROM tasks WHERE status IN ('queued','running','waiting_user','stopping')"):
                if json.loads(row[0])['environment_id'] == ident:
                    raise DomainError('环境存在活动任务')
            self.core.db.execute('DELETE FROM environments WHERE id=?', (ident,))

    def save_profile(self, value):
        ident = value.get('id') or new_id()
        kind, name = value.get('kind'), value.get('name', '').strip()
        if kind not in ('openai', 'nga', 'opencode', 'codeagent') or not name:
            raise DomainError('后端名称或类型无效')
        config = dict(value.get('config', {}))
        if kind == 'openai':
            from urllib.parse import urlsplit
            url = urlsplit(config.get('base_url', ''))
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise DomainError('API 地址必须是 http(s) 服务根地址，不含账号密码或查询参数')
        else:
            if not config.get('command'):
                config['command'] = kind
            if not isinstance(config.get('args', ['acp']), list) or not all(isinstance(a, str) for a in config.get('args', [])):
                raise DomainError('启动参数必须是字符串数组')
        config.pop('api_key', None)
        config['credential'] = 'profile-' + ident
        with self.core.tx():
            self.core.db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)', (ident, name, kind, json.dumps(config)))
            if value.get('api_key'):
                self.vault.put(config['credential'], value['api_key'])
        return ident

    def profile(self, ident):
        with self.core.lock:
            row = self.core.db.execute('SELECT * FROM profiles WHERE id=?', (ident,)).fetchone()
            if not row:
                raise DomainError('后端不存在')
            return dict(row) | {'config': json.loads(row['config'])}

    def delete_profile(self, ident):
        with self.core.tx():
            for row in self.core.db.execute("SELECT snapshot FROM tasks WHERE status IN ('queued','running','waiting_user','stopping')"):
                if json.loads(row[0]).get('backend') == ident:
                    raise DomainError('此后端有活动任务')
            self.core.db.execute('DELETE FROM profiles WHERE id=?', (ident,))

    def state(self):
        with self.core.lock:
            state = self.core.overview()
            from .scenarios import catalog as scenario_catalog
            state['scenarios'] = scenario_catalog()
            roles = {}
            for row in self.core.db.execute('SELECT package FROM skills ORDER BY id,version'):
                package = json.loads(row['package'])
                contract = json.loads(package['files'].get('contract.json', '{}'))
                for role in contract.get('roles', []):
                    name = role.get('name')
                    name = name.strip() if isinstance(name, str) else ''
                    ident = role['id']
                    if ident not in roles or (not roles[ident]['name'] and name):
                        roles[ident] = {'id': ident, 'name': name}
            state['skill_roles'] = [roles[ident] for ident in sorted(roles)]
            state['devices'] = [dict(r) for r in self.core.db.execute('SELECT d.*,s.port,s.username,s.fingerprint,s.credential FROM devices d LEFT JOIN device_settings s ON d.id=s.id')]
            for device in state['devices']:
                device['has_password'] = bool(self.vault.get(device.pop('credential')))
            state['profiles'] = [dict(r) | {'config': json.loads(r['config'])} for r in self.core.db.execute('SELECT * FROM profiles')]
            for profile in state['profiles']:
                profile['has_key'] = bool(self.vault.get(profile['config'].pop('credential', '')))
            state['settings'] = {r[0]: json.loads(r[1]) for r in self.core.db.execute('SELECT * FROM settings')}
            return state

    def export_environments(self):
        state = self.state()
        return {'format': 'testagent-environments-v1', 'devices': [{k: v for k,v in d.items() if k not in ('has_password', 'fingerprint')} for d in state['devices']], 'environments': state['environments']}

    def import_environments(self, data):
        if data.get('format') != 'testagent-environments-v1':
            raise DomainError('不支持的环境文件格式')
        mapping = {}
        # Validate all references before starting writes.
        devices, environments = data.get('devices', []), data.get('environments', [])
        ids = {d['id'] for d in devices}
        for environment in environments:
            roles = environment['roles']
            if isinstance(roles, str):
                roles = json.loads(roles)
            if not set(roles.values()).issubset(ids):
                raise DomainError('环境导入文件存在无效设备引用')
        for device in devices:
            address = str(device['address']).strip().lower()
            with self.core.lock:
                existing = self.core.db.execute('SELECT id FROM devices WHERE address=?', (address,)).fetchone()
            mapping[device['id']] = existing[0] if existing else self.save_device({k: v for k,v in device.items() if k in ('name','address','username','port')})
        for environment in environments:
            roles = json.loads(environment['roles']) if isinstance(environment['roles'], str) else environment['roles']
            self.save_environment({'name': environment['name'], 'policy': environment['policy'], 'roles': {r: mapping[d] for r,d in roles.items()}})
        return {'devices': len(mapping), 'environments': len(environments)}

    def add_file(self, task, name, data, category='input'):
        safe_relative(name)
        if len(data) > 64 * 1024 * 1024:
            raise DomainError('单文件超过 64 MiB')
        ident = new_id()
        path = self.task_dir(task) / 'files' / ident
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        with self.core.tx():
            self.core.db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', (ident, task, name, category, len(data), now()))
            self.core.emit(task, 'file.added', {'id': ident, 'name': name, 'category': category, 'size': len(data)})
        return ident

    def file(self, task, ident):
        with self.core.lock:
            row = self.core.db.execute('SELECT * FROM artifacts WHERE id=? AND task_id=?', (ident,task)).fetchone()
            if not row:
                raise DomainError('文件不存在或不属于该任务')
        return dict(row), (self.task_dir(task) / 'files' / ident)

    def files(self, task):
        with self.core.lock:
            return [dict(r) for r in self.core.db.execute('SELECT * FROM artifacts WHERE task_id=? ORDER BY created', (task,))]

    def message(self, task, text):
        text = self.vault.redact(text.strip())
        if not text:
            raise DomainError('消息不能为空')
        with self.core.tx():
            if self.core.task(task)['status'] not in ACTIVE:
                raise DomainError('任务已结束，请新建任务')
            self.core.db.execute('INSERT INTO messages(task_id,text) VALUES(?,?)', (task,text))
            self.core.emit(task, 'user.message', {'text': text})
            changed=self.core.db.execute("UPDATE previews SET status='superseded' WHERE task_id=? AND status IN ('pending','approved')",(task,)).rowcount
            if changed:
                for row in self.core.db.execute("SELECT id FROM approvals WHERE task_id=? AND status IN ('pending','approved')",(task,)).fetchall():
                    self.core.db.execute("UPDATE approvals SET status='finished' WHERE id=?",(row['id'],))
                    self.core.emit(task,'operation.superseded',{'id':row['id'],'reason':'用户调整了要求，需要重新预览'})
                self.core.db.execute("UPDATE tasks SET status='running' WHERE id=? AND status='waiting_user'",(task,))
                self.core.emit(task,'preview.superseded',{'reason':'用户调整了要求，需要重新预览'})

    def drain_messages(self, task):
        with self.core.tx():
            rows = list(self.core.db.execute('SELECT seq,text FROM messages WHERE task_id=? AND consumed=0 ORDER BY seq', (task,)))
            self.core.db.execute('UPDATE messages SET consumed=1 WHERE task_id=?', (task,))
            return [r['text'] for r in rows]

    def delete_task(self, task):
        with self.core.tx():
            item = self.core.task(task)
            if item['status'] in ACTIVE or self.core.db.execute('SELECT 1 FROM reservations WHERE task_id=?', (task,)).fetchone():
                raise DomainError('活动或现场待处理任务不能删除')
            if self.core.db.execute("SELECT 1 FROM recovery WHERE task_id=? AND status IN ('planning','running','proposed')", (task,)).fetchone():
                raise DomainError('请先处理恢复方案')
            self.core.db.execute('DELETE FROM load_samples WHERE workload_id IN (SELECT id FROM workloads WHERE task_id=?)',(task,))
            for table in ('workloads','tuning','config_captures','previews','events','checks','steps','approvals','messages','artifacts','process_handles'):
                self.core.db.execute(f'DELETE FROM {table} WHERE task_id=?', (task,))
            self.core.db.execute('DELETE FROM recovery WHERE task_id=? OR child_id=?', (task,task))
            self.core.db.execute('DELETE FROM tasks WHERE id=?', (task,))
        shutil.rmtree(self.root / 'tasks' / task, ignore_errors=True)

    def release_scene(self, task):
        with self.core.tx():
            if self.core.task(task)['status'] in ACTIVE:
                raise DomainError('不能释放活动任务的设备')
            if self.core.db.execute("SELECT 1 FROM workloads WHERE task_id=? AND state NOT IN ('succeeded','failed','stopped')",(task,)).fetchone():raise DomainError('负载状态尚未核实，请先查询或停止负载')
            self.core.db.execute('DELETE FROM reservations WHERE task_id=?', (task,))
            self.core.db.execute("UPDATE tasks SET scene='user_released' WHERE id=?", (task,))
            self.core.emit(task, 'scene.released', {'reason': '用户确认已核对现场并释放占用；不代表自动恢复'})

    def report(self, task):
        item = self.core.task(task)
        events=[]; cursor=0
        while True:
            page=self.core.events(task,cursor)
            events.extend(page)
            if len(page)<1000: break
            cursor=page[-1]['seq']
        lines=['# '+item['title'], '', '任务：'+task, '状态：'+item['status'], '现场：'+item['scene'], '', '## 执行记录', '']
        for event in events:
            lines += [f"### {event['seq']} · {event['kind']} · {event['at']}", '', '```json', json.dumps(event['payload'],ensure_ascii=False,indent=2), '```','']
        stream=io.BytesIO()
        from .execution import comparisons
        changes=comparisons(self.core,task)
        plans=self.core.previews(task)
        lines += ['## 配置前后对比', '']
        for change in changes:
            lines += ['### '+change['name'], '角色：'+change['role'],
                      ('有配置差异' if change['changed'] else '采集结果一致') if change['available'] else '无法对比：缺少执行前或执行后采集结果', '']
            if change['available']:lines += ['```diff',change['diff'],'```','']
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
            with self.core.lock:
                jobs=[dict(r) for r in self.core.db.execute('SELECT * FROM workloads WHERE task_id=?',(task,))]
                for job in jobs:
                    rows=[json.loads(r[0]) for r in self.core.db.execute('SELECT sample FROM load_samples WHERE workload_id=? ORDER BY seq',(job['id'],))]
                    archive.writestr('workloads/'+job['name']+'.json',json.dumps(self.vault.redact_tree({'job':job,'samples':rows}),ensure_ascii=False))
                    raw=self.task_dir(task)/('load-'+job['id']+'.log')
                    if raw.exists():archive.write(raw,'workloads/'+job['name']+'.log')
                tuning=[dict(r) for r in self.core.db.execute('SELECT * FROM tuning WHERE task_id=?',(task,))]
                archive.writestr('tuning.json',json.dumps(self.vault.redact_tree(tuning),ensure_ascii=False))
            archive.writestr('previews.json',json.dumps(self.vault.redact_tree(plans),ensure_ascii=False,indent=2))
            archive.writestr('comparisons.json',json.dumps(self.vault.redact_tree(changes),ensure_ascii=False,indent=2))
            for index,change in enumerate(changes,1):
                for phase in ('before','after'):
                    if change[phase]:archive.writestr(f'comparisons/{index}/{phase}.txt',self.vault.redact(change[phase]['text']))
                if change['available']:archive.writestr(f'comparisons/{index}/change.diff',self.vault.redact(change['diff']))
            archive.writestr('report.md',self.vault.redact('\n'.join(lines)))
            archive.writestr('events.json',json.dumps(self.vault.redact_tree(events),ensure_ascii=False,indent=2))
            safe_task = dict(item)
            safe_task['snapshot'] = {k:v for k,v in item['snapshot'].items() if k not in ('profile','devices')}
            archive.writestr('task.json',json.dumps(self.vault.redact_tree(safe_task),ensure_ascii=False,indent=2))
            archive.writestr('files.json',json.dumps(self.files(task),ensure_ascii=False,indent=2))
        return stream.getvalue()

    def capture(self, task, operation, evidence, result):
        text=result.get({'exec':'stdout','shell_send':'output','remote_read':'text'}[operation['action']])
        if not isinstance(text,str) or len(text.encode('utf-8'))>2*1024*1024:
            raise DomainError('配置采集结果必须是 2 MiB 以内的文本')
        if operation['action']=='shell_send' and result.get('completion')!='prompt_match':
            raise DomainError('配置采集没有完成提示符，不能作为对比依据')
        with self.core.tx():
            row=self.core.db.execute("SELECT operation FROM approvals WHERE id=? AND task_id=? AND status='finished'",(evidence,task)).fetchone()
            if not row or json.loads(json.loads(row['operation'])['command'])!=operation:
                raise DomainError('配置采集与执行证据不一致')
            if self.core.db.execute('SELECT 1 FROM config_captures WHERE task_id=? AND comparison_id=? AND phase=?',(task,operation['capture_id'],operation['capture_phase'])).fetchone():
                raise DomainError('配置采集证据已经保存，不能覆盖')
            self.core.db.execute('INSERT INTO config_captures VALUES(?,?,?,?,?)',
                (task,operation['capture_id'],operation['capture_phase'],evidence,self.vault.redact(text)))
            self.core.emit(task,'config.captured',{'comparison_id':operation['capture_id'],'phase':operation['capture_phase'],'operation_id':evidence,'role':operation['role']})
