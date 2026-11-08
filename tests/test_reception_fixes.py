import json,sys,tempfile,time,unittest,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Service,now,PREVIOUS_PROMPT
from test_core import FakeTransport
from test_transport import received
from feige_transport import FeigeTransport
from vendor.feige import ws_reader
from app import make_server
from playwright.sync_api import sync_playwright,expect

class ReceptionFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.inputs=[]
        self.s=Service(self.tmp.name,FakeTransport,lambda s,m:self.inputs.append(m) or '是的，这款是我们家的。');self.s.connect();self.s.connected_at=time.time()-10
    def tearDown(self):self.s.close();self.s.db.close();self.tmp.cleanup()
    def incoming(self,mid='one',text='这个是你家商品吧',**extra):
        self.s.on_message(dict(id=mid,conversation_id='talk',role='customer',text=text,timestamp=now(),shop_scope='shop_id:42',shop_scope_type='shop_id',buyer_id='buyer',buyer_id_type='security_sender_id',**extra))
    def test_unread_survives_reply_restart_and_read_race(self):
        self.incoming();through=self.s.conversation('live:talk')['read_through']
        self.s.on_message(dict(id='reply',conversation_id='talk',role='agent',text='是的',timestamp=now(),shop_scope='shop_id:42'))
        self.assertEqual(self.s.state()['conversations'][0]['unread'],1)
        self.incoming('two');self.s.mark_read({'conversation_id':'live:talk','through':through})
        self.assertEqual(self.s.state()['conversations'][0]['unread'],1)
        self.s.close();self.s.db.close();self.s=Service(self.tmp.name,FakeTransport);self.s.live()
        self.assertEqual(self.s.state()['conversations'][0]['unread'],1)
    def test_history_and_duplicate_do_not_alert(self):
        calls=[];self.s.on_inbound=lambda:calls.append(1)
        self.incoming('history',historical=True);self.incoming('one');self.incoming('one')
        self.assertEqual(calls,[1]);self.assertEqual(self.s.state()['conversations'][0]['unread'],1)
    def test_tracking_id_coalesces_update_frames_but_keeps_repeat_sends(self):
        transport=FeigeTransport(Path(self.tmp.name)/'bridge',self.s.on_message,lambda *_:None)
        transport._started_at=time.time()-2
        for i in range(6):
            m=ws_reader.extract_messages(received(talk='talk',message_id=700+i,buyer_id='buyer',shop_id='42',cid='client-one',text='[商品]',msg_type='template_card',goods_id='9000000000000000001',keywords='{"content":"抽绳垃圾袋"}',extra_kv={'b_temp_track_message_id':'8000000000000001'}))
            transport._accept_messages((1,'socket'),m)
        self.assertEqual(len(self.s.conversation('live:talk')['messages']),1)
        self.assertEqual(self.s.state()['conversations'][0]['unread'],1)
        self.assertIn('705',transport._sources['talk']['ids'])
        self.assertEqual(transport._sources['talk']['id'],'8000000000000001')
        # A deliberate second send of the same content has a different identity.
        m=ws_reader.extract_messages(received(talk='talk',message_id=900,buyer_id='buyer',shop_id='42',cid='client-two',text='[商品]',msg_type='template_card',goods_id='9000000000000000001',keywords='{"content":"抽绳垃圾袋"}',extra_kv={'b_temp_track_message_id':'8000000000000002'}))
        transport._accept_messages((1,'socket'),m)
        self.assertEqual(len(self.s.conversation('live:talk')['messages']),2)
    def test_existing_alias_reconciles_without_deleting_raw_record(self):
        self.incoming('wire',client_message_id='same');self.incoming('canonical',client_message_id='same',wire_message_id='wire')
        self.assertEqual(len(self.s.conversation('live:talk')['messages']),1)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM messages WHERE cid='live:talk'").fetchone()[0],2)
    def test_shop_identity_and_scoped_lessons_reach_model(self):
        product={'id':'p1','title':'抽绳垃圾袋','source':'customer_card'}
        self.incoming(product=product)
        self.s.lesson({'conversation_id':'live:talk','question':'这是你家商品吗','answer':'是的，这款抽绳垃圾袋是我们家的。','product_only':True})
        self.s.db.execute("INSERT INTO staff_lessons VALUES ('wrong-shop','shop_id:99','p1','这是你家商品吗','别家规则','now')")
        self.s.db.execute("INSERT INTO staff_lessons VALUES ('wrong-product','shop_id:42','p2','这是你家商品吗','其他商品规则','now')")
        self.s.transport.official_query=lambda section,body:{'scope':body['scope'],'shop_name':'演示商品店'}
        self.s.generate('live:talk');prompt=self.inputs[-1][0]['content']
        self.assertIn('演示商品店',prompt);self.assertIn('是的，这款抽绳垃圾袋是我们家的。',prompt)
        self.assertNotIn('别家规则',prompt);self.assertNotIn('其他商品规则',prompt)
        self.assertFalse(self.s.transport.calls)
    def test_same_text_distinct_messages_never_collapsed(self):
        self.incoming('first');self.incoming('second')
        self.assertEqual(len(self.s.conversation('live:talk')['messages']),2)

class ReceptionFixUITests(unittest.TestCase):
    def test_alert_survives_auto_reply_and_teaching_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Service(tmp,FakeTransport);s.connect()
            server=make_server(s);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True);page=browser.new_page(viewport={'width':1366,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{server.server_port}');page.wait_for_function('state !== null')
                    s.on_message(dict(id='one',conversation_id='talk',name='测试买家',role='customer',text='这个是你家商品吗',timestamp=now(),shop_scope='shop_id:42',shop_scope_type='shop_id',buyer_id='buyer',buyer_id_type='security_sender_id',product={'id':'p1','title':'抽绳垃圾袋','source':'customer_card'}))
                    s.on_message(dict(id='reply',conversation_id='talk',role='agent',text='原来冗长的回复',timestamp=now(),shop_scope='shop_id:42'))
                    page.evaluate('refresh()');expect(page.locator('#inboundAlert')).to_be_visible();expect(page.locator('#inboxReminder')).to_contain_text('1 条未读')
                    page.get_by_role('button',name='查看来信',exact=True).click();page.locator('#teachBtn').click();page.locator('#lessonAnswer').fill('是的，这款是我们家的。');page.get_by_role('button',name='记住这次纠正').click()
                    expect(page.locator('#lessonDialog')).to_contain_text('是的，这款是我们家的。')
                    self.assertEqual(s.db.execute('SELECT COUNT(*) FROM staff_lessons').fetchone()[0],1);self.assertFalse(s.transport.calls)
                    page.locator('#lessonDialog').get_by_role('button',name='关闭',exact=True).click();page.evaluate('refresh()')
                    expect(page.locator('#inboxReminder')).to_contain_text('暂无未读');self.assertFalse(errors,errors);browser.close()
            finally:server.shutdown();server.server_close();thread.join();s.close();s.db.close()

if __name__=='__main__':unittest.main()
