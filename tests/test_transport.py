import asyncio
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feige_transport import FeigeTransport, build_exact_frame, _im_channel_ready
from vendor.feige import ws_reader, ws_sender


def string(n, value):
    return n, 2, ('str', value)


def message(n, fields):
    return n, 2, ('msg', fields)


def kv(n, key, value):
    return message(n, [string(1, key), string(2, value)])


def sent(talk='123456789123456789', receiver='r' * 88, text='旧回复'):
    cid = '0b9b7fea-1df5-4e7e-a4e4-d0c771c01023'
    body = [string(4, text), string(8, cid), (14, 0, int(talk)),
            kv(5, 'security_receiver_id', receiver), kv(5, 's:client_message_id', cid)]
    return ws_sender.encode([message(8, [message(8, [message(100, body)])])])


def received(talk='123456789123456789', role='1', cid='inbound-id', text='你好', include_talk=True,
             timestamp_ms=None, message_id=92233720360000000, buyer_id='', shop_id='', fallback_uid='',
             msg_type='text', goods_id='', keywords='', extra_kv=None):
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    values = [(3, 0, message_id), string(8, text), (10, 0, timestamp_ms),
              kv(9, 'sender_role', role), kv(9, 'nickname', '客户甲'), kv(9, 'type', msg_type),
              kv(9, 'pigeon_cid', 'merchant-id'), kv(9, 's:client_message_id', cid)]
    if include_talk:
        values.append(kv(9, 'talk_id', talk))
    for key,value in (extra_kv or {}).items():
        values.append(kv(9,key,value))
    for key, value in [('security_sender_id',buyer_id),('shop_id',shop_id),('security_pigeon_uid',fallback_uid),
                       ('goods_id',goods_id),('generic_search_keywords',keywords)]:
        if value: values.append(kv(9,key,value))
    return ws_sender.encode([message(8, [message(6, [message(500, [message(5, values)])])])])


class FakePage:
    url = 'https://im.jinritemai.com/pc_seller_v2/main/workspace'

    def __init__(self, operation=None):
        self.operation = operation
        self.calls = 0

    async def evaluate(self, js, arg):
        if 'payload' not in arg:
            return {'recv': 1, 'sent': 1}
        self.calls += 1
        if self.operation:
            return self.operation(arg)
        return 'sent'


class FrameTests(unittest.TestCase):
    def test_first_attachment_without_text_preserves_buyer_and_message_identity(self):
        for role in ('1','2'):
            parsed=ws_reader.extract_messages(received(role=role,text='',msg_type='image',buyer_id='buyer',shop_id='shop'))
            self.assertEqual(len(parsed),1)
            self.assertEqual(parsed[0].sender_role,role)
            self.assertEqual(parsed[0].media,[{'kind':'image','url':''}])
            self.assertTrue(parsed[0].conversation_id)

    def test_attachment_metadata_urls_can_arrive_without_open_page(self):
        parsed=ws_reader.extract_messages(received(text='[图片]',msg_type='image',extra_kv={'image_info':'{"url":"https://p9-pigeon-sign.ecombdimg.com/photo.png"}'}))
        self.assertEqual(parsed[0].media[0]['url'],'https://p9-pigeon-sign.ecombdimg.com/photo.png')

    def test_empty_workspace_requires_actual_im_channel(self):
        url='https://im.jinritemai.com/pc_seller_v2/main/workspace'
        self.assertFalse(_im_channel_ready(url,{'open_sockets':2,'row_count':0}))
        self.assertFalse(_im_channel_ready(url,{'actual_im_socket_count':0,'row_count':5}))
        self.assertTrue(_im_channel_ready(url,{'actual_im_socket_count':1,'row_count':0}))
        self.assertFalse(_im_channel_ready(url.replace('/workspace','/login'),{'actual_im_socket_count':1}))
        self.assertFalse(_im_channel_ready(url.replace('im.jinritemai.com','evil.invalid'),{'actual_im_socket_count':1}))

    def test_customer_card_product_reaches_callback(self):
        messages=[]
        bridge=FeigeTransport(tempfile.gettempdir(),messages.append,lambda s:None)
        bridge._observe_received((1,'s'),received(msg_type='template_card',goods_id='123',
            keywords='{"content":"测试商品"}',text=''))
        self.assertEqual(messages[0]['product'],{'id':'123','title':'测试商品','source':'customer_card'})

    def test_product_card_id_only_and_title_only(self):
        for options in [{'goods_id':'123'},{'keywords':'{"content":"标题"}'}]:
            parsed=ws_reader.extract_messages(received(msg_type='template_card',text='',**options))
            self.assertEqual(parsed[0].product['source'],'customer_card')

    def test_non_customer_recommendation_or_text_has_no_product(self):
        for options in [{'role':'2','msg_type':'template_card'}, {'role':'4','msg_type':'template_card'},
                        {'role':'1','msg_type':'text'}]:
            parsed=ws_reader.extract_messages(received(goods_id='123',keywords='{"content":"推荐"}',**options))
            self.assertIsNone(parsed[0].product)

    def test_malformed_or_missing_product_fields_not_guessed(self):
        for raw in ['bad-json','{"content":{"foo":"bar"}}','[]','']:
            parsed=ws_reader.extract_messages(received(msg_type='template_card',keywords=raw))
            self.assertIsNone(parsed[0].product)

    def test_identity_same_buyer_across_talks_is_hash_evidence(self):
        messages=[]
        bridge=FeigeTransport(tempfile.gettempdir(),messages.append,lambda s:None)
        for talk in ['111','222']:
            bridge._observe_received((1,'socket'),received(talk=talk,buyer_id='private-buyer',shop_id='store'))
        self.assertEqual([m['buyer_id'] for m in messages],['private-buyer']*2)
        self.assertEqual(messages[0]['shop_scope'],'shop_id:store')
        identities=bridge.diagnostics()['identity_mappings']
        self.assertEqual(identities['111'],identities['222'])
        self.assertNotIn('private-buyer',str(identities))

    def test_agent_and_fallback_are_not_customer_identity(self):
        for raw in [received(role='2',buyer_id='agent'),received(fallback_uid='unverified')]:
            self.assertEqual(ws_reader.extract_messages(raw)[0].buyer_id,'')

    def test_identity_conflict_is_visible_and_scope_is_typed(self):
        bridge=FeigeTransport(tempfile.gettempdir(),lambda m:None,lambda s:None)
        for buyer in ['a','b']:
            bridge._observe_received((1,'socket'),received(buyer_id=buyer))
        entry=bridge.diagnostics()['identity_mappings']['123456789123456789']
        self.assertTrue(entry['conflict'])
        self.assertEqual(entry['observations'][0]['shop_scope_type'],'pigeon_cid')

    def test_same_conversation_and_receiver_preserved(self):
        raw = sent()
        new = build_exact_frame(raw, '123456789123456789', '新回复', 'fresh-uuid')
        self.assertEqual(ws_sender.sent_talk(new), '123456789123456789')
        self.assertEqual(ws_sender.frame_receiver_id(new), b'r' * 88)
        self.assertEqual(ws_sender.frame_text(new), '新回复')
        self.assertNotIn(b'0b9b7fea-1df5-4e7e-a4e4-d0c771c01023', new)
        self.assertEqual(new.count(b'fresh-uuid'), 2)

    def test_retarget_refused(self):
        with self.assertRaises(ValueError):
            build_exact_frame(sent(), '999999999', 'wrong recipient', 'fresh')

    def test_missing_talk_never_falls_back_to_merchant_id(self):
        rows = ws_reader.extract_messages(received(include_talk=False))
        self.assertTrue(rows)
        self.assertEqual(rows[0].conversation_id, '')

    def test_reader_emits_customer_and_agent(self):
        for role in ('1', '2'):
            row = ws_reader.extract_messages(received(role=role))[0]
            self.assertEqual(row.sender_role, role)
            self.assertEqual(row.conversation_id, '123456789123456789')

    def test_random_data_does_not_turn_into_message(self):
        self.assertEqual(ws_reader.extract_messages(b'not a protobuf frame'), [])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages=[]
        self.transport=FeigeTransport(tempfile.gettempdir(),self.messages.append,lambda s:None)
        self.talk='123456789123456789'
        self.page=FakePage()
        self.key=(id(self.page),'socket1')
        self.transport._pages[id(self.page)]=self.page
        self.source='92233720360000000'
        self.transport._observe_received(self.key,received())
        self.messages.clear()
        self.actions=[]
        self.commit_hook=None
        async def evaluate(js,arg):
            action=arg['action']; self.actions.append(action)
            if action=='commit':
                if self.commit_hook:
                    return self.commit_hook(arg)
                self.transport._observe_sent(self.page,self.key,'wss://example.invalid',sent(text=arg['text']))
                self.transport._observe_received(self.key,received(role='2',
                    cid='0b9b7fea-1df5-4e7e-a4e4-d0c771c01023',text=arg['text'],message_id=222))
                return {'ok':True,'submitted':True}
            if action=='probe': return {'row_count':0}
            return {'ok':True}
        self.page.evaluate=evaluate

    async def test_no_template_first_reply_succeeds(self):
        self.assertEqual(self.transport._routes,{})
        result=await self.transport._send(self.talk,'回复',self.source)
        self.assertEqual(result['status'],'confirmed')
        self.assertEqual(self.actions.count('commit'),1)
        self.assertTrue(self.messages[0]['own'])

    async def test_no_bootstrap_or_expiry_requirement(self):
        self.transport._routes[self.talk]={'at':time.monotonic()-100000}
        result=await self.transport._send(self.talk,'回复',self.source)
        self.assertEqual(result['status'],'confirmed')

    async def test_unknown_conversation_never_commits(self):
        self.assertEqual((await self.transport._send('different','回复'))['status'],'failed')
        self.assertNotIn('commit',self.actions)

    async def test_stale_source_never_commits(self):
        self.assertEqual((await self.transport._send(self.talk,'回复','old'))['status'],'failed')
        self.assertNotIn('commit',self.actions)

    async def test_manual_reply_never_commits(self):
        self.transport._last_turn[self.talk]=('agent','manual')
        self.assertEqual((await self.transport._send(self.talk,'回复',self.source))['status'],'failed')
        self.assertNotIn('commit',self.actions)

    async def test_source_rechecked_after_preparation(self):
        previous=self.page.evaluate
        async def evaluate(js,arg):
            if arg['action']=='prepare': self.transport._last_turn[self.talk]=('customer','new')
            return await previous(js,arg)
        self.page.evaluate=evaluate
        self.assertEqual((await self.transport._send(self.talk,'回复',self.source))['status'],'failed')
        self.assertNotIn('commit',self.actions)
        self.assertIn('abort',self.actions)

    async def test_commit_guard_refusal_is_definite_failure(self):
        self.commit_hook=lambda a:{'ok':False,'reason':'source_changed'}
        self.assertEqual((await self.transport._send(self.talk,'回复',self.source))['status'],'failed')
        self.assertEqual(self.actions.count('commit'),1)

    async def test_commit_exception_is_unknown_and_no_retry(self):
        def fail(a): raise RuntimeError('result unclear')
        self.commit_hook=fail
        self.assertEqual((await self.transport._send(self.talk,'回复',self.source))['status'],'unknown')
        self.assertEqual(self.actions.count('commit'),1)

    async def test_wrong_talk_echo_cannot_confirm(self):
        def wrong(arg):
            self.transport._observe_sent(self.page,self.key,'wss://example.invalid',sent(text=arg['text']))
            self.transport._observe_received(self.key,received(talk='888',role='2',
                cid='0b9b7fea-1df5-4e7e-a4e4-d0c771c01023',text=arg['text'],message_id=222))
            return {'ok':True}
        self.commit_hook=wrong
        with patch('feige_transport.CONFIRM_TIMEOUT',.01):
            self.assertEqual((await self.transport._send(self.talk,'回复',self.source))['status'],'unknown')
        self.assertEqual(self.actions.count('commit'),1)

    async def test_stop_blocks_send(self):
        self.transport._stop_requested.set()
        self.assertEqual((await self.transport._send(self.talk,'回复',self.source))['status'],'failed')
        self.assertNotIn('commit',self.actions)

    async def test_inbound_without_created_event_is_received(self):
        bridge=FeigeTransport(tempfile.gettempdir(),self.messages.append,lambda s:None)
        bridge._frame(self.page,{'requestId':'already-open','response':{
            'opcode':2,'payloadData':base64.b64encode(received()).decode()}},False)
        self.assertEqual(len(self.messages),1)

    async def test_manual_send_does_not_mix_clock_domains(self):
        self.transport._last_ts_ms[self.talk]=1234
        self.transport._observe_sent(self.page,self.key,'wss://example.invalid',sent())
        self.assertEqual(self.transport._last_ts_ms[self.talk],1234)
        self.assertEqual(self.transport._last_turn[self.talk][0],'agent')

    async def test_historical_frame_never_becomes_source(self):
        bridge=FeigeTransport(tempfile.gettempdir(),self.messages.append,lambda s:None)
        bridge._started_at=time.time()
        bridge._observe_received(self.key,received(timestamp_ms=int((time.time()-60)*1000)))
        self.assertFalse(bridge._last_turn)
        self.assertFalse(bridge._sources)
        self.assertTrue(self.messages[0]['historical'])

    async def test_unknown_timestamp_never_becomes_source(self):
        bridge=FeigeTransport(tempfile.gettempdir(),self.messages.append,lambda s:None)
        bridge._observe_received(self.key,received(timestamp_ms=0))
        self.assertFalse(bridge._last_turn)
        self.assertFalse(bridge._sources)

    async def test_old_frame_never_overwrites_new_turn(self):
        now=int(time.time()*1000)+100
        self.transport._observe_received(self.key,received(timestamp_ms=now,message_id=111))
        self.transport._observe_received(self.key,received(timestamp_ms=now-1000,message_id=222))
        self.assertEqual(self.transport._last_turn[self.talk],('customer','111'))
        self.assertEqual(self.transport._sources[self.talk]['id'],'111')
        self.assertEqual(len(self.messages),2)
        self.assertTrue(self.messages[-1]['historical'])

    async def test_duplicate_messages_are_emitted_once(self):
        raw=received(message_id=333)
        self.transport._observe_received(self.key,raw)
        self.transport._observe_received(self.key,raw)
        self.assertEqual(len(self.messages),1)

if __name__=='__main__': unittest.main()
