import json,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from official_services import OfficialServices,OfficialError,PATHS,CATALOG_ORIGIN
from test_official_services import Request
from test_core import FakeTransport
from core import Service,now

def item(i='1',shop=42,status='售卖中'):
    return {'product_id':i,'shop_id':shop,'name':'测试垃圾袋','tab':status,'secret':'hidden'}

class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.r=Request();self.s=OfficialServices(SimpleNamespace(request=self.r))
        self.r.override['catalog']={'code':0,'total':1,'page':0,'data':[item()]}
    async def test_live_schema_scope_and_cache_refresh(self):
        a=await self.s.catalog({'scope':'shop_id:42'})
        self.assertTrue(a['complete']);self.assertEqual(a['total'],1);self.assertEqual(a['on_sale_count'],1)
        self.assertNotIn('hidden',json.dumps(a))
        call=next(c for c in self.r.calls if c[1]=='catalog')
        self.assertEqual(call[0],'get');self.assertEqual(call[2]['params']['shop_id'],'42')
        self.assertEqual(self.r.responses[1].url,CATALOG_ORIGIN+PATHS['catalog'])
        a['items'].clear();self.assertEqual(len((await self.s.catalog({}))['items']),1)
        self.assertEqual(sum(c[1]=='catalog' for c in self.r.calls),1)
        await self.s.catalog({'refresh':True});self.assertEqual(sum(c[1]=='catalog' for c in self.r.calls),2)
    async def test_wrong_shop_and_switch_during_query(self):
        with self.assertRaises(OfficialError):await self.s.catalog({'scope':'shop_id:99'})
        self.r.override['catalog']['data']=[item(shop=99)]
        with self.assertRaises(OfficialError):await self.s.catalog({})
        self.r.override['catalog']['data']=[item()]
        original=self.r.answer
        def answer(method,url,kw):
            r=original(method,url,kw)
            if url.endswith(PATHS['catalog']):self.r.shop='43'
            return r
        self.r.answer=answer
        with self.assertRaises(OfficialError):await self.s.catalog({})
        self.assertNotIn('catalog',self.s.cache)
    async def test_all_pages_and_offline_not_counted_on_sale(self):
        original=self.r.answer
        def answer(method,url,kw):
            if url.endswith(PATHS['catalog']):
                p=kw['params']['page'];self.r.override['catalog']={'code':0,'total':21,'page':p,'data':[item(str(i),status='已下架' if i==20 else '售卖中') for i in range(p*20,min(21,(p+1)*20))]}
            return original(method,url,kw)
        self.r.answer=answer
        a=await self.s.catalog({});self.assertEqual(a['total'],21);self.assertEqual(len(a['items']),21);self.assertEqual(a['on_sale_count'],20)
    async def test_bounded_result_not_complete(self):
        original=self.r.answer
        def answer(method,url,kw):
            if url.endswith(PATHS['catalog']):
                p=kw['params']['page'];self.r.override['catalog']={'code':0,'total':201,'page':p,'data':[item(str(i)) for i in range(p*20,(p+1)*20)]}
            return original(method,url,kw)
        self.r.answer=answer
        a=await self.s.catalog({});self.assertFalse(a['complete']);self.assertIsNone(a['on_sale_count'])
    async def test_no_stale_fallback_after_failure(self):
        await self.s.catalog({});self.r.override['catalog']={'code':100,'data':None}
        with self.assertRaises(OfficialError):await self.s.catalog({'refresh':True})
        self.assertNotIn('catalog',self.s.cache)
    async def test_incomplete_duplicate_and_unknown_schema_rejected(self):
        for data,total in [([],1),([item(),item()],2),([{'product_id':'1','shop_id':42}],1)]:
            self.r.override['catalog']={'code':0,'page':0,'total':total,'data':data}
            with self.assertRaises(OfficialError):await self.s.catalog({'refresh':True})
    async def test_zero_is_complete_not_a_failed_query(self):
        self.r.override['catalog']={'code':0,'page':0,'total':0,'data':[]}
        a=await self.s.catalog({});self.assertTrue(a['complete']);self.assertEqual(a['total'],0)

class CatalogReplyTests(unittest.TestCase):
    def test_catalog_reaches_model_and_wrong_shop_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            prompts=[];s=Service(d,FakeTransport,lambda settings,m:prompts.append(m) or '目前就这一款。');s.connect()
            try:
                s.on_message(dict(id='1',conversation_id='t',role='customer',text='只有这款吗',timestamp=now(),shop_scope='shop_id:42',shop_scope_type='shop_id',buyer_id='b',buyer_id_type='security_sender_id'))
                def query(section,body):
                    if section=='identity':return {'shop_name':'test','scope':'shop_id:42'}
                    return {'status':'ready','scope':'shop_id:42','total':1,'complete':True,'items':[{'name':'独家测试商品'}]}
                s.transport.official_query=query;s.generate('live:t')
                self.assertIn('独家测试商品',prompts[-1][0]['content']);self.assertFalse(s.transport.calls)
                s.transport.official_query=lambda *a:{'status':'ready','scope':'shop_id:99','items':[{'name':'错误店商品'}]}
                s.generate('live:t');self.assertNotIn('错误店商品',prompts[-1][0]['content'].split('<本店商品目录')[1])
            finally:s.close();s.db.close()

if __name__=='__main__':unittest.main()
