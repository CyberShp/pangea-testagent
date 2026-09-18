"""Task-scoped SSH transports, interactive views and tracked POSIX process groups."""
import base64
import hashlib
import json
import re
import shlex
import threading
import time
from pathlib import Path
import paramiko
from .core import DomainError, new_id


class Cancelled(Exception):
    pass


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


class SSHExecutor:
    def __init__(self, catalog, task, cancelled, emit):
        self.catalog, self.core, self.task = catalog, catalog.core, task
        self.cancelled, self.emit = cancelled, emit
        self.clients, self.sessions, self.operations = {}, {}, {}
        self.lock = threading.RLock()
        self.interactive_used = False

    def device(self, device):
        snapshot = self.core.task(self.task)['snapshot']
        if device not in snapshot['roles'].values():
            raise DomainError('设备不在任务范围')
        return snapshot['devices'][device]

    def connect(self, device, fresh=False, ignore_cancel=False):
        if self.cancelled.is_set() and not ignore_cancel:
            raise Cancelled('任务已停止')
        with self.lock:
            client = self.clients.get(device)
            if not fresh and client and client.get_transport() and client.get_transport().is_active():
                return client
        config = self.device(device)
        client = paramiko.SSHClient()
        # Host identities are pinned per device in the application DB. New devices use TOFU,
        # expose the fingerprint, and reject later changes including after a reboot.
        executor = self
        class PinPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                actual = fingerprint(key)
                with executor.core.tx():
                    row = executor.core.db.execute('SELECT fingerprint FROM device_settings WHERE id=?', (device,)).fetchone()
                    expected = (row[0] if row else '') or config.get('fingerprint', '')
                    if expected and expected != actual:
                        raise DomainError('SSH 主机指纹发生变化，请核对设备身份后更新指纹')
                    if row and not row[0]:
                        executor.core.db.execute('UPDATE device_settings SET fingerprint=? WHERE id=?', (actual,device))
                client.get_host_keys().add(hostname,key.get_name(),key)
        client.set_missing_host_key_policy(PinPolicy())
        try:
            client.connect(config['address'], port=config.get('port') or 22, username=config['username'],
                           password=self.catalog.vault.get(config['credential']), allow_agent=False, look_for_keys=False,
                           timeout=15, banner_timeout=15, auth_timeout=15)
        except paramiko.AuthenticationException as exc:
            raise DomainError('SSH 认证失败，请在设备管理中更新账号或密码后重新下发任务') from exc
        if not fresh:
            with self.lock:
                self.clients[device] = client
        return client

    def _collect(self, channel, timeout, *, ignore_cancel=False, output=True):
        deadline = time.monotonic() + timeout
        out, err = [], []
        total = 0
        while True:
            if self.cancelled.is_set() and not ignore_cancel:
                raise Cancelled('任务被用户中断')
            for ready, receive, dest, stream in ((channel.recv_ready, channel.recv, out,'stdout'),
                                                  (channel.recv_stderr_ready,channel.recv_stderr,err,'stderr')):
                while ready():
                    chunk = receive(32768).decode('utf-8', errors='replace')
                    total += len(chunk)
                    if total > 16*1024*1024:
                        raise DomainError('命令输出超过 16 MiB，请重定向到文件后下载')
                    dest.append(chunk)
                    if output:
                        self.emit('tool.output', {'stream':stream,'text':chunk})
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                return {'stdout':''.join(out),'stderr':''.join(err),'exit_code':channel.recv_exit_status(),'completion':'exit_status'}
            if time.monotonic() >= deadline:
                raise TimeoutError('远端命令超时')
            time.sleep(.03)

    def exec(self, device, command, timeout=300, operation_id=None):
        if not isinstance(command,str) or not command.strip():
            raise DomainError('命令不能为空')
        client = self.connect(device)
        op = operation_id or new_id()
        directory = '/tmp/testagent-' + self.task + '-' + op
        inner = 'umask 077; echo $$ > '+shlex.quote(directory+'/pid')+'; '+command+'\n'
        wrapper = ('umask 077; mkdir -p '+shlex.quote(directory)+' && '
                   'command -v setsid >/dev/null 2>&1 && '
                   'TESTAGENT_OPERATION='+shlex.quote(op)+' setsid sh -c '+shlex.quote(inner))
        handle = {'directory':directory,'operation_id':op,'kind':'process_group'}
        with self.core.tx():
            self.core.db.execute('INSERT OR REPLACE INTO process_handles VALUES(?,?,?,?,1)', (op,self.task,device,json.dumps(handle)))
        stdin, stdout, stderr = client.exec_command(wrapper)
        stdin.close()
        with self.lock:
            self.operations[op]=(device,handle,stdout.channel)
        try:
            result = self._collect(stdout.channel, float(timeout))
            # A detached/background child may still hold the group after the shell exits.
            alive = self._group_control(device, handle, kill=False)
            if alive != 'stopped':
                raise DomainError('命令已返回但进程组未确认退出，保留设备占用')
            with self.core.tx():
                self.core.db.execute('UPDATE process_handles SET active=0 WHERE id=?', (op,))
            if result['exit_code'] != 0:
                raise DomainError(f"远端命令失败，退出码 {result['exit_code']}：{result['stderr'][-2000:]}")
            return result
        finally:
            with self.lock:
                self.operations.pop(op,None)

    def _group_control(self, device, handle, kill=True):
        client = self.connect(device, fresh=True, ignore_cancel=True)
        try:
            directory=shlex.quote(handle['directory']); op=shlex.quote('TESTAGENT_OPERATION='+handle['operation_id'])
            # The marker guards PID reuse while the parent is alive. If only children remain,
            # uncertain identity is reported, never terminate unrelated processes.
            script=(f'd={directory}; if [ ! -s "$d/pid" ]; then echo unknown; exit; fi; '
                    'p=$(cat "$d/pid"); case "$p" in ""|*[!0-9]*) echo unknown; exit;; esac; '
                    'if ! /bin/kill -0 -- -"$p" 2>/dev/null; then echo stopped; exit; fi; '
                    f'if ! tr "\\000" "\\n" < "/proc/$p/environ" 2>/dev/null | grep -Fx -- {op} >/dev/null; then echo unknown; exit; fi; ')
            if kill:
                script += '/bin/kill -KILL -- -"$p" 2>/dev/null; '
            script += 'if /bin/kill -0 -- -"$p" 2>/dev/null; then echo active; else echo stopped; fi'
            _,out,_=client.exec_command(script,timeout=10)
            result=self._collect(out.channel,10,ignore_cancel=True,output=False)
            return result['stdout'].strip().splitlines()[-1] if result['stdout'].strip() else 'unknown'
        except Exception:
            return 'unknown'
        finally:
            client.close()

    def shell_open(self, device, timeout=30, expect=None):
        client=self.connect(device)
        channel=client.invoke_shell(width=160,height=48)
        ident=new_id()
        self.sessions[ident]=(device,channel)
        self.interactive_used=True
        result=self.shell_read(ident,timeout,expect)
        return {'session_id':ident,**result}

    def shell_read(self, session, timeout=30, expect=None, expect_disconnect=False):
        if session not in self.sessions:
            raise DomainError('交互会话不存在；重连后需重新进入诊断视图')
        channel=self.sessions[session][1]
        deadline=time.monotonic()+float(timeout)
        last=time.monotonic(); output=''
        pattern=re.compile(expect) if expect else None
        while time.monotonic()<deadline:
            if self.cancelled.is_set(): raise Cancelled('任务已停止')
            if channel.recv_ready():
                chunk=channel.recv(32768).decode('utf-8',errors='replace')
                output+=chunk; last=time.monotonic()
                self.emit('tool.output',{'stream':'terminal','text':chunk,'session_id':session})
                if len(output)>4*1024*1024: raise DomainError('终端输出超过限额')
                if pattern and pattern.search(output):
                    return {'output':output,'completion':'prompt_match'}
            elif channel.closed:
                self.sessions.pop(session,None)
                if expect_disconnect:
                    return {'output':output,'completion':'expected_disconnect','exit_code':None,'reboot_verified':False}
                raise DomainError('交互会话已断开，不能沿用原诊断视图')
            elif not pattern and not expect_disconnect and time.monotonic()-last>.5:
                return {'output':output,'completion':'idle_read','exit_code':None}
            time.sleep(.03)
        if pattern or expect_disconnect: raise TimeoutError('等待 Skill 指定提示符或预期断连超时')
        return {'output':output,'completion':'read_timeout','exit_code':None}

    def shell_send(self, device, session, text, expect, timeout=60, expect_disconnect=False):
        if session not in self.sessions or self.sessions[session][0]!=device:
            raise DomainError('终端会话不属于此设备')
        if not expect and not expect_disconnect: raise DomainError('交互命令必须提供 expect，预期重启时可设置 expect_disconnect')
        self.sessions[session][1].sendall(text+'\n')
        return self.shell_read(session,timeout,expect,expect_disconnect)

    def shell_close(self, device, session):
        if session not in self.sessions or self.sessions[session][0]!=device:
            raise DomainError('交互会话不存在或设备不匹配')
        self.sessions.pop(session)[1].close()
        return {'closed':True,'remote_process_termination':'not_asserted'}

    def read_file(self, device, path):
        with self.connect(device).open_sftp() as sftp:
            if sftp.stat(path).st_size>64*1024*1024: raise DomainError('远端文件超过 64 MiB')
            with sftp.open(path,'rb') as stream: return stream.read()

    def write_file(self, device, path, data):
        if not path.startswith('/'): raise DomainError('远端文件必须使用绝对路径')
        client=self.connect(device)
        with client.open_sftp() as sftp:
            previous=None; mode=None
            try:
                attrs=sftp.stat(path)
                if attrs.st_size>64*1024*1024: raise DomainError('原文件过大，不能建立恢复副本')
                mode=attrs.st_mode & 0o777
                with sftp.open(path,'rb') as stream: previous=stream.read()
            except FileNotFoundError: pass
            backup=None
            if previous is not None:
                backup=self.catalog.add_file(self.task,Path(path).name+'.before',previous,'backup')
            temp=path+'.testagent-'+new_id()
            try:
                with sftp.open(temp,'wb') as stream: stream.write(data)
                if self.cancelled.is_set(): raise Cancelled('文件写入被中断')
                if mode is not None: sftp.chmod(temp,mode)
                else: sftp.chmod(temp,0o600)
                try: sftp.posix_rename(temp,path)
                except OSError:
                    # No delete-then-rename: that could destroy the original on failure.
                    if previous is None: sftp.rename(temp,path)
                    else: raise DomainError('目标 SFTP 不支持原子覆盖；原文件保留，请用专用脚本处理')
            finally:
                try: sftp.remove(temp)
                except OSError: pass
        verified=self.read_file(device,path)==data
        if not verified: raise DomainError('文件回读与写入内容不一致')
        return {'path':path,'bytes':len(data),'verified':True,'backup_file_id':backup,'previously_existed':previous is not None}

    def wait_connected(self, device, timeout=300):
        with self.lock:
            previous=self.clients.pop(device,None)
            if previous: previous.close()
            for ident,(dev,ch) in list(self.sessions.items()):
                if dev==device: ch.close(); self.sessions.pop(ident,None)
        deadline=time.monotonic()+float(timeout)
        while time.monotonic()<deadline:
            if self.cancelled.is_set(): raise Cancelled('任务已停止')
            try:
                self.connect(device)
                return {'connected':True,'view':'new_session','note':'连接恢复不代表控制器重启已完成，需 Skill 继续验证'}
            except (paramiko.AuthenticationException,DomainError): raise
            except Exception:
                if self.cancelled.wait(1): raise Cancelled('任务已停止')
        raise TimeoutError('等待设备恢复连接超时')

    def stop(self):
        results=[]
        with self.core.lock:
            rows=list(self.core.db.execute('SELECT * FROM process_handles WHERE task_id=? AND active=1',(self.task,)))
        for row in rows:
            result=self._group_control(row['device_id'],json.loads(row['details']),kill=True)
            if result=='active':
                time.sleep(.15)
                result=self._group_control(row['device_id'],json.loads(row['details']),kill=False)
            if result=='stopped':
                with self.core.tx(): self.core.db.execute('UPDATE process_handles SET active=0 WHERE id=?',(row['id'],))
            results.append({'operation_id':row['id'],'result':result})
        interactive=self.interactive_used
        self.close()
        return {'processes':results,'confirmed':all(r['result']=='stopped' for r in results) and not interactive,
                'interactive': 'disconnected_not_confirmed_terminated' if interactive else 'none'}

    def close(self):
        with self.lock:
            for _,channel in self.sessions.values(): channel.close()
            for client in self.clients.values(): client.close()
            self.clients.clear(); self.sessions.clear()
