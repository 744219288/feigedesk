import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Service, now
from app import make_server


class FakeTransport:
    def __init__(self, path, on_message, on_status):
        self.on_message, self.on_status = on_message, on_status
        self.calls = []
        self.result = {'status':'confirmed', 'detail':'test'}
    def start(self): self.on_status({'state':'connected', 'detail':'test'})
    def stop(self): self.on_status({'state':'disconnected', 'detail':'test'})
    def send(self, cid, text, expected_source_id=None):
        self.calls.append((cid, text))
        return self.result


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Service(self.tmp.name, FakeTransport, lambda *_:'您好，请提供商品型号。')
    def tearDown(self):
        self.s.close()
        self.s.db.close()
        self.tmp.cleanup()
    def inbound(self, mid='1', cid='abc', role='customer', **kw):
        self.s.on_message(dict(id=mid,conversation_id=cid,name='同名客户',text='什么时候发货',timestamp=now(),role=role,**kw))
    def live(self):
        self.s.connect()
        self.inbound()
    def test_demo_cannot_send_live(self):
        result=self.s.send('demo:customer','你好','demo:welcome')
        self.assertEqual(result['delivery']['status'],'confirmed')
        self.assertFalse(self.s.transport.calls)
    def test_duplicate_inbound_is_idempotent(self):
        self.live(); self.inbound()
        self.assertEqual(len(self.s.conversation('live:abc')['messages']),1)

    def test_history_then_live_same_message_promotes_without_duplicate(self):
        self.s.connect()
        self.inbound(historical=True)
        self.assertFalse(self.s.db.execute("SELECT is_current FROM messages WHERE id='live:1'").fetchone()[0])
        self.inbound(historical=False)
        self.assertEqual(len(self.s.conversation('live:abc')['messages']),1)
        self.assertTrue(self.s.db.execute("SELECT is_current FROM messages WHERE id='live:1'").fetchone()[0])
        self.assertEqual(self.s.conversation('live:abc')['active_talk_id'],'abc')
    def test_same_name_isolated_by_id(self):
        self.live(); self.inbound('2','def')
        self.assertEqual(len(self.s.state()['conversations']),2)
        self.assertEqual(len(self.s.conversation('live:abc')['messages']),1)

    def buyer(self, mid, talk, buyer='buyer1', scope='shop1', stamp=None, **kw):
        self.s.on_message(dict(id=mid, conversation_id=talk, role='customer', text=mid,
                              name='同名客户', timestamp=stamp or now(), buyer_id=buyer,
                              buyer_id_type='security_sender_id', shop_scope=scope,
                              shop_scope_type='shop_id', **kw))

    def test_buyer_new_talk_history_and_real_send_route(self):
        self.s.connect()
        self.buyer('m1','old'); self.buyer('m2','new')
        self.assertEqual(len(self.s.state()['conversations']),1)
        data=self.s.conversation('live:old')
        self.assertEqual([m['id'] for m in data['messages']],['live:m1','live:m2'])
        self.assertEqual(data['active_talk_id'],'new')
        seen=[]
        self.s.transport.send=lambda cid,text,expected_source_id=None: seen.append((cid,expected_source_id)) or {'status':'confirmed'}
        self.s.send('live:old','reply','live:m2')
        self.assertEqual(seen,[('new','m2')])

    def test_buyer_aliases_survive_restart(self):
        self.s.connect(); self.buyer('m1','old'); self.buyer('m2','new')
        self.s.close(); self.s.db.close()
        self.s=Service(self.tmp.name,FakeTransport,lambda *_:'ok'); self.s.connect()
        self.buyer('m3','third')
        self.assertEqual(len(self.s.state()['conversations']),1)
        self.assertEqual(len(self.s.conversation('live:old')['messages']),3)

    def test_scoped_buyer_separation_and_same_talk(self):
        self.s.connect()
        self.buyer('m1','same',buyer='one'); self.buyer('m2','other',buyer='two')
        self.buyer('m3','same',buyer='one',scope='shop2')
        self.assertEqual(len(self.s.state()['conversations']),3)

    def test_unscoped_uid_never_merges(self):
        self.s.connect(); self.buyer('m1','old',scope=''); self.buyer('m2','new',scope='')
        self.assertEqual(len(self.s.state()['conversations']),2)

    def test_conflicting_buyer_on_established_alias_does_not_merge(self):
        self.s.connect(); self.buyer('m1','one',buyer='a'); self.buyer('m2','two',buyer='b')
        self.buyer('bad','one',buyer='b')
        self.assertEqual(len(self.s.state()['conversations']),2)
        self.assertEqual(len(self.s.conversation('live:one')['messages']),1)
        self.assertEqual(len(self.s.conversation('live:two')['messages']),1)

    def test_retired_route_cannot_reverse_even_with_new_timestamp(self):
        self.s.connect(); self.buyer('m1','old'); self.buyer('m2','new'); self.buyer('late','old')
        self.assertEqual(self.s._latest('live:old')['id'],'live:m2')
        self.assertEqual(self.s.conversation('live:old')['active_talk_id'],'new')
        self.assertEqual(len(self.s.conversation('live:old')['messages']),3)

    def test_explicit_merge_requires_evidence_preserves_human_and_watermark(self):
        self.s.connect(); self.inbound('a','old'); self.inbound('b','new')
        self.s.human('live:old',True)
        with self.assertRaises(ValueError): self.s.merge_verified_conversations('live:old','live:new')
        data=self.s.merge_verified_conversations('live:old','live:new',evidence='用户明确确认同人')
        self.assertTrue(data['human']); self.assertEqual(data['active_talk_id'],'new')
        self.inbound('late','old')
        self.assertEqual(self.s._latest('live:old')['id'],'live:b')
        self.assertEqual(len(self.s.state()['conversations']),1)
        self.assertEqual(len(self.s.conversation('live:old')['messages']),3)

    def test_legacy_merge_identity_anchor_survives_restart_and_fourth_talk(self):
        self.s.connect()
        self.inbound('m1','first'); self.inbound('m2','second')
        self.s.human('live:first',True)
        self.s.merge_verified_conversations('live:first','live:second',evidence='用户确认旧两次接待属于同一买家')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM buyer_identities').fetchone()[0],0)
        self.buyer('m3','third',scope='shop_id:900000001')
        self.assertEqual(len(self.s.state()['conversations']),2)
        self.s.merge_verified_conversations('live:first','live:third',scope='shop_id:900000001',evidence='合成测试证据确认属于同一买家')
        identity=self.s.db.execute('SELECT * FROM buyer_identities').fetchone()
        self.assertEqual(identity['cid'],'live:first')
        self.assertEqual(identity['scope'],'shop_id:900000001')
        self.assertEqual(self.s.conversation('live:first')['active_talk_id'],'third')
        self.s.close(); self.s.db.close()
        self.s=Service(self.tmp.name,FakeTransport,lambda *_:'ok'); self.s.connect()
        self.buyer('m4','fourth',scope='shop_id:900000001')
        data=self.s.conversation('live:first')
        self.assertEqual(len(self.s.state()['conversations']),1)
        self.assertEqual([m['id'] for m in data['messages']],['live:m1','live:m2','live:m3','live:m4'])
        self.assertEqual(data['active_talk_id'],'fourth')
        self.assertTrue(data['human'])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM talk_aliases WHERE cid=?',('live:first',)).fetchone()[0],4)
        seen=[]
        self.s.transport.send=lambda cid,text,expected_source_id=None: seen.append((cid,expected_source_id)) or {'status':'confirmed'}
        self.s.send('live:first','人工回复','live:m4')
        self.assertEqual(seen,[('fourth','m4')])
        self.assertTrue(self.s.conversation('live:first')['human'])

    def test_product_context_persists_and_is_bound_to_current_talk(self):
        self.s.connect()
        product={'id':'123456','title':'蓝色水杯','source':'customer_card','price':'fabricated'}
        self.buyer('card','old',product=product); self.buyer('question','old')
        context=self.s.conversation('live:old')['product_context']
        self.assertEqual(context['id'],'123456'); self.assertEqual(context['message_id'],'live:card')
        self.assertEqual(context['talk_id'],'old'); self.assertNotIn('price',context)
        self.s.close(); self.s.db.close()
        self.s=Service(self.tmp.name,FakeTransport,lambda *_:'ok'); self.s.connect()
        self.assertEqual(self.s.conversation('live:old')['product_context']['id'],'123456')
        self.buyer('new-question','new')
        data=self.s.conversation('live:old')
        self.assertIsNone(data['product_context'])
        self.assertEqual(data['messages'][0]['product']['title'],'蓝色水杯')

    def test_product_context_excludes_agent_invalid_source_and_other_buyer(self):
        self.s.connect()
        product={'id':'123456','title':'蓝色水杯','source':'customer_card'}
        self.buyer('card','one',buyer='a',product=product)
        self.buyer('other','two',buyer='b')
        self.assertIsNone(self.s.conversation('live:two')['product_context'])
        self.inbound('agent','two',role='agent',product=product)
        self.assertIsNone(self.s.conversation('live:two')['product_context'])
        self.buyer('fake','two',buyer='b',product={**product,'source':'entry_source'})
        self.assertIsNone(self.s.conversation('live:two')['product_context'])

    def test_product_merge_preserves_cards_but_uses_latest_route(self):
        self.s.connect()
        self.inbound('card','old',product={'id':'123','title':'旧杯','source':'customer_card'})
        self.inbound('new','new')
        self.s.merge_verified_conversations('live:old','live:new',evidence='用户明确确认')
        self.assertIsNone(self.s.conversation('live:old')['product_context'])
        self.inbound('late-card','old',product={'id':'456','title':'迟到卡','source':'customer_card'})
        self.assertIsNone(self.s.conversation('live:old')['product_context'])
        self.inbound('new-card','new',product={'id':'789','title':'当前卡','source':'customer_card'})
        data=self.s.conversation('live:old')
        self.assertEqual(data['product_context']['id'],'789')
        self.assertEqual(sum(bool(m['product']) for m in data['messages']),3)

    def test_product_generation_data_and_knowledge_priority(self):
        self.s.connect()
        for i in range(6): self.s.knowledge({'title':'什么时候发货'+str(i),'content':'通用说明'})
        self.s.knowledge({'title':'商品 123456','content':'这款杯子材质是玻璃'})
        self.buyer('card','one',product={'id':'123456','title':'蓝色水杯','source':'customer_card'})
        captured=[]
        self.s.model_call=lambda settings,messages: captured.extend(messages) or 'ok'
        self.s.generate('live:one')
        system=captured[0]['content']
        self.assertIn('这款杯子材质是玻璃',system)
        self.assertIn('customer_card',system); self.assertIn('历史咨询商品',system)
        self.assertIn('不代表进店来源',system); self.assertIn('live:card',system)
    def test_mode_guard(self):
        self.live(); self.s.demo()
        with self.assertRaises(ValueError): self.s.send('live:abc','hello','live:1')
        self.assertFalse(self.s.transport.calls)
    def test_stale_message_rejected(self):
        self.live(); self.inbound('2')
        with self.assertRaises(ValueError): self.s.send('live:abc','hello','live:1')
        self.assertFalse(self.s.transport.calls)
    def test_live_send_route(self):
        self.live(); self.s.send('live:abc','hello','live:1')
        self.assertEqual(self.s.transport.calls,[('abc','hello')])
    def test_unknown_not_retried(self):
        self.live(); self.s.transport.result={'status':'unknown','detail':'no echo'}
        self.s.send('live:abc','hello','live:1')
        with self.assertRaises(ValueError): self.s.send('live:abc','hello','live:1')
        self.assertEqual(len(self.s.transport.calls),1)
    def test_model_result_invalidated_by_new_message(self):
        self.live()
        def call(*_):
            self.inbound('2'); return 'stale reply'
        self.s.model_call=call
        with self.assertRaises(ValueError): self.s.generate('live:abc')
        self.assertIsNone(self.s.conversation('live:abc')['draft'])
    def test_knowledge_change_invalidates_generation(self):
        self.live()
        def call(*_):
            self.s.knowledge({'title':'更新','content':'次日发货'}); return 'stale reply'
        self.s.model_call=call
        with self.assertRaises(ValueError): self.s.generate('live:abc')
    def test_handoff_draft_does_not_send_until_auto_runs(self):
        self.live(); self.s.model_call=lambda *_:'[转人工] 退款需人工核实'
        data=self.s.generate('live:abc')
        self.assertEqual(data['draft']['status'],'ready')
        self.assertEqual(data['assistance']['status'],'pending')
        self.assertFalse(data['human'])
        self.assertFalse(self.s.transport.calls)
    def test_auto_handoff_sends_receipt_then_flags_human(self):
        self.live(); self.s.model_call=lambda *_:'[转人工] 退款需人工核实'
        self.s.settings['auto_reply']=True
        self.s._auto('live:abc','live:1',self.s.revision)
        self.assertEqual(len(self.s.transport.calls),1)
        self.assertNotIn('[转人工]',self.s.transport.calls[0][1])
        data=self.s.conversation('live:abc')
        self.assertFalse(data['human'])
        self.assertEqual(data['assistance']['status'],'pending')
        self.assertEqual(data['draft']['status'],'confirmed')
        self.s._auto('live:abc','live:1',self.s.revision)
        self.assertEqual(len(self.s.transport.calls),1)
    def test_failed_handoff_receipt_still_flags_human(self):
        self.live(); self.s.model_call=lambda *_:'请问您希望处理哪个订单？[请求人工]'
        self.s.settings['auto_reply']=True
        self.s.transport.result={'status':'failed','detail':'未发送'}
        self.s._auto('live:abc','live:1',self.s.revision)
        self.assertFalse(self.s.conversation('live:abc')['human'])
        self.assertEqual(self.s.conversation('live:abc')['assistance']['status'],'pending')
        self.assertEqual(self.s.conversation('live:abc')['draft']['status'],'failed')
    def test_handoff_cannot_send_for_superseded_customer_message(self):
        self.live(); self.s.model_call=lambda *_:'请问您希望处理哪个订单？[请求人工]'
        self.s.settings['auto_reply']=True
        d=self.s.generate('live:abc')['draft']
        self.inbound('2')
        with self.assertRaises(ValueError):
            self.s.send('live:abc',d['text'],'live:1',automatic=True,generation=d['generation'])
        self.assertFalse(self.s.transport.calls)
    def test_handoff_respects_operator_takeover_before_send(self):
        self.live(); self.s.model_call=lambda *_:'请问您希望处理哪个订单？[请求人工]'
        self.s.settings['auto_reply']=True
        d=self.s.generate('live:abc')['draft']
        self.s.human('live:abc',True)
        with self.assertRaises(ValueError):
            self.s.send('live:abc',d['text'],'live:1',automatic=True,generation=d['generation'])
        self.assertFalse(self.s.transport.calls)
    def test_agent_message_does_not_replace_explicit_takeover(self):
        self.live(); self.inbound('2',role='agent')
        self.assertFalse(self.s.conversation('live:abc')['human'])
    def test_own_echo_does_not_take_over(self):
        self.live(); self.inbound('2',role='agent',own=True)
        self.assertFalse(self.s.conversation('live:abc')['human'])
    def test_auto_requires_connection_and_model(self):
        with self.assertRaises(ValueError): self.s.update_settings({'auto_reply':True})
        self.assertFalse(self.s.settings['auto_reply'])
    def test_human_cancels_auto(self):
        self.live(); self.s.update_settings({'model':'test','auto_reply':True})
        self.s.generate('live:abc'); self.s.human('live:abc',True)
        with self.assertRaises(ValueError): self.s.send('live:abc','reply','live:1',automatic=True)
        self.assertFalse(self.s.transport.calls)
    def test_historical_never_queued(self):
        self.live(); self.s.update_settings({'model':'test','auto_reply':True})
        self.inbound('2',historical=True)
        self.assertFalse(self.s.timers)
    def test_delayed_history_cannot_replace_live_auto_source(self):
        self.live(); self.s.update_settings({'model':'test','auto_reply':True})
        self.inbound('old',historical=True)
        self.assertEqual(self.s._latest('live:abc')['id'],'live:1')
        self.assertIn('live:old', [m['id'] for m in self.s.conversation('live:abc')['messages']])
        self.s._auto('live:abc','live:1',self.s.revision)
        self.assertEqual(len(self.s.transport.calls),1)
    def test_settings_do_not_expose_secret(self):
        self.s.update_settings({'api_key':'secret-local-test-key'})
        encoded=json.dumps(self.s.state())
        self.assertNotIn('secret-local-test-key',encoded)
        self.assertNotIn('secret-local-test-key',self.s.db.execute("SELECT v FROM config WHERE k='settings'").fetchone()[0])
        self.assertTrue(self.s.state()['settings']['has_key'])
    def test_external_plain_http_rejected(self):
        with self.assertRaises(ValueError): self.s.update_settings({'base_url':'http://example.com/v1'})
    def test_restart_restores_auto_and_connection(self):
        self.live(); self.s.update_settings({'model':'test','auto_reply':True})
        second=Service(self.tmp.name,FakeTransport)
        self.assertTrue(second.settings['auto_reply'])
        self.assertEqual(second.mode,'live')
        second.close(); second.db.close()

    def test_assistance_request_keeps_answering_until_operator_accepts(self):
        self.live()
        self.s.settings.update(model='test',auto_reply=True)
        self.s.model_call=lambda *_:'图中上方是垃圾袋，您问的是这一款吗？[请求人工]'
        self.s._auto('live:abc','live:1',self.s.revision)
        self.assertFalse(self.s.conversation('live:abc')['human'])
        self.assertIn('垃圾袋', self.s.transport.calls[0][1])
        self.inbound('2')
        self.s._auto('live:abc','live:2',self.s.revision)
        self.assertEqual(len(self.s.transport.calls),2)
        self.s.human('live:abc',True)
        self.assertEqual(self.s.conversation('live:abc')['assistance']['status'],'accepted')
        self.inbound('3')
        self.s._auto('live:abc','live:3',self.s.revision)
        self.assertEqual(len(self.s.transport.calls),2)

    def test_model_failure_requests_help_without_taking_over(self):
        self.live(); self.s.settings['auto_reply']=True
        def fail(*_): raise ValueError('model error')
        self.s.model_call=fail
        self.s._auto('live:abc','live:1',self.s.revision)
        self.assertFalse(self.s.conversation('live:abc')['human'])
        self.assertEqual(self.s.conversation('live:abc')['assistance']['status'],'pending')
        self.s.model_call=lambda *_:'后续消息可以正常回答'
        self.inbound('2'); self.s._auto('live:abc','live:2',self.s.revision)
        self.assertEqual(len(self.s.transport.calls),1)

    def test_pause_and_connection_changes_preserve_preference(self):
        self.live(); self.s.update_settings({'model':'test','auto_reply':True})
        self.s.disconnect(); self.assertTrue(self.s.settings['auto_reply'])
        self.s.demo(); self.assertTrue(self.s.settings['auto_reply'])
        self.s.connect(); self.assertTrue(self.s.settings['auto_reply'])
        self.s.update_settings({'auto_reply':False})
        second=Service(self.tmp.name,FakeTransport)
        self.assertFalse(second.settings['auto_reply'])
        self.assertEqual(second.mode,'demo')
        second.close();second.db.close()

    def test_new_model_configuration_enables_auto(self):
        self.s.update_settings({'model':'test'})
        self.assertTrue(self.s.settings['auto_reply'])
    def test_human_during_send_is_not_cleared(self):
        self.live(); self.s.update_settings({'model':'test','auto_reply':True})
        draft=self.s.generate('live:abc')['draft']
        def send(*a, **kw):
            self.inbound('manual',role='agent')
            self.s.human('live:abc',True)
            return {'status':'confirmed','detail':'test'}
        self.s.transport.send=send
        self.s.send('live:abc',draft['text'],'live:1',automatic=True,generation=draft['generation'])
        self.assertTrue(self.s.conversation('live:abc')['human'])
    def test_mode_aba_discards_old_generation(self):
        self.live()
        def call(*_):
            self.s.demo(); self.s.live(); return 'old'
        self.s.model_call=call
        with self.assertRaises(ValueError): self.s.generate('live:abc')
    def test_old_generation_cannot_borrow_new_draft(self):
        self.live(); self.s.update_settings({'model':'test','auto_reply':True})
        old=self.s.generate('live:abc')['draft']
        self.s.knowledge({'title':'new','content':'new facts'})
        self.s.generate('live:abc')
        with self.assertRaises(ValueError):
            self.s.send('live:abc',old['text'],old['source_message_id'],automatic=True,generation=old['generation'])
        self.assertFalse(self.s.transport.calls)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(self.tmp.name,FakeTransport)
        self.server=make_server(self.s)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.s.close(); self.s.db.close(); self.tmp.cleanup()
    def test_requires_request_token(self):
        req=urllib.request.Request(self.url+'/api/demo',data=b'{}',headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code,403)
    def test_cross_origin_rejected_even_with_token(self):
        with urllib.request.urlopen(self.url+'/api/state') as r: state=json.load(r)
        req=urllib.request.Request(self.url+'/api/demo',data=b'{}',headers={'X-Desk-Token':state['token'],'Origin':'https://evil.test'})
        with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code,403)
    def test_same_origin_mutation(self):
        with urllib.request.urlopen(self.url+'/api/state') as r: state=json.load(r)
        req=urllib.request.Request(self.url+'/api/demo_message',data=json.dumps({'text':'测试问题'}).encode(),headers={'X-Desk-Token':state['token'],'Origin':self.url})
        with urllib.request.urlopen(req) as r: result=json.load(r)
        self.assertEqual(result['messages'][-1]['text'],'测试问题')
        self.assertFalse(self.s.transport.calls)


if __name__=='__main__': unittest.main()
