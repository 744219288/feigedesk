"""Wake the existing desktop without starting another store connection."""
import ctypes
import json
import os
import time
import urllib.request
from pathlib import Path

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def publish_instance(data_dir, server):
    path=Path(data_dir)/'desktop-instance.json'
    info={'pid':os.getpid(),'port':server.server_port,'token':server.activation_token}
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(info),encoding='utf-8')
    os.replace(temp,path)
    return path


def clear_instance(path, token):
    try:
        if json.loads(path.read_text(encoding='utf-8')).get('token')==token:path.unlink()
    except (OSError,ValueError):pass


def wake_existing(data_dir, attempts=12):
    path=Path(data_dir)/'desktop-instance.json'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    for attempt in range(attempts):
        try:
            if path.stat().st_size>4096:return False
            value=json.loads(path.read_text(encoding='utf-8'))
            port=value.get('port');pid=value.get('pid');token=value.get('token')
            if type(port)is not int or not 1<=port<=65535 or type(pid)is not int or pid<=0 or not isinstance(token,str) or not 32<=len(token)<=128:return False
            # The user launched this process; let the owning instance foreground itself.
            if os.name=='nt':ctypes.windll.user32.AllowSetForegroundWindow(pid)
            origin='http://127.0.0.1:'+str(port)
            req=urllib.request.Request(origin+'/api/activate',data=b'{}',headers={'Content-Type':'application/json','Origin':origin,'X-Feige-Activation':token})
            with opener.open(req,timeout=3) as response:
                result=json.loads(response.read(4096))
            if result.get('activated')is True:return True
        except (OSError,ValueError,TypeError):pass
        if attempt+1<attempts:time.sleep(.25)
    return False


def show_desktop(window):
    if window.native is None or window.gui is None:return False
    # Call the verified WinForms backend directly: Window.show's decorator waits
    # for 'shown', which a startup-hidden window may never have signalled.
    window.gui.show(window.uid)
    window.gui.restore(window.uid)
    window.gui.show(window.uid)
    return True


def show_when_loaded(window):
    if window.events.loaded.wait(20):show_desktop(window)
