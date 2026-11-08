import sys,tempfile,threading,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Service,now
from app import make_server
from test_core import FakeTransport
from playwright.sync_api import sync_playwright,expect

class OfficialUITests(unittest.TestCase):
    def test_reply_to_draft_knowledge_review_and_no_customer_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=Service(tmp,FakeTransport);service.connect()
            service.on_message(dict(id='1',conversation_id='test',name='测试客户',role='customer',text='你好',timestamp=now()))
            calls=[]
            def query(section,body,expected):
                calls.append(body)
                if section=='overview':return {'account':{'ShopName':'测试店铺','operator':'测试客服'},'sections':{'tickets':{'status':'ready','data':{'total':'0'}}},'fetched_at':time.time()}
                if section=='catalog':return {'account':{'ShopName':'测试店铺'},'total':1,'on_sale_count':1,'complete':True,'coverage':'商品管理全部列表','items':[{'id':'123','name':'<img src=x onerror=alert(1)> 垃圾袋','status':'售卖中','category':'家清'}],'fetched_at':time.time()}
                if section=='replies':return {'shop_id':'42','groups':[{'id':'1','name':'常用','reply_count':'1'}],'group_id':'1','type':0,'items':[{'id':'1','content':'<img src=x onerror=alert(1)> 请提供型号','group_name':'常用','has_media':False}],'page':1,'has_more':False,'fetched_at':time.time()}
                raise ValueError('测试中未开放')
            service.transport.official_query=query
            server=make_server(service);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{server.server_port}');page.locator('.conv').first.click()
                    page.locator('#officialBtn').click();expect(page.locator('#officialBody')).to_contain_text('官方待处理计数')
                    page.locator('#officialSection').select_option('catalog');expect(page.locator('#officialBody')).to_contain_text('商品总数：1 件 · 售卖中：1 件')
                    expect(page.locator('#officialBody')).to_contain_text('垃圾袋');self.assertEqual(page.locator('#officialBody img').count(),0)
                    self.assertTrue(next(c for c in calls if c['section']=='catalog')['refresh'])
                    page.locator('#officialSection').select_option('replies');expect(page.locator('#officialBody')).to_contain_text('请提供型号')
                    self.assertEqual(page.locator('#officialBody img').count(),0)
                    page.get_by_role('button',name='填入回复草稿',exact=True).click()
                    expect(page.locator('#reply')).to_have_value('<img src=x onerror=alert(1)> 请提供型号')
                    page.locator('#officialBtn').click();expect(page.locator('#officialBody')).to_contain_text('请提供型号')
                    page.get_by_role('button',name='整理为本地知识',exact=True).click()
                    expect(page.locator('#knowledgeContent')).to_have_value('<img src=x onerror=alert(1)> 请提供型号')
                    self.assertEqual(service.db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0],0)
                    page.locator('#officialBtn').click();page.locator('#officialSection').select_option('quality')
                    expect(page.locator('#officialBody')).to_contain_text('待人工协助')
                    page.get_by_role('button',name='关闭',exact=True).click()
                    self.assertFalse(errors,errors);self.assertFalse(service.transport.calls)
                    self.assertTrue(any(c['section']=='replies' for c in calls));browser.close()
            finally:server.shutdown();server.server_close();thread.join();service.close();service.db.close()

if __name__=='__main__':unittest.main()
