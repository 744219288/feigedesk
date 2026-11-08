"""Exercise operator consent and persistent automation controls with a fake shop."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Service, now
from app import make_server
from test_core import FakeTransport
from playwright.sync_api import sync_playwright, expect


class ReceptionUITests(unittest.TestCase):
    def test_history_scroll_stays_at_same_message_on_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=Service(tmp,FakeTransport)
            service.connect()
            for i in range(35):
                service.on_message(dict(id=str(i),conversation_id='scroll',name='滚动测试',role='customer',text='历史内容 '+str(i),timestamp=now()))
            server=make_server(service)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(viewport={'width':1366,'height':900})
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    page.locator('.conv').first.click()
                    expect(page.locator('.message')).to_have_count(35)
                    before=page.evaluate("() => {const b=document.querySelector('#messages');b.scrollTop=400;return b.scrollTop}")
                    service.on_message(dict(id='36',conversation_id='scroll',name='滚动测试',role='customer',text='新消息',timestamp=now()))
                    page.evaluate('fetchConversation(false)')
                    expect(page.locator('.message')).to_have_count(36)
                    after=page.locator('#messages').evaluate('(b)=>b.scrollTop')
                    self.assertAlmostEqual(before,after,delta=2)
                    browser.close()
            finally:
                server.shutdown();server.server_close();thread.join();service.close();service.db.close()

    def test_request_accept_pause_and_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=Service(tmp,FakeTransport,lambda *_:'图中是垃圾袋，请问是这款吗？[请求人工]')
            service.connect()
            service.on_message(dict(id='1',conversation_id='ui',name='测试买家',role='customer',text='图片咨询',timestamp=now()))
            service.update_settings({'model':'test','auto_reply':True})
            service.generate('live:ui')
            server=make_server(service)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            errors=[]
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(viewport={'width':1366,'height':900})
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    expect(page.locator('#autoPauseBtn')).to_have_text('暂停自动接待')
                    self.assertEqual(page.locator('#autoReply').count(),0)
                    page.locator('.conv').first.click()
                    expect(page.locator('#chatSubtitle')).to_contain_text('AI 继续接待')
                    self.assertFalse(service.conversation('live:ui')['human'])
                    page.locator('#humanBtn').click()
                    expect(page.locator('#humanBtn')).to_have_text('恢复 AI 接待')
                    self.assertTrue(service.conversation('live:ui')['human'])
                    page.locator('#humanBtn').click()
                    expect(page.locator('#humanBtn')).to_have_text('确认人工接管')
                    self.assertFalse(service.conversation('live:ui')['human'])
                    page.locator('#autoPauseBtn').click()
                    expect(page.locator('#autoPauseBtn')).to_have_text('恢复自动接待')
                    self.assertFalse(service.settings['auto_reply'])
                    page.locator('#autoPauseBtn').click()
                    expect(page.locator('#autoPauseBtn')).to_have_text('暂停自动接待')
                    page.locator('#modelTab').click()
                    with page.expect_response(lambda r:r.url.endswith('/api/settings')) as saved:
                        page.locator('#saveSettings').click()
                    self.assertEqual(saved.value.status,200)
                    expect(page.locator('#saveSettings')).to_be_enabled()
                    expect(page.locator('#settingsStatus')).to_have_text('已同步后台保存的配置')
                    self.assertTrue(service.settings['auto_reply'])
                    self.assertFalse(errors,errors)
                    self.assertFalse(service.transport.calls)
                    browser.close()
            finally:
                server.shutdown();server.server_close();thread.join();service.close();service.db.close()
