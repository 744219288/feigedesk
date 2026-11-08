import unittest
import test_core

class EntryTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown
    buyer=test_core.CoreTests.buyer

    def capture(self,ids,channel='抖音-私信-购物消息页',fields=None):
        self.s._sync_entry([{'id':x} for x in ids],{'channel':channel,'fields':fields or []})

    def test_official_source_persists_and_new_talk_does_not_inherit(self):
        self.s.connect(); self.buyer('one','11')
        self.capture(['11_CsAssign_','one'])
        self.assertEqual(self.s.conversation('live:11')['entry_context']['channel'],'抖音-私信-购物消息页')
        self.buyer('two','22')
        self.assertIsNone(self.s.conversation('live:11')['entry_context'])
        self.capture(['11_CsAssign_','one'])
        self.assertIsNone(self.s.conversation('live:11')['entry_context'])
        self.capture(['22_CsAssign_','two'],'商品详情页',[{'label':'来源商品','value':'眼镜 A'}])
        self.assertEqual(self.s.conversation('live:11')['entry_context']['fields'][0]['value'],'眼镜 A')

    def test_other_customer_and_conflicting_snapshot_rejected(self):
        self.s.connect(); self.buyer('one','11'); self.buyer('two','22',buyer='buyer2')
        self.capture(['one','two'])
        self.assertIsNone(self.s._entry_context('live:11'))
        self.capture(['22_CsAssign_','one'])
        self.assertIsNone(self.s._entry_context('live:11'))
        self.capture(['22_CsAssign_','two'])
        self.assertIsNone(self.s._entry_context('live:11'))
        self.assertIsNotNone(self.s._entry_context('live:22'))

    def test_customer_product_card_not_entry_product(self):
        self.s.connect(); self.buyer('one','11',product={'id':'p','title':'卡片商品','source':'customer_card'})
        self.assertIsNone(self.s._entry_context('live:11'))
        self.capture(['one'],fields=[{'label':'客户发送的商品','value':'卡片商品'}])
        self.assertEqual(self.s._entry_context('live:11')['fields'],[])

    def test_no_exact_message_evidence_no_source(self):
        self.s.connect(); self.buyer('one','11')
        self.capture(['11_CsAssign_','unknown'])
        self.assertIsNone(self.s._entry_context('live:11'))

if __name__=='__main__':unittest.main()
