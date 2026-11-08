import asyncio,base64,copy,hashlib,io,json,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from outbound_assets import OutboundAssets
from commerce_data import product_rows,stable_knowledge,coupon_rows
from aftersale_data import query_tree,merge_details
from native_sender import match_card_request,observe_card_result
from core import Service,now
from inbox_journal import InboxJournal
from feige_transport import FeigeTransport
from vendor.feige import ws_sender
from test_transport import string,message,kv,received,FakePage
from test_core import FakeTransport
from test_official_services import Request
from official_services import OfficialServices,OfficialError,PATHS

class AssetTests(unittest.TestCase):
    def test_file_validation_and_integrity(self):
        with tempfile.TemporaryDirectory() as d:
            store=OutboundAssets(d);out=io.BytesIO();Image.new('RGB',(2,2)).save(out,format='PNG')
            encoded=base64.b64encode(out.getvalue()).decode()
            for name in ('../photo.png','C:\\photo.png','x.exe','x.jpg','x\n.png'):
                with self.assertRaises(ValueError):store.upload({'name':name,'data':encoded})
            for name,data in [('x.pdf',b'%PDF-truncated'),('x.mp4',b'not really mp4')]:
                with self.assertRaises(ValueError):store.upload({'name':name,'data':base64.b64encode(data).decode()})
            meta=store.upload({'name':'photo.png','data':encoded})
            self.assertEqual(store.read(meta['id'])[1],out.getvalue())
            (store.directory/(meta['id']+'.png')).write_bytes(b'changed')
            with self.assertRaises(ValueError):store.read(meta['id'])
            with self.assertRaises(ValueError):store.read('../other')

class CommerceTests(unittest.TestCase):
    def product(self):
        return {'product_item':{'shop_id':'42','product_id':'10','product_base_info':{'product_id':'10','title':'bags',
          'property_value_pair':[{'Property':{'PropertyName':'quantity'},'Values':[{'ValueName':'20'}]}],
          'skus':[{'id':'11','product_id':'10','sale_property_value_pair':[{'Property':{'PropertyName':'spec'},'Value':{'ValueName':'50'}}]}]},
          'sku_info':{'sku_stock_info':{'11':{'sku_id':11,'normal_stock_num_sum':8}},'sku_marketing_info':{'11':{'sku_id':11,'show_sku_info':{'show_price':{'show_price':{'show_amount':'99.00'}}}}}}}}
    def test_dynamic_values_excluded_and_conflicting_spec_preserved(self):
        p=product_rows([self.product()],'42')[0];k=stable_knowledge(p)
        self.assertEqual(p['skus'][0]['display_price'],'99.00');self.assertEqual(p['skus'][0]['stock'],8)
        self.assertNotIn('stock',json.dumps(k));self.assertNotIn('99.00',json.dumps(k))
        self.assertEqual(k['properties'][0]['values'],['20']);self.assertEqual(k['skus'][0]['specs'][0]['value'],'50')
    def test_wrong_shop_product_or_sku_rejected(self):
        for path in [('shop_id',),('product_base_info','product_id'),('sku_info','sku_marketing_info','11','sku_id')]:
            raw=self.product();v=raw['product_item']
            for key in path[:-1]:v=v[key]
            v[path[-1]]='999'
            with self.assertRaises(ValueError):product_rows([raw],'42')
    def test_coupon_shop_and_invalid_money(self):
        data={'shop_meta_list':[{'shop_id':42,'coupon_meta_id':'5','credit':True,'threshold':-1}]}
        r=coupon_rows(data,'42')[0];self.assertIsNone(r['credit_fen']);self.assertIsNone(r['threshold_fen'])
        with self.assertRaises(ValueError):coupon_rows(data,'43')

class IdentityTests(unittest.TestCase):
    def test_verified_identity_persists_and_conflicts_stay_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            j=InboxJournal(d);i={'talk':'1','scope':'shop_id:42','buyer':'buyer'};j.remember_identity(i)
            self.assertEqual(InboxJournal(d).identities()['1'],i)
            j.remember_identity({**i,'buyer':'other'});j.remember_identity(i)
            self.assertIsNone(j.identities()['1'])
    def test_outbound_idempotency_and_restart_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            s=Service(d,FakeTransport);s.connect()
            s.on_message({'id':'1','conversation_id':'100','text':'hi','role':'customer','timestamp':now(),'buyer_id':'b','buyer_id_type':'security_sender_id','shop_scope':'shop_id:42','shop_scope_type':'shop_id'})
            req={'conversation_id':'live:100','request_id':'a'*32,'kind':'coupon','coupon_id':'7'}
            with patch('native_sender.send_native',return_value={'status':'accepted','detail':'ok'}) as call:
                first=s.manual_outbound(req);second=s.manual_outbound(req)
                self.assertEqual(first,second);self.assertEqual(call.call_count,1)
                with self.assertRaises(ValueError):s.manual_outbound({**req,'coupon_id':'8'})
            self.assertTrue(s.conversation('live:100')['human'])
            s.db.execute("UPDATE outbound_jobs SET status='sending'");s.db.commit();s.close();s.db.close()
            s=Service(d,FakeTransport)
            self.assertEqual(s.db.execute('SELECT status FROM outbound_jobs').fetchone()[0],'unknown');s.close();s.db.close()
    def test_new_talk_echo_requires_exact_buyer_shop_socket_kind(self):
        with tempfile.TemporaryDirectory() as d:
            got=[];t=FeigeTransport(d,got.append,lambda _:None);p=FakePage();socket=(id(p),'ws');cid='uuid'
            def outgoing(buyer='buyer',shop='42'):
                body=[string(4,'[视频]'),string(8,cid),(14,0,200),kv(5,'security_receiver_id',buyer),kv(5,'shop_id',shop),kv(5,'type','video')]
                return ws_sender.encode([message(8,[message(8,[message(100,body)])])])
            pending={'kind':'video','talk':'100','scope':'shop_id:42','buyer':'buyer','page_id':id(p),'event':asyncio.Event(),'armed':True,'socket':None}
            t._native_pending=pending
            for raw in (outgoing('wrong'),outgoing(shop='43')):t._observe_sent(p,socket,'wss://ws.fxg.jinritemai.com',raw)
            self.assertNotIn('client_id',pending)
            t._observe_sent(p,socket,'wss://ws.fxg.jinritemai.com',outgoing())
            self.assertEqual(pending['talk'],'200')
            t._observe_received((id(p),'wrong'),received(talk='200',role='2',cid=cid,text='[视频]',msg_type='video',shop_id='42'))
            self.assertFalse(pending['event'].is_set())
            t._observe_received(socket,received(talk='200',role='2',cid=cid,text='[视频]',msg_type='video',shop_id='43'))
            self.assertFalse(pending['event'].is_set())
            t._observe_received(socket,received(talk='200',role='2',cid=cid,text='[视频]',msg_type='video',shop_id='42'))
            self.assertTrue(pending['event'].is_set());self.assertEqual(t._journal.identities()['200']['buyer'],'buyer')
            self.assertEqual(got[-1]['buyer_id_type'],'verified_outbound_receiver')

class CardTests(unittest.IsolatedAsyncioTestCase):
    def request(self,**updates):
        body={'card_type':'pdf_upload_card','data_id':'md5','security_conversation_id':'official-conv',
              'server_ext':json.dumps({'file_md5':'md5','file_name':'test.pdf','file_size':'10'})};body.update(updates)
        return SimpleNamespace(url='https://pigeon.jinritemai.com/backstage/sendCard',method='POST',post_data_json=body)
    def pending(self):
        return {'meta':{'kind':'file','md5':'md5','name':'test.pdf','size':10},'buyer':'buyer','proof':{},'principal':'42:staff','event':asyncio.Event()}
    async def test_wrong_content_or_origin_cannot_confirm(self):
        p=self.pending();self.assertTrue(match_card_request(p,self.request()))
        self.assertFalse(match_card_request(p,self.request(data_id='wrong')))
        req=self.request();req.url='https://evil.test/backstage/sendCard';self.assertFalse(match_card_request(p,req))
        p['meta']={'kind':'coupon','id':'7'}
        self.assertFalse(match_card_request(p,self.request(card_type='kefu_coupon',security_user_id='other',extra='{"coupon_meta_id":"7"}')))
    async def test_http_success_is_accepted_only_with_same_operation_and_proof(self):
        p=self.pending();p['http_request']=self.request()
        class Response:
            status=200;request=p['http_request']
            async def body(self):return b'{"code":0,"data":{"success":true}}'
        class Page:
            ok=True
            async def evaluate(self,*a):return {'ok':self.ok}
        page=Page();t=SimpleNamespace(_native_pending=p,_official=SimpleNamespace(principal='42:staff'))
        page.ok=False;await observe_card_result(t,page,Response(),p);self.assertFalse(p['event'].is_set())
        page.ok=True;t._official.principal='43:other';await observe_card_result(t,page,Response(),p);self.assertFalse(p['event'].is_set())
        t._official.principal='42:staff';await observe_card_result(t,page,Response(),p);self.assertTrue(p['http_accepted'])

class AftersaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.request=Request();self.s=OfficialServices(SimpleNamespace(request=self.request));self.i={'scope':'shop_id:42','buyer':'b','talk':'t'}
        self.order={'order_id':'10','order_status':3,'order_biz_type':2,'sku_order_list':[{'sku_order_id':'11','after_sale_orders':[{'after_sale_id':'12','after_sale_status':1}]}]}
        self.detail={'info_list':[{'shop_order_id':'10','info_list':[{'sku_order_id':'11','info_list':[{'after_sale_id':'12','after_sale_status_desc':'待处理','private':'secret','buttons':[{'action_key':'aftersale_refund_refund_agree','action_type':8,'button_name':'同意退款'}]}]}]}]}
        self.request.override.update(orders={'code':0,'data':[self.order]},aftersales={'code':0,'data':self.detail},refund_precheck={'code':0,'data':{'need_notice':True,'notice_text':'read only','secret':'private'}})
    async def test_order_chain_and_precheck_never_execute_refund(self):
        r=await self.s.customer({'action':'aftersales'},self.i);self.assertEqual(r['items'][0]['after_sale_id'],'12');self.assertNotIn('secret',json.dumps(r))
        result=await self.s.customer({'action':'refund_precheck','after_sale_id':'12','action_key':'aftersale_refund_refund_agree','action_type':999},self.i)
        self.assertFalse(result['executed']);call=next(c for c in self.request.calls if c[1]=='refund_precheck')
        self.assertEqual(call[2]['params'],{'after_sale_id_list':'12','shop_order_id':'10','action_type':8})
        self.assertNotIn('confirm_refund',PATHS)
    async def test_empty_orders_no_invented_after_sale_request(self):
        self.request.override['orders']={'code':0,'data':[]}
        r=await self.s.customer({'action':'aftersales'},self.i);self.assertEqual(r['items'],[])
        self.assertNotIn('aftersales',[c[1] for c in self.request.calls])
    async def test_cross_order_details_rejected(self):
        self.detail['info_list'][0]['shop_order_id']='999'
        with self.assertRaises(ValueError):await self.s.customer({'action':'aftersales'},self.i)
    async def test_unbound_stale_or_other_buyer_precheck_blocked(self):
        query={'action':'refund_precheck','after_sale_id':'12','action_key':'aftersale_refund_refund_agree'}
        with self.assertRaises(ValueError):await self.s.customer(query,self.i)
        await self.s.customer({'action':'aftersales'},self.i)
        with self.assertRaises(ValueError):await self.s.customer(query,{**self.i,'buyer':'other'})
        self.s.aftersales[('t','12')]['at']-=61
        with self.assertRaises(ValueError):await self.s.customer(query,self.i)
        self.assertNotIn('refund_precheck',[c[1] for c in self.request.calls])
