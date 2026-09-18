"""A real loopback SSH/SFTP server for isolated integration tests."""
import os
from pathlib import Path
import socket
import subprocess
import threading
import paramiko


class SSHFixture:
    def __init__(self,root):
        self.root=Path(root);self.key=paramiko.RSAKey.generate(2048)
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen(8);self.sock.settimeout(.2)
        self.port=self.sock.getsockname()[1];self.closed=threading.Event();self.transports=[];self.processes=[]
        self.thread=threading.Thread(target=self.accept,daemon=True);self.thread.start()

    def accept(self):
        fixture=self
        class Server(paramiko.ServerInterface):
            def check_auth_password(self,username,password):return paramiko.AUTH_SUCCESSFUL if (username,password)==('tester','fixture-secret') else paramiko.AUTH_FAILED
            def get_allowed_auths(self,username):return 'password'
            def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_pty_request(self,*args):return True
            def check_channel_exec_request(self,channel,command):
                def execute():
                    process=subprocess.Popen(command.decode(),shell=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    fixture.processes.append(process)
                    def pump(pipe,send):
                        try:
                            while True:
                                data=os.read(pipe.fileno(),4096)
                                if not data:break
                                send(data)
                        except (OSError,EOFError):pass
                        finally:pipe.close()
                    out=threading.Thread(target=pump,args=(process.stdout,channel.sendall),daemon=True)
                    err=threading.Thread(target=pump,args=(process.stderr,channel.sendall_stderr),daemon=True)
                    out.start();err.start();code=process.wait();out.join();err.join()
                    try:channel.send_exit_status(code);channel.shutdown_write();channel.close()
                    except OSError:pass
                threading.Thread(target=execute,daemon=True).start();return True
            def check_channel_shell_request(self,channel):
                def shell():
                    view='device';buffer=''
                    try:
                        channel.sendall(b'device>')
                        while True:
                            data=channel.recv(4096)
                            if not data:break
                            buffer+=data.decode()
                            while '\n' in buffer:
                                line,buffer=buffer.split('\n',1)
                                if line=='diagnose':view='diagnose';out='diagnose>'
                                elif line=='show test-config' and view=='diagnose':out='flag=enabled\ndiagnose>'
                                elif line=='reboot':channel.close();return
                                elif line=='exit':view='device';out='device>'
                                else:out='unknown command\n'+view+'>'
                                channel.sendall(out.encode())
                    except OSError:pass
                threading.Thread(target=shell,daemon=True).start();return True
        class SFTP(paramiko.SFTPServerInterface):
            def path(self,path):
                target=(fixture.root/path.lstrip('/')).resolve()
                if not target.is_relative_to(fixture.root.resolve()):raise PermissionError()
                return target
            def stat(self,path):
                try:return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
                except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)
            lstat=stat
            def open(self,path,flags,attr):
                try:
                    fd=os.open(self.path(path),flags,0o600)
                    mode='r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb'
                    stream=os.fdopen(fd,mode);handle=paramiko.SFTPHandle(flags);handle.readfile=stream;handle.writefile=stream
                    return handle
                except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)
            def posix_rename(self,old,new):
                try:os.replace(self.path(old),self.path(new));return paramiko.SFTP_OK
                except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)
            rename=posix_rename
            def remove(self,path):
                try:self.path(path).unlink();return paramiko.SFTP_OK
                except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)
            def chattr(self,path,attr):
                try:
                    if attr.st_mode is not None:os.chmod(self.path(path),attr.st_mode)
                    return paramiko.SFTP_OK
                except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)
        while not self.closed.is_set():
            try:client,_=self.sock.accept()
            except socket.timeout:continue
            except OSError:break
            transport=paramiko.Transport(client);self.transports.append(transport)
            transport.add_server_key(self.key);transport.set_subsystem_handler('sftp',paramiko.SFTPServer,SFTP)
            try:transport.start_server(server=Server())
            except Exception:transport.close()

    def close(self):
        self.closed.set();self.sock.close()
        for t in self.transports:t.close()
        for p in self.processes:
            if p.poll() is None:p.kill()
        self.thread.join(1)
