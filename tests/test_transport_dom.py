"""Offline browser fixtures; no shop connection or real message sending."""
import sys
import unittest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from feige_transport import DOM_JS, FeigeTransport
from transcript_sync import SNAPSHOT_JS, CONTEXT_JS
from playwright.async_api import async_playwright

class DOMTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_capture_excludes_chat_cards_and_hidden_fields(self):
        await self.page.set_content('''<div id="topbar-sec-container"><div>新客</div><div class="AzMmxwmwwqzN1dn7P2cL">抖音-私信-购物消息页</div></div>
        <div><span>来源商品：</span><span>官方商品 A</span></div>
        <div style="display:none"><span>来源页面</span><span>隐藏页面</span></div>
        <div data-qa-id="qa-message-warpper"><div data-id="one"><span>来源商品</span><span>客户输入内容</span></div></div>''')
        result=await self.page.evaluate(CONTEXT_JS)
        self.assertEqual(result['entry']['channel'],'抖音-私信-购物消息页')
        self.assertEqual(result['entry']['fields'],[{'label':'来源商品','value':'官方商品 A'}])
        self.assertEqual(result['snapshot'][0]['id'],'one')

    async def test_attachment_recovery_does_not_open_other_conversations(self):
        stamp=(datetime.now()-timedelta(seconds=30)).strftime('%H:%M:%S')
        await self.page.set_content('''<div data-qa-id="qa-conversation-chat-item">同名客户</div>
        <div data-qa-id="qa-conversation-chat-item">同名客户</div><div id="chat"></div>
        <textarea data-qa-id="qa-send-message-textarea"></textarea>''')
        await self.page.evaluate('''stamp=>{const rows=document.querySelectorAll('[data-qa-id="qa-conversation-chat-item"]');
        rows.forEach((r,n)=>r.onclick=()=>{document.querySelector('#chat').innerHTML=`<div data-qa-id="qa-message-warpper"><div data-id="${n?'wanted-image':'wrong-image'}"><div class="Ie29C7uLyEjZzd8JeS8A" style="flex-direction:row"><img alt="图片" src="https://p9-pigeon-sign.ecombdimg.com/test.png"><div multiple-click-times="5">${stamp}</div></div></div></div>`})}''',stamp)
        with tempfile.TemporaryDirectory() as folder:
            received=[]
            bridge=FeigeTransport(folder,received.append,lambda _:None)
            bridge.pending_sync=lambda:[{'talk':'wanted-talk','ids':['wanted-image']}]
            await bridge._recover_pending_media(self.page)
            self.assertEqual(received,[])
            self.assertEqual(await self.page.locator('#chat').inner_html(),'')
            await self.page.locator('[data-qa-id=qa-conversation-chat-item]').nth(1).click()
            await bridge._recover_pending_media(self.page)
            self.assertEqual(len(received),1)
            self.assertEqual(received[0]['conversation_id'],'wanted-talk')
            self.assertEqual(received[0]['id'],'wanted-image')
            self.assertTrue(received[0]['media'])
            self.assertEqual(await self.page.locator('textarea').input_value(),'')

    async def test_recovery_never_changes_manual_draft(self):
        await self.page.locator('textarea').fill('人工草稿')
        with tempfile.TemporaryDirectory() as folder:
            bridge=FeigeTransport(folder,lambda _:None,lambda _:None)
            bridge.pending_sync=lambda:[{'talk':'100','ids':['missing']}]
            await bridge._recover_pending_media(self.page)
            self.assertEqual(await self.page.locator('textarea').input_value(),'人工草稿')

    async def test_repeated_recovery_preserves_scroll_tabs_and_selection(self):
        await self.page.set_content('''<button role="tab" aria-selected="false" onclick="window.clicks++">最近联系</button>
        <div data-qa-id="qa-conversation-chat-item" onclick="window.clicks++">客户</div>
        <div id="history" style="height:100px;overflow-y:auto"><div style="height:1000px">
        <div data-qa-id="qa-message-warpper"><div data-id="visible">消息</div></div></div></div>
        <textarea data-qa-id="qa-send-message-textarea"></textarea>''')
        await self.page.evaluate("window.clicks=0;document.querySelector('#history').scrollTop=450")
        with tempfile.TemporaryDirectory() as folder:
            bridge=FeigeTransport(folder,lambda _:None,lambda _:None)
            for pending in ([],[{'talk':'missing-talk','ids':['missing-image']} ]):
                bridge.pending_sync=lambda:pending
                for _ in range(5): await bridge._recover_pending_media(self.page)
                self.assertEqual(await self.page.evaluate('window.clicks'),0)
                self.assertEqual(await self.page.locator('#history').evaluate('(e)=>e.scrollTop'),450)
                self.assertEqual(await self.page.locator('[role=tab]').get_attribute('aria-selected'),'false')

    async def test_real_image_structure_both_sides_outside_text_bubble(self):
        await self.page.set_content(''.join(f'''<div data-qa-id="qa-message-warpper"><div data-id="{role}">
        <div class="rcHPT4n3TlQD0Nu4sSiv">13:45</div>
        <div class="Ie29C7uLyEjZzd8JeS8A" style="flex-direction:{direction}">
        <img alt="头像" src="https://example.com/avatar.png"><div><img alt="图片" src="https://example.com/photo.png">
        <div multiple-click-times="5">13:45:21</div></div></div></div></div>'''
        for role,direction in [('customer','row'),('agent','row-reverse')]))
        rows=await self.page.evaluate(SNAPSHOT_JS)
        self.assertEqual([r['role'] for r in rows],['customer','agent'])
        self.assertEqual([r['clock'] for r in rows],['13:45:21','13:45:21'])
        for row in rows:
            self.assertEqual(row['text'],'')
            self.assertEqual(row['media'],[{'kind':'image','url':'https://example.com/photo.png'}])

    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(channel='chrome',headless=True)
        self.page=await self.browser.new_page()
        await self.page.set_content('''<div data-qa-id="qa-conversation-chat-item" data-talk-id="100" aria-selected="true">同名客户</div>
        <div data-qa-id="qa-conversation-chat-item" data-talk-id="200">同名客户</div>
        <div data-qa-id="qa-message-warpper"><div class="messageNotMe" data-id="source-100">测试</div></div>
        <textarea data-qa-id="qa-send-message-textarea"></textarea>
        <button data-qa-id="qa-send-btn" onclick="window.clicks=(window.clicks||0)+1">发送</button>''')
        self.args={'talk':'100','source_ids':['source-100'],'token':'fixture','text':'离线回复'}

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def action(self,action):
        return await self.page.evaluate(DOM_JS,{**self.args,'action':action})

    async def test_native_upload_blocks_wrong_customer_and_existing_draft(self):
        await self.page.locator('body').evaluate('''e=>e.insertAdjacentHTML('beforeend','<label><span data-qa-id="qa-send-img">图片</span><input type="file"></label>')''')
        args={**self.args,'manual':True,'action':'native_upload','kind':'image','name':'test.png','mime':'image/png','payload':'aGk='}
        wrong=await self.page.evaluate(DOM_JS,{**args,'talk':'200'})
        self.assertFalse(wrong['ok'])
        await self.page.locator('textarea').fill('人工正在写的内容')
        result=await self.page.evaluate(DOM_JS,args);self.assertEqual(result['reason'],'manual_draft')
        self.assertEqual(await self.page.locator('textarea').input_value(),'人工正在写的内容')

    async def test_native_video_uses_file_preview_and_exact_filename(self):
        await self.page.locator('body').evaluate('''e=>e.insertAdjacentHTML('beforeend','<div role="dialog"><span>test.mp4</span><button data-qa-id="qa-send-file-popup-submit" onclick="window.videoSent=(window.videoSent||0)+1">发送</button></div>')''')
        await self.page.evaluate('window.__fdNativeOperation={token:"fixture"}')
        args={**self.args,'manual':True,'action':'native_confirm','kind':'video','name':'other.mp4'}
        self.assertFalse((await self.page.evaluate(DOM_JS,args))['ok'])
        self.assertTrue((await self.page.evaluate(DOM_JS,{**args,'name':'test.mp4'}))['ok'])
        self.assertEqual(await self.page.evaluate('window.videoSent'),1)

    async def test_coupon_sent_history_is_excluded_and_probe_never_sends(self):
        await self.page.locator('body').evaluate('''e=>e.insertAdjacentHTML('beforeend','<div data-qa-id="qa-message-warpper"><div><span>券名</span><span onclick="window.wrong=true">立即发送</span></div></div><section><span>券名</span><button onclick="window.couponSent=(window.couponSent||0)+1"><span>立即发送</span></button></section>')''')
        args={**self.args,'manual':True,'action':'native_coupon','coupon':{'name':'券名'}}
        self.assertTrue((await self.page.evaluate(DOM_JS,{**args,'probe':True}))['ok'])
        self.assertIsNone(await self.page.evaluate('window.couponSent'))
        self.assertTrue((await self.page.evaluate(DOM_JS,args))['ok'])
        self.assertEqual(await self.page.evaluate('window.couponSent'),1)
        self.assertIsNone(await self.page.evaluate('window.wrong'))

    async def test_exact_source_prepare_and_commit_once(self):
        self.assertTrue((await self.action('check'))['ok'])
        self.assertTrue((await self.action('prepare'))['ok'])
        self.assertTrue((await self.action('commit'))['ok'])
        self.assertFalse((await self.action('commit'))['ok'])
        self.assertEqual(await self.page.evaluate('window.clicks'),1)

    async def test_duplicate_name_does_not_match_wrong_source(self):
        self.args['talk']='200'
        self.args['source_ids']=['source-200']
        self.assertFalse((await self.action('check'))['ok'])
        self.assertFalse((await self.action('prepare'))['ok'])

    async def test_selected_identity_mismatch_blocks(self):
        self.args['talk']='200'
        self.assertFalse((await self.action('check'))['ok'])

    async def test_later_message_blocks_commit(self):
        self.assertTrue((await self.action('prepare'))['ok'])
        await self.page.evaluate('''document.querySelector('[data-id]').setAttribute('data-id','new-source')''')
        self.assertFalse((await self.action('commit'))['ok'])
        self.assertIsNone(await self.page.evaluate('window.clicks'))
        await self.action('abort')
        self.assertEqual(await self.page.locator('textarea').input_value(),'')

    async def test_manual_draft_is_preserved(self):
        await self.page.locator('textarea').fill('人工草稿')
        self.assertFalse((await self.action('prepare'))['ok'])
        self.assertEqual(await self.page.locator('textarea').input_value(),'人工草稿')

    async def test_probe_only_reports_structure(self):
        probe=await self.action('probe')
        self.assertEqual(probe['source_match_count'],1)
        self.assertEqual(probe['input_count'],1)
        self.assertEqual(probe['exact_talk_rows'],1)
        self.assertNotIn('source-100',str(probe))
        self.assertNotIn('同名客户',str(probe))

    async def test_only_open_exact_im_socket_is_counted(self):
        await self.page.evaluate('''window.__feigeDeskSockets = [
          {url:'wss://auxiliary.invalid/socket',readyState:1},
          {url:'wss://ws.fxg.jinritemai.com.evil.invalid/socket',readyState:1},
          {url:'wss://ws.fxg.jinritemai.com/socket',readyState:3},
          {url:'ws://ws.fxg.jinritemai.com/socket',readyState:1}
        ]''')
        self.assertEqual((await self.action('probe'))['actual_im_socket_count'],0)
        await self.page.evaluate("window.__feigeDeskSockets.push({url:'wss://ws.fxg.jinritemai.com/socket?secret=hidden',readyState:1})")
        probe=await self.action('probe')
        self.assertEqual(probe['actual_im_socket_count'],1)
        self.assertNotIn('hidden',str(probe))

    async def test_product_probe_excludes_message_labels_and_contents(self):
        await self.page.evaluate('''() => {
          document.querySelector('[data-id]').innerHTML='<span>来源商品</span>';
          const aside=document.createElement('aside');
          aside.innerHTML='<span>咨询来源</span><a href="https://example.invalid/?secret=private">私密商品</a>';
          document.body.append(aside);
        }''')
        probe=(await self.action('probe'))['product_probe']
        self.assertTrue(probe['source_verified'])
        self.assertEqual([x['label'] for x in probe['labels']],['咨询来源'])
        self.assertNotIn('private',str(probe))
        self.assertNotIn('私密商品',str(probe))
        self.args['source_ids']=['wrong']
        self.assertFalse((await self.action('probe'))['product_probe']['source_verified'])

if __name__=='__main__': unittest.main()
