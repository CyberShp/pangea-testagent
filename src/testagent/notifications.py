"""OS notifications are independent of the browser. No credentials in notification text."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading

LABELS={'task.failed':'任务失败','task.stopped':'任务已停止','approval.requested':'等待操作授权',
        'input.requested':'等待补充信息','recovery.proposed':'恢复方案待确认'}


class Notifications:
    def __init__(self,core,root,port):
        self.core,self.root,self.port=core,Path(root),port
        self.queue=queue.Queue();self.closed=False
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
        core.listeners.append(self.on_event)

    def on_event(self,task,kind):
        if kind in LABELS or kind=='task.state': self.queue.put((task,kind))

    def run(self):
        while True:
            item=self.queue.get()
            if item is None:return
            task,kind=item
            try:
                state=self.core.task(task)
                if kind=='task.state' and state['status']!='succeeded':continue
                title=LABELS.get(kind,'任务完成')
                if os.name!='nt':continue
                # Windows tray balloon with a click handler. Small isolated process; UI remains closed.
                payload=json.dumps({'title':title,'body':'请打开 testagent 查看任务结果或处理待办。',
                    'url':f'http://127.0.0.1:{self.port}/#tasks/{task}'},ensure_ascii=False)
                import base64
                data=base64.b64encode(payload.encode('utf-8')).decode()
                script="""Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')) | ConvertFrom-Json
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.Text = 'Pangea Testagent'
$n.BalloonTipTitle = $p.title
$n.BalloonTipText = $p.body
$script:target = $p.url
$n.add_BalloonTipClicked({ Start-Process $script:target })
$n.ShowBalloonTip(15000)
$t = [Diagnostics.Stopwatch]::StartNew()
while ($t.Elapsed.TotalSeconds -lt 20) { [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }
$n.Dispose()
""" % data
                encoded=base64.b64encode(script.encode('utf-16-le')).decode()
                subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-EncodedCommand',encoded],
                                 creationflags=subprocess.CREATE_NO_WINDOW,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except Exception:
                # Notification failure cannot change the actual task result.
                continue

    def close(self):
        self.closed=True;self.queue.put(None)
