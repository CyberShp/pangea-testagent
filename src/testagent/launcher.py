"""Portable entrypoint: background server, browser reopening, MCP subprocess routing."""
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser
from .paths import data_root


def main():
    if '--mcp' in sys.argv:
        from .bridge import main as bridge
        bridge();return
    if '--serve' in sys.argv:
        sys.argv.remove('--serve')
        from .server import main as serve
        serve();return
    root=data_root();root.mkdir(parents=True,exist_ok=True)
    port=8765
    url=f'http://127.0.0.1:{port}'
    try:
        with urlopen(url+'/api/health',timeout=1) as response:
            if response.status==200:webbrowser.open(url);return
    except Exception:pass
    log=(root/'service.log').open('ab')
    argv=[sys.executable,'--serve'] if getattr(sys,'frozen',False) else [sys.executable,'-m','testagent.server']
    flags=subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS if os.name=='nt' else 0
    child=subprocess.Popen(argv,stdout=log,stderr=log,stdin=subprocess.DEVNULL,creationflags=flags,start_new_session=os.name!='nt')
    for _ in range(100):
        if child.poll() is not None:break
        try:
            with urlopen(url+'/api/health',timeout=.5) as response:
                if response.status==200:webbrowser.open(url);return
        except Exception:time.sleep(.1)
    if os.name=='nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,'后台未启动，请查看 '+str(root/'service.log'),'Pangea Testagent',0x10)
    else:raise RuntimeError('后台未启动，请查看 '+str(root/'service.log'))

if __name__=='__main__':main()
