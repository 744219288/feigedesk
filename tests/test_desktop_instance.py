import json,sys,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from desktop_instance import publish_instance,clear_instance,wake_existing,show_desktop
from app import make_server
from test_core import FakeTransport
from core import Service

class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Service(self.tmp.name,FakeTransport)
        self.server=make_server(self.s);self.calls=[]
        self.server.activate_desktop=lambda:self.calls.append('show') or True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.path=publish_instance(self.tmp.name,self.server)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        self.s.close();self.s.db.close();self.tmp.cleanup()
    def test_second_launch_wakes_same_server_without_shop_send(self):
        self.assertTrue(wake_existing(self.tmp.name,attempts=1));self.assertEqual(self.calls,['show']);self.assertFalse(self.s.transport.calls)
    def test_wrong_token_and_cross_origin_do_not_activate(self):
        for token,origin in [('wrong',self.url),(self.server.activation_token,'https://evil.test')]:
            req=urllib.request.Request(self.url+'/api/activate',data=b'{}',headers={'Origin':origin,'X-Feige-Activation':token})
            with self.assertRaises(urllib.error.HTTPError) as e:self.opener.open(req)
            self.assertEqual(e.exception.code,403)
        self.assertEqual(self.calls,[])
    def test_not_ready_is_not_success(self):
        self.server.activate_desktop=lambda:False
        self.assertFalse(wake_existing(self.tmp.name,attempts=1))
    def test_runtime_validation_rejects_invalid_destination(self):
        self.path.write_text(json.dumps({'pid':1,'port':'https://evil.test','token':'x'*40}),encoding='utf-8')
        self.assertFalse(wake_existing(self.tmp.name,attempts=1))
    def test_old_process_cannot_delete_new_instance_descriptor(self):
        clear_instance(self.path,'other');self.assertTrue(self.path.exists())
        clear_instance(self.path,self.server.activation_token);self.assertFalse(self.path.exists())
    def test_hidden_window_uses_backend_without_waiting_for_shown(self):
        calls=[];gui=SimpleNamespace(show=lambda uid:calls.append(('show',uid)),restore=lambda uid:calls.append(('restore',uid)))
        self.assertFalse(show_desktop(SimpleNamespace(native=None,gui=gui)))
        self.assertTrue(show_desktop(SimpleNamespace(native=object(),gui=gui,uid='main')))
        self.assertEqual(calls,[('show','main'),('restore','main'),('show','main')])

if __name__=='__main__':unittest.main()
