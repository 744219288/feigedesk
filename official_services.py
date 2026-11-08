"""Bounded, read-only Feige business APIs. No arbitrary URLs or write actions."""
from __future__ import annotations
import asyncio
import copy
import json
import re
import time
from urllib.parse import urlparse, parse_qs

ORIGIN = 'https://pigeon.jinritemai.com'
PATHS = {
    'account': '/backstage/currentuser',
    'staff': '/backstage/onlineservice',
    'permissions': '/backstage/getCustomerServiceAllPermission',
    'groups': '/backstage/quickreply/groupList',
    'replies': '/backstage/quickreply/replylist',
    'metrics': '/backstage/data/conversationAreaRealtimeData',
    'tickets': '/backstage/task_order/cnt',
    'robot': '/backstage/intelligence_robot/status',
    'scenes': '/backstage/intelligence_robot/scene_config',
    'queue': '/backstage/getQueueingType',
    'notifications': '/backstage/pullCurrentNotification',
    'orders': '/backstage/cmpoent/order/query',
    'products': '/backstage/workstation/get_consulting_products',
    'logistics': '/backstage/workshop/express_info',
    'aftersales': '/backstage/workstation/get_after_sale_list',
    'catalog': '/product/tproduct/list',
    'product_details': '/backstage/workstation/get_product_list',
    'coupon_permission': '/backstage/workstation/get_coupon_permission',
    'coupons': '/backstage/workstation/get_coupon_list',
    'aftersale_logs': '/backstage/workshop/after_sale_get_log_list',
    'refund_precheck': '/backstage/workshop/refund_pre_check',
}
CATALOG_ORIGIN = 'https://fxg.jinritemai.com'
METRICS = {
    'threeMinResponseConRate':'3 分钟回复率', 'satisfactionRate':'满意率',
    'laborAmount':'人工接待会话数', 'laborUserAmount':'人工接待人数',
    'shopQueueLength':'店铺排队人数', 'workerQueueLength':'当前客服排队人数',
    'firstResponseDuration':'首次响应时长', 'avgResponseDuration':'平均响应时长',
    'dissatisfactionRate':'不满意率', 'inquiryConversionRate':'询单转化率',
    'tradeCompound':'官方成交指标',
}
PERMISSIONS = {
    'conversation':'会话', 'historyConversation':'历史会话', 'convenientPhrase':'快捷短语',
    'onlineService':'在线客服', 'customerService':'客服管理', 'groupShunting':'分组分流',
    'sessionAllocation':'会话分配', 'shopData':'店铺数据', 'salesData':'销售数据',
    'reportCenter':'报表', 'realtimeAlert':'实时提醒', 'robotPermit':'智能客服',
    'callAnswerSetting':'接待设置', 'blockUsers':'黑名单', 'selfHelpService':'自助服务',
}

class OfficialError(ValueError):
    pass

def scalar(value, limit=500):
    return str(value)[:limit] if isinstance(value, (str,int,float,bool)) else ''

def pick(value, fields):
    if not isinstance(value, dict):
        raise OfficialError('官方返回格式变化，请刷新或检查版本')
    return {k: scalar(value[k],4000) for k in fields if k in value and value[k] is not None}

def rows(value):
    if not isinstance(value,list):
        raise OfficialError('官方列表格式变化，请检查版本')
    if len(value)>500:
        raise OfficialError('官方单页数据过多，请缩小查询范围')
    return value

def integer(value, minimum, maximum):
    if isinstance(value,bool) or not re.fullmatch(r'\d+',str(value)):
        raise OfficialError('查询参数无效')
    value=int(value)
    if not minimum<=value<=maximum:raise OfficialError('查询参数超出范围')
    return value

def business_rows(data, kind):
    """Explicit display fields only; no tokens, addresses, phone numbers or actions."""
    fields={
        'orders':('order_id','shop_order_id','order_status_desc','order_type_desc','order_time_sec','pay_time_sec','total_pay_amount_desc','total_pay_amount_str'),
        'products':('product_id','id','title','name','product_name','price_desc','sku_space_text'),
        'aftersales':('after_sale_id','order_id','shop_order_id','sku_order_id','after_sale_status_desc','status_desc','after_sale_type_desc','refund_amount_desc','reason'),
        'logistics':('company','company_name','express_company','logistics_code','status','status_desc','time','description','context','content'),
    }[kind]
    result=[]
    for v in rows(data):
        item=pick(v,fields)
        if kind=='orders' and isinstance(v.get('sku_order_list'),list):
            item['products']=[pick(x,('sku_order_id','product_id','product_name','sku_space_text','order_status_desc','buy_num_str','actual_pay_amount_desc')) for x in rows(v['sku_order_list'])]
        if not item and v:raise OfficialError('官方返回新数据结构，暂不展示未经校验的内容')
        result.append(item)
    return result

class OfficialServices:
    def __init__(self, context):
        self.context=context
        self.lock=asyncio.Lock()
        self.shop_id=''
        self.principal=''
        self.account={}
        self.cache={}
        self.orders={}
        self.cooldown={}
        self.observed={}
        self.aftersales={}

    async def request(self, name, params=None):
        if name not in PATHS:raise OfficialError('不支持的官方查询')
        if self.cooldown.get(name,0)>time.monotonic():
            raise OfficialError('官方暂时限制此查询，请稍后在飞鸽核实')
        method='post' if name in ('orders','aftersales','coupons','product_details','refund_precheck') else 'get'
        kwargs={'data':params or {}} if method=='post' and name!='refund_precheck' else {'params':params or {}}
        origin=CATALOG_ORIGIN if name=='catalog' else ORIGIN
        response=None
        try:
            response=await getattr(self.context.request,method)(origin+PATHS[name],**kwargs,timeout=12000,max_redirects=0)
            u=urlparse(response.url)
            if u.scheme!='https' or u.netloc!=urlparse(origin).netloc or u.path!=PATHS[name]:
                raise OfficialError('官方查询地址校验失败')
            if response.status!=200:
                raise OfficialError('官方查询失败（HTTP %s），请检查登录或权限'%response.status)
            if int(response.headers.get('content-length','0'))>2*1024*1024:
                raise OfficialError('官方响应过大，已停止读取')
            raw=await response.body()
            if len(raw)>2*1024*1024:raise OfficialError('官方响应过大，已停止读取')
            value=json.loads(raw)
            if not isinstance(value,dict) or type(value.get('code')) is not int or value['code']!=0:
                code=scalar(value.get('code') if isinstance(value,dict) else '',40)
                code=code if re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',code) else '未知'
                self.cooldown[name]=time.monotonic()+60
                raise OfficialError('官方未允许此查询（业务码 %s），请在飞鸽核实权限或验证要求'%code)
            spam=value.get('check_spam_resp') or {}
            if isinstance(spam,dict) and spam.get('decision') not in (None,'','PASS','pass','ALLOW','allow'):
                self.cooldown[name]=time.monotonic()+60
                raise OfficialError('官方要求验证，已停止此查询，请在飞鸽处理')
            return value
        except OfficialError:raise
        except Exception as e:raise OfficialError('官方查询超时或返回异常，请检查连接') from e
        finally:
            if response is not None:await response.dispose()

    async def authenticate(self):
        d=(await self.request('account'))['data']
        account=pick(d,('ShopName','ShopId'))
        shop=scalar(d.get('ShopId'))
        if not re.fullmatch(r'\d+',shop):raise OfficialError('未能核实当前店铺身份')
        info=d.get('CustomerServiceInfo') or {}
        principal=shop+':'+scalar(info.get('id'))
        if self.shop_id!=shop or self.principal!=principal:
            self.cache.clear();self.orders.clear();self.cooldown.clear();self.observed.clear();self.aftersales.clear()
        self.shop_id=shop
        self.principal=principal
        account['operator']=scalar(info.get('screen_name'))
        self.account=account
        return account

    async def verify_unchanged(self, principal):
        await self.authenticate()
        if self.principal!=principal:raise OfficialError('登录店铺或客服已变化，已丢弃本次查询，请刷新')

    async def catalog(self, body):
        """The shop-wide goods-management list, never consulting/recommended goods.

        Read-only, zero-based pages, explicit shop binding, no stale fallback.
        A bounded/inconsistent listing must never establish absence of a product.
        """
        async with self.lock:
            account=await self.authenticate()
            principal=self.principal
            scope='shop_id:'+self.shop_id
            if body.get('scope') and body['scope']!=scope:
                raise OfficialError('店铺身份已变化，不能沿用其他店铺商品')
            cached=self.cache.get('catalog')
            if not body.get('refresh') and cached and time.time()-cached['fetched_at']<30:
                return copy.deepcopy(cached)
            self.cache.pop('catalog',None)
            items=[];seen=set();total=None;complete=False
            for page in range(10):
                value=await self.request('catalog',{'page':page,'pageSize':20,'shop_id':self.shop_id})
                count=integer(value.get('total'),0,10000000)
                if integer(value.get('page'),0,1000000)!=page:
                    raise OfficialError('商品目录分页异常，请刷新')
                if total is not None and total!=count:
                    raise OfficialError('商品目录在查询中发生变化，请刷新')
                total=count
                batch=rows(value.get('data'))
                for raw in batch:
                    if not isinstance(raw,dict) or str(raw.get('shop_id'))!=self.shop_id:
                        raise OfficialError('商品归属与当前店铺不一致，已丢弃目录')
                    pid=str(raw.get('product_id',''))
                    if not re.fullmatch(r'\d+',pid) or pid in seen:
                        raise OfficialError('商品目录编号缺失或分页重复，请刷新')
                    name=raw.get('name');status=raw.get('tab')
                    if not isinstance(name,str) or not name.strip() or not isinstance(status,str) or not status.strip():
                        raise OfficialError('商品目录结构已变化，不能确认名称或状态')
                    seen.add(pid)
                    items.append({'id':pid,'name':name[:300],'status':status[:40],
                                  'category':scalar(raw.get('category_name'),200)})
                if len(items)>total:raise OfficialError('商品目录数量不一致，请刷新')
                if len(items)==total:
                    complete=True;break
                if not batch:raise OfficialError('商品目录缺页，无法确认完整商品范围')
            await self.verify_unchanged(principal)
            result={'account':account,'scope':scope,'status':'ready','source':'抖店商品管理官方接口',
                    'coverage':'商品管理全部列表；不含回收站、草稿箱',
                    'total':total,'complete':complete,'items':items,
                    'on_sale_count':sum(x['status']=='售卖中' for x in items) if complete else None,
                    'fetched_at':time.time()}
            self.cache['catalog']=result
            return copy.deepcopy(result)

    async def observe(self, response):
        """Read normal official-page query results; never navigate or replay a write."""
        u=urlparse(response.url)
        name=next((n for n in ('orders','products','aftersales','logistics','product_details') if u.path==PATHS[n]),None)
        if u.scheme!='https' or u.netloc!='pigeon.jinritemai.com' or not name or response.status!=200:return
        try:
            principal=getattr(response.request,'_feige_principal',None)
            if not principal:return
            params={k:v[0] for k,v in parse_qs(u.query).items()}
            if response.request.method=='POST':
                posted=response.request.post_data_json
                if not isinstance(posted,dict):return
                params.update(posted)
            buyer=params.get('security_user_id')
            if name!='product_details' and (not isinstance(buyer,str) or not buyer):return
            # Bound page and query: never mix search/filter results with an all-orders page.
            if name=='orders' and (params.get('search_words') or params.get('order_id_list') or params.get('create_time_start') or params.get('create_time_end') or str(params.get('tab_type'))!='0'):return
            page=integer(params.get('page_no',0),0,1000)
            raw=await asyncio.wait_for(response.body(),12)
            if len(raw)>2*1024*1024:return
            value=json.loads(raw)
            if type(value.get('code'))is not int or value['code']!=0:return
            spam=value.get('check_spam_resp') or {}
            if spam.get('decision') not in (None,'','PASS','pass','ALLOW','allow'):return
            async with self.lock:
                await self.authenticate()
                if self.principal!=principal:return
                if name=='product_details':
                    from commerce_data import product_rows
                    data=product_rows(value['data'],self.shop_id)
                    # Browsing footprints/recommendations must not replace the shop catalog.
                    if str(params.get('check_status'))!='3' or params.get('search_words'):return
                    self.cache['product_details']={'items':data,'fetched_at':time.time(),'scope':'shop_id:'+self.shop_id,
                        'page':page,'total':value.get('total'),'complete':page==0 and type(value.get('total')) is int and value['total']==len(data),
                        'source':'官方商品面板正常响应'}
                    return
                self.observed[(self.principal,buyer,name,page)]={'at':time.time(),'data':value['data']}
                if len(self.observed)>100:self.observed.pop(next(iter(self.observed)))
        except (ValueError,TypeError,KeyError,AttributeError,asyncio.TimeoutError):return

    async def coupons(self, body, identity):
        from commerce_data import coupon_rows
        async with self.lock:
            account=await self.authenticate();principal=self.principal
            if not identity or identity['scope']!='shop_id:'+self.shop_id:raise OfficialError('优惠券查询缺少当前客户店铺身份')
            permission=(await self.request('coupon_permission'))['data']
            can_send=(permission.get('kefu_send_coupon') or {}).get('HasPermission') is True
            offset=integer(body.get('offset',0),0,10000)
            value=await self.request('coupons',{'shop_id':int(self.shop_id),'status':[2],'coupon_biz_types':[8],
                                              'offset':offset,'limit':50,'talk_id':identity['talk']})
            data=value['data'];items=coupon_rows(data,self.shop_id)
            await self.verify_unchanged(principal)
            return {'items':items,'can_send':can_send,'account':account,'offset':offset,'total':data.get('total_count'),
                    'has_more':len(items)==50,'fetched_at':time.time(),'scope':identity['scope']}

    async def overview(self):
        async with self.lock:
            account=await self.authenticate()
            principal=self.principal
            cached=self.cache.get('overview')
            if cached and time.time()-cached['fetched_at']<30:return copy.deepcopy(cached)
            specs=('staff','permissions','metrics','tickets','robot','scenes','queue','notifications')
            sections={}
            # Low concurrency, independent errors do not erase other sections.
            gate=asyncio.Semaphore(3)
            async def fetch(name):
                async with gate:
                    try:
                        d=(await self.request(name))['data']
                        sections[name]={'status':'ready','data':self.normalize(name,d),'fetched_at':time.time()}
                    except OfficialError as exc:
                        sections[name]={'status':'error','detail':str(exc),'fetched_at':time.time()}
                    except (KeyError,TypeError,AttributeError):
                        sections[name]={'status':'error','detail':'官方返回结构已变化，暂不可用','fetched_at':time.time()}
            await asyncio.gather(*(fetch(n) for n in specs))
            await self.verify_unchanged(principal)
            result={'account':account,'sections':sections,'fetched_at':time.time()}
            self.cache['overview']=result
            return copy.deepcopy(result)

    @staticmethod
    def normalize(name,d):
        if name=='staff':return [pick(x,('service_name','online_status','valid_status')) for x in rows(d)]
        if name=='permissions':return [{'name':label,'value':scalar(d.get(k))} for k,label in PERMISSIONS.items() if k in d]
        if name=='metrics':
            if not isinstance(d,dict) or d.get('dataPermissions') is False:raise OfficialError('当前账号无统计数据权限')
            return {'items':[{'name':label,'value':scalar(d[k].get('amount'))} for k,label in METRICS.items() if isinstance(d.get(k),dict)],'update_time':d.get('updateTime')}
        if name=='tickets':return pick(d,('total',))
        if name=='robot':return pick(d,('open_status','can_open','is_first_open','product_can_open','test_window_can_open'))
        if name=='scenes':
            return [{'name':scalar(g.get('group_name')),'items':[pick(x,('item_name','desc','open_status','editable')) for x in rows(g.get('config_items',[]))]} for g in rows(d.get('config_groups',[]))]
        if name=='queue':return {'type':scalar(d)}
        if name=='notifications':return [pick(x,('title','content','message','create_time','type')) for x in rows(d)]
        raise OfficialError('不支持的数据格式')

    async def quick_replies(self, body):
        async with self.lock:
            await self.authenticate()
            principal=self.principal
            kind=integer(body.get('type',0),0,1)
            d=(await self.request('groups',{'type':kind}))['data']
            groups=[pick(x,('id','name','reply_count')) for x in rows(d.get('group_list',[]))]
            if not any(g['id']=='0' for g in groups):groups.insert(0,{'id':'0','name':'默认分组','reply_count':scalar(d.get('default_count',0))})
            group=scalar(body.get('group_id',''))
            if not group:group=next((g['id'] for g in groups if g.get('reply_count') not in ('','0')), '0')
            if group not in {g['id'] for g in groups}:raise OfficialError('快捷回复分组不属于当前店铺')
            page=integer(body.get('page',1),1,10000)
            r=await self.request('replies',{'type':kind,'group_id':group,'page':page,'size':20})
            items=[]
            for x in rows(r['data']):
                if x.get('shop_id') is not None and scalar(x['shop_id'])!=self.shop_id:raise OfficialError('快捷回复店铺归属不匹配')
                item=pick(x,('id','content','code','group_name','keyword'))
                item['content']=scalar(x.get('content'),20000)
                item['truncated']=isinstance(x.get('content'),str) and len(x['content'])>20000
                item['has_media']=bool(x.get('media') or x.get('link_cards'))
                items.append(item)
            total=r.get('total')
            await self.verify_unchanged(principal)
            return {'shop_id':self.shop_id,'groups':groups,'group_id':group,'type':kind,'items':items,'page':page,'has_more':page*20<total if type(total)is int else len(items)==20,'fetched_at':time.time()}

    async def customer(self, body, identity):
        if body.get('action')=='aftersales':
            # Refresh the same buyer's order page before constructing any aftersale IDs.
            order_page=await self.customer({**body,'action':'orders'},identity)
            async with self.lock:
                await self.authenticate();principal=self.principal
                if identity['scope']!='shop_id:'+self.shop_id:raise OfficialError('店铺已变化')
                raw=[]
                for item in order_page['items']:
                    oid=scalar(item.get('order_id') or item.get('shop_order_id'))
                    cached=self.orders.get((identity['talk'],oid))
                    if not cached or cached['buyer']!=identity['buyer']:raise OfficialError('订单关联已变化')
                    raw.append(cached['data'])
                from aftersale_data import query_tree,merge_details
                query,allowed=query_tree(raw)
                items=[]
                if query:
                    value=await self.request('aftersales',{'security_user_id':identity['buyer'],'biz_type':2,'query_list':query,'workstation_opt_gray':True})
                    items=merge_details(value['data'],allowed)
                await self.verify_unchanged(principal)
                self.aftersales={k:v for k,v in self.aftersales.items() if time.time()-v['at']<60}
                for item in items:self.aftersales[(identity['talk'],item['after_sale_id'])]={'buyer':identity['buyer'],'at':time.time(),'data':item}
                return {'items':items,'page':order_page['page'],'has_more':order_page['has_more'],'fetched_at':time.time(),
                        'note':'本页订单关联的售后；只读查询和预检查，不执行退款或拒绝'}
        async with self.lock:
            await self.authenticate()
            principal=self.principal
            if not identity or identity.get('scope')!='shop_id:'+self.shop_id or not identity.get('buyer'):
                raise OfficialError('尚未收到可核实店铺归属的客户身份，请等待该客户的新消息')
            buyer,talk=identity['buyer'],identity['talk']
            action=body.get('action','orders')
            if action not in ('orders','products','logistics','aftersale_logs','refund_precheck'):raise OfficialError('不支持的客户查询')
            if action in ('aftersale_logs','refund_precheck'):
                aid=scalar(body.get('after_sale_id'))
                case=self.aftersales.get((talk,aid))
                if not case or case['buyer']!=buyer or time.time()-case['at']>60:raise OfficialError('请先刷新当前客户售后，不能查询未验证的售后编号')
                if action=='refund_precheck':
                    choices=[a for a in case['data']['precheck_actions'] if a['key']==body.get('action_key')]
                    if len(choices)!=1:raise OfficialError('官方未返回此售后操作，不能猜测预检查参数')
                    d=(await self.request('refund_precheck',{'after_sale_id_list':aid,'shop_order_id':case['data']['order_id'],'action_type':choices[0]['action_type']}))['data']
                    result={'precheck':pick(d,('need_notice','notice_type','notice','notice_text','message')),'executed':False,
                            'note':'仅返回官方预检查提示；未退款、未拒绝，不会自动执行后续操作'}
                else:
                    d=(await self.request('aftersale_logs',{'after_sale_id':aid}))['data']
                    result={'items':[pick(x,('create_time','operate_time','operator','operation','content','description','status_desc')) for x in rows(d)]}
                await self.verify_unchanged(principal)
                return {**result,'fetched_at':time.time()}
            if action=='orders':
                page=integer(body.get('page',0),0,1000)
                observed=self.observed.get((principal,buyer,'orders',page))
                d=observed['data'] if observed and time.time()-observed['at']<60 else (await self.request('orders',{'security_user_id':buyer,'page_no':page,'page_size':5,'is_init_tab':1,'tab_type':0,'biz_type':2,'version':'1.0','workstation_opt_gray':True,'workstation_opt_version':'v2'}))['data']
                data=business_rows(d,'orders')
                if any(x.get('security_user_id') and x['security_user_id']!=buyer for x in d):
                    raise OfficialError('官方订单的客户归属不匹配，已阻止展示')
                await self.verify_unchanged(principal)
                if len(self.orders)>2000:self.orders.clear()
                for x in d:
                    oid=scalar(x.get('order_id') or x.get('shop_order_id'))
                    if oid:self.orders[(talk,oid)]={'at':time.time(),'buyer':buyer,'data':x}
                return {'items':data,'page':page,'has_more':len(data)>=5,'fetched_at':observed['at'] if observed and time.time()-observed['at']<60 else time.time()}
            if action=='products':
                d=(await self.request('products',{'security_user_id':buyer,'conv_id':talk}))['data']
                await self.verify_unchanged(principal)
                return {'items':business_rows(d.get('consulting_product',[]),'products'),'fetched_at':time.time()}
            oid=scalar(body.get('order_id',''))
            order=self.orders.get((talk,oid))
            if not order or order['buyer']!=buyer or time.time()-order['at']>120:raise OfficialError('请先刷新当前客户订单，再查询该订单的物流或售后')
            if action=='logistics':
                d=(await self.request('logistics',{'order_id':oid,'shop_order_tags':scalar(order['data'].get('shop_order_tags',''))}))['data']
                # Unknown nested package schema is not silently displayed as success.
                packages=d.get('multi_package_list',d.get('package_list')) if isinstance(d,dict) else None
                await self.verify_unchanged(principal)
                return {'items':business_rows(packages,'logistics'),'fetched_at':time.time()}
            raise OfficialError('不支持的客户查询')
