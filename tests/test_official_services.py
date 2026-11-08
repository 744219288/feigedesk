import asyncio
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from official_services import OfficialServices,OfficialError,PATHS,ORIGIN,business_rows
from core import Service,now
from test_core import FakeTransport
from feige_transport import FeigeTransport
from vendor.feige.ws_reader import extract_messages
from test_transport import received

class Response:
    status=200
    headers={}
    def __init__(self,url,data):self.url=url;self.data=data;self.disposed=False
    async def body(self):return json.dumps(self.data).encode()
    async def dispose(self):self.disposed=True

class Request:
    def __init__(self):self.calls=[];self.responses=[];self.override={};self.shop='42'
    async def get(self,url,**kw):return self.answer('get',url,kw)
    async def post(self,url,**kw):return self.answer('post',url,kw)
    def answer(self,method,url,kw):
        name=next(n for n,p in PATHS.items() if url.endswith(p))
        self.calls.append((method,name,kw))
        data={'code':0,'data':{'ShopId':int(self.shop),'ShopName':'test','CustomerServiceInfo':{'screen_name':'staff','security_id':'secret'}}} if name=='account' else self.override.get(name,{'code':0,'data':[]})
        r=Response(url,data);self.responses.append(r);return r

class OfficialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.request=Request();self.s=OfficialServices(SimpleNamespace(request=self.request))
    async def test_closed_api_allowlist(self):
        for key in ('https://evil.test','/backstage/workshop/confirm_refund','refund'):
            with self.assertRaises(OfficialError):await self.s.request(key)
        self.assertEqual(self.request.calls,[])
    async def test_business_failure_backoff_and_no_leaked_message(self):
        self.request.override['orders']={'code':'10001010A','msg':'secret signature and customer details'}
        for _ in range(2):
            with self.assertRaises(OfficialError) as err:await self.s.request('orders')
            self.assertNotIn('signature',str(err.exception))
        self.assertEqual(len(self.request.calls),1)
        self.assertTrue(self.request.responses[0].disposed)
    async def test_missing_or_boolean_code_is_not_success(self):
        for value in ({'data':[]},{'code':False,'data':[]}):
            self.request.override['staff']=value;self.s.cooldown.clear()
            with self.assertRaises(OfficialError):await self.s.request('staff')
    async def test_official_captcha_is_not_success(self):
        self.request.override['staff']={'code':0,'data':[],'check_spam_resp':{'decision':'BLOCK'}}
        with self.assertRaises(OfficialError):await self.s.request('staff')
    async def test_redirect_rejected_and_response_disposed(self):
        original=self.request.answer
        def answer(*a):
            r=original(*a);r.url='https://evil.test/';return r
        self.request.answer=answer
        with self.assertRaises(OfficialError):await self.s.request('staff')
        self.assertEqual(self.request.calls[0][2]['max_redirects'],0)
        self.assertTrue(self.request.responses[0].disposed)
    async def test_account_secrets_not_public_and_switch_clears_cache(self):
        a=await self.s.authenticate();self.assertNotIn('secret',json.dumps(a))
        self.s.orders[('talk','order')]={};self.s.cache['overview']={};self.request.shop='43'
        await self.s.authenticate();self.assertEqual(self.s.orders,{});self.assertEqual(self.s.cache,{})
    async def test_cross_shop_customer_query_blocked_before_order_request(self):
        with self.assertRaises(OfficialError):await self.s.customer({}, {'scope':'shop_id:43','buyer':'b','talk':'t'})
        self.assertEqual([x[1] for x in self.request.calls],['account'])
    async def test_customer_identity_required(self):
        with self.assertRaises(OfficialError):await self.s.customer({},None)
    async def test_order_id_injection_cannot_query_logistics(self):
        with self.assertRaises(OfficialError):await self.s.customer({'action':'logistics','order_id':'someone-elses-order'}, {'scope':'shop_id:42','buyer':'b','talk':'t'})
        self.assertEqual([x[1] for x in self.request.calls],['account'])
    async def test_customer_orders_are_paged_and_redacted(self):
        self.request.override['orders']={'code':0,'data':[{'order_id':'123','order_status_desc':'已付款','mobile':'private','post_address':'private','token':'private','sku_order_list':[{'sku_order_id':'234','product_name':'test','receiver':'private'}]}]}
        r=await self.s.customer({'page':0},{'scope':'shop_id:42','buyer':'b','talk':'t'})
        self.assertNotIn('private',json.dumps(r));self.assertEqual(r['items'][0]['order_id'],'123')
        call=next(c for c in self.request.calls if c[1]=='orders')
        self.assertEqual(call[0],'post');self.assertEqual(call[2]['data']['security_user_id'],'b')
        with self.assertRaises(OfficialError):await self.s.customer({'action':'logistics','order_id':'123'},{'scope':'shop_id:42','buyer':'other','talk':'t'})
    async def test_expired_order_binding_denied(self):
        self.s.shop_id='42';self.s.orders[('t','123')]={'at':time.time()-121,'buyer':'b'}
        with self.assertRaises(OfficialError):await self.s.customer({'action':'logistics','order_id':'123'},{'scope':'shop_id:42','buyer':'b','talk':'t'})
    async def test_quickreply_group_membership_and_pagination(self):
        self.request.override.update(groups={'code':0,'data':{'group_list':[{'id':10,'name':'问候','reply_count':25}],'default_count':0}},replies={'code':0,'data':[{'id':1,'shop_id':42,'content':'你好','token':'secret','media':{'images':[{}]}}],'total':25})
        r=await self.s.quick_replies({});self.assertEqual(r['group_id'],'10');self.assertTrue(r['has_more']);self.assertTrue(r['items'][0]['has_media']);self.assertNotIn('secret',json.dumps(r))
        with self.assertRaises(OfficialError):await self.s.quick_replies({'group_id':'99'})
    async def test_cross_shop_quickreply_rejected(self):
        self.request.override.update(groups={'code':0,'data':{'group_list':[],'default_count':1}},replies={'code':0,'data':[{'shop_id':999,'content':'wrong'}]})
        with self.assertRaises(OfficialError):await self.s.quick_replies({})
    async def test_long_quickreply_not_silently_shortened_into_sendable_draft(self):
        self.request.override.update(groups={'code':0,'data':{'group_list':[],'default_count':1}},replies={'code':0,'data':[{'shop_id':42,'content':'x'*4500}]})
        r=await self.s.quick_replies({});self.assertEqual(len(r['items'][0]['content']),4500)
        self.request.override['replies']['data'][0]['content']='x'*20001
        r=await self.s.quick_replies({});self.assertTrue(r['items'][0]['truncated'])
    async def test_overview_partial_failure_keeps_other_sections(self):
        self.request.override.update(permissions={'code':0,'data':{}},metrics={'code':0,'data':{'dataPermissions':False}},tickets={'code':0,'data':{'total':3}},robot={'code':0,'data':{}},scenes={'code':0,'data':{'config_groups':[]}},queue={'code':0,'data':1})
        r=await self.s.overview();self.assertEqual(r['sections']['metrics']['status'],'error');self.assertEqual(r['sections']['tickets']['data']['total'],'3')
    async def test_unknown_schema_not_silent_empty_success(self):
        with self.assertRaises(OfficialError):business_rows([{'new_unsupported_schema':'value'}],'orders')
    async def test_login_switch_during_order_request_discards_results(self):
        original=self.request.answer
        def answer(method,url,kw):
            r=original(method,url,kw)
            if url.endswith(PATHS['orders']):self.request.shop='43'
            return r
        self.request.answer=answer
        with self.assertRaises(OfficialError):await self.s.customer({}, {'scope':'shop_id:42','buyer':'b','talk':'t'})
        self.assertEqual(self.s.orders,{})
    async def test_wrong_customer_in_response_is_blocked(self):
        self.request.override['orders']={'code':0,'data':[{'order_id':'123','security_user_id':'other'}]}
        with self.assertRaises(OfficialError):await self.s.customer({}, {'scope':'shop_id:42','buyer':'b','talk':'t'})
    async def test_observed_official_order_read_is_bound_and_no_new_order_request(self):
        await self.s.authenticate()
        r=Response(ORIGIN+PATHS['orders'],{'code':0,'data':[{'order_id':'123'}]})
        r.request=SimpleNamespace(method='POST',post_data_json={'security_user_id':'b','page_no':0,'tab_type':0},_feige_principal=self.s.principal)
        await self.s.observe(r)
        out=await self.s.customer({}, {'scope':'shop_id:42','buyer':'b','talk':'t'})
        self.assertEqual(out['items'][0]['order_id'],'123')
        self.assertFalse(any(c[1]=='orders' for c in self.request.calls))
    async def test_observed_filtered_or_old_account_orders_not_reused(self):
        await self.s.authenticate()
        for extra,principal in [({'search_words':'specific'},self.s.principal),({},'old-account'),({'tab_type':1},self.s.principal)]:
            r=Response(ORIGIN+PATHS['orders'],{'code':0,'data':[{'order_id':'123'}]})
            r.request=SimpleNamespace(method='POST',post_data_json={'security_user_id':'b','page_no':0,'tab_type':0,**extra},_feige_principal=principal)
            await self.s.observe(r)
        self.assertEqual(self.s.observed,{})

class IdentityTests(unittest.TestCase):
    def test_conflicting_transport_identity_remains_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            t=FeigeTransport(d,lambda _:None,lambda _:None)
            for buyer in ('a','b','a'):
                t._accept_messages((1,'socket'),extract_messages(received(buyer_id=buyer)),historical_only=True)
            self.assertTrue(t._business_identities)
            self.assertTrue(all(x is None for x in t._business_identities.values()))
    def test_service_requires_live_and_proven_identity(self):
        with tempfile.TemporaryDirectory() as d:
            s=Service(d,FakeTransport,lambda *_:'test')
            s.transport.official_query=lambda *a:{'unexpected':True}
            try:
                with self.assertRaises(ValueError):s.official_query({'section':'overview'})
                s.connect();s.on_message({'id':'1','conversation_id':'a','role':'customer','text':'hello','timestamp':now()})
                with self.assertRaises(ValueError):s.official_query({'section':'customer','conversation_id':'live:a'})
                self.assertIn('deliveries',s.official_query({'section':'quality'}))
            finally:s.close();s.db.close()
    def test_mode_change_during_request_discards_response(self):
        with tempfile.TemporaryDirectory() as d:
            s=Service(d,FakeTransport,lambda *_:'test');s.connect()
            def query(*args):s.demo();return {'data':'old'}
            s.transport.official_query=query
            try:
                with self.assertRaises(ValueError):s.official_query({'section':'overview'})
            finally:s.close();s.db.close()

if __name__=='__main__':unittest.main()
