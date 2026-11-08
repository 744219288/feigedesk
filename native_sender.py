"""Bounded manual sends through the official UI; confirmed only by matched server echo."""
import asyncio,base64,concurrent.futures,hashlib,json,uuid
import time
from urllib.parse import urlsplit
from vendor.feige import ws_reader,ws_sender

def outgoing_kind(raw):
    dec=ws_reader.decode(raw);p=ws_sender.get_path(dec,[8,8,100]) if dec else None
    if not p or not isinstance(p[1],tuple) or p[1][0]!='msg':return ''
    fields=p[1][1]
    kv=ws_reader._kvmap([(9,w,v) for f,w,v in fields if f==5])
    kind=str(kv.get('type') or '').lower()
    mapping={'image':'image','file_image':'image','video':'video','file_video':'video','file':'file','document':'file','file_document':'file'}
    if kind in mapping:return mapping[kind]
    content=ws_sender.frame_text(raw)
    try:value=json.loads(content)
    except (TypeError,ValueError):return ''
    if not isinstance(value,dict):return ''
    if value.get('card_type')=='kefu_coupon' or kv.get('card_type')=='kefu_coupon':return 'coupon'
    if kind in ('card','custom') and 'coupon' in str(value.get('type','')):return 'coupon'
    return ''

def outgoing_shop(raw):
    dec=ws_reader.decode(raw);p=ws_sender.get_path(dec,[8,8,100]) if dec else None
    if not p or not isinstance(p[1],tuple) or p[1][0]!='msg':return ''
    kv=ws_reader._kvmap([(9,w,v) for f,w,v in p[1][1] if f==5])
    return 'shop_id:'+str(kv['shop_id']) if kv.get('shop_id') else ''

def incoming_kind(m):
    kind=str(m.msg_type).lower()
    if kind in ('image','file_image','pic','picture'):return 'image'
    if kind in ('video','file_video'):return 'video'
    if kind in ('file','document','file_document','pdf'):return 'file'
    if m.media:return m.media[0].get('kind','')
    if ('优惠券' in m.text or '优惠劵' in m.text) and kind=='template_card':return 'coupon'
    if m.text.startswith('[文件]') and kind=='template_card':return 'file'
    return ''

def match_card_request(pending, request):
    """Match only the official action triggered under this operation's DOM lock."""
    u=urlsplit(request.url)
    if u.scheme!='https' or u.netloc!='pigeon.jinritemai.com' or u.path!='/backstage/sendCard' or request.method!='POST':return False
    try:
        body=request.post_data_json
        def obj(v):return json.loads(v) if isinstance(v,str) else v or {}
        if not isinstance(body,dict) or not body.get('security_conversation_id'):return False
        meta=pending['meta']
        if meta['kind']=='coupon':
            extra=obj(body.get('extra'))
            return body.get('card_type')=='kefu_coupon' and body.get('security_user_id')==pending['buyer'] and str(extra.get('coupon_meta_id'))==meta['id']
        if meta['kind']=='file':
            extra=obj(body.get('server_ext'))
            return (body.get('card_type')=='pdf_upload_card' and body.get('data_id')==meta['md5']
                    and extra.get('file_md5')==meta['md5'] and extra.get('file_name')==meta['name'] and str(extra.get('file_size'))==str(meta['size']))
    except (ValueError,AttributeError,TypeError):pass
    return False

async def observe_card_result(t,page,response,pending):
    if t._native_pending is not pending or response.request is not pending.get('http_request'):return
    try:
        if response.status!=200:return
        raw=await response.body()
        if len(raw)>65536:return
        value=json.loads(raw)
        if type(value.get('code')) is not int or value['code']!=0 or (value.get('data') or {}).get('success') is not True:return
        if (value.get('check_spam_resp') or {}).get('decision') not in (None,'','PASS','ALLOW'):return
        if t._official.principal!=pending['principal'] or pending.get('ambiguous'):return
        from feige_transport import DOM_JS
        if not (await page.evaluate(DOM_JS,{**pending['proof'],'action':'check'})).get('ok'):return
        pending['http_accepted']=True;pending['event'].set()
    except Exception:pass

def send_native(transport,expected,meta,raw=None):
    loop=transport._loop
    if not loop or not loop.is_running():raise ValueError('飞鸽尚未连接')
    f=asyncio.run_coroutine_threadsafe(_send(transport,expected,meta,raw),loop)
    try:return f.result(timeout=150)
    except concurrent.futures.TimeoutError:
        # Do not retry or cancel a potentially submitted upload.
        return {'status':'unknown','detail':'发送仍可能在官方处理中，请核对飞鸽，禁止重复提交'}

async def _send(t,expected,meta,raw):
    from feige_transport import DOM_JS,_feige_page
    if t._dom_lock is None:t._dom_lock=asyncio.Lock()
    async with t._dom_lock:
        talk=expected['talk'];identity=t._business_identities.get(talk)
        if not identity or identity['scope']!=expected['scope'] or hashlib.sha256(identity['buyer'].encode()).hexdigest()!=expected['buyer_hash']:
            raise ValueError('客户身份未核实，不能发送')
        await t._official.authenticate();principal=t._official.principal
        if 'shop_id:'+t._official.shop_id!=identity['scope']:raise ValueError('登录店铺已变化')
        anchor=t._manual_sources.get(talk);page=t._pages.get(anchor['page_id']) if anchor else None
        if not page or not _feige_page(page.url):raise ValueError('未读取到该会话的官方消息，不能发送')
        args={'talk':talk,'source_ids':anchor['ids'],'manual':True,'token':uuid.uuid4().hex}
        async def evaluate(action,**kw):return await page.evaluate(DOM_JS,{**args,'action':action,**kw})
        if not (await evaluate('check')).get('ok'):
            await evaluate('locate')
            await asyncio.sleep(.25)
        if not (await evaluate('check')).get('ok'):raise ValueError('请在官方飞鸽选中此客户；未能核实当前会话')
        pending={'kind':meta['kind'],'meta':meta,'proof':args,'principal':principal,'talk':talk,'scope':identity['scope'],'buyer':identity['buyer'],'page_id':id(page),'socket':None,'event':asyncio.Event(),'armed':True}
        t._native_pending=pending;submitted=False
        try:
            await t._official.verify_unchanged(principal)
            if t._business_identities.get(talk)!=identity:raise ValueError('客户身份已经变化')
            if meta['kind']=='coupon':
                result=await t._official.coupons({'offset':0},identity)
                matches=[c for c in result['items'] if c['id']==meta['id']]
                if not result['can_send'] or len(matches)!=1 or matches[0]['status']!=2:raise ValueError('当前优惠券不允许发送')
                coupon=matches[0]
                if coupon['start'] is None or coupon['end'] is None or not coupon['start']<=time.time()<=coupon['end'] or coupon['remaining']==0:
                    raise ValueError('优惠券未生效、已过期或剩余数量为零')
                if sum(c['name']==coupon['name'] for c in result['items'])!=1:raise ValueError('同名优惠券无法唯一定位，请在官方飞鸽发送')
                if not (await evaluate('native_coupon',coupon=coupon,probe=True)).get('ok'):
                    await page.locator('.i-icon-send-coupon-default, .i-icon-send-coupon-hover, .i-icon-send-coupon-selected').first.click(timeout=4000)
                for _ in range(20):
                    if (await evaluate('native_coupon',coupon=coupon,probe=True)).get('ok'):break
                    await asyncio.sleep(.25)
                else:raise ValueError('优惠券面板未显示唯一可发送券')
                submitted=True
                accepted=await evaluate('native_coupon',coupon=coupon)
            else:
                submitted=True
                accepted=await evaluate('native_upload',kind=meta['kind'],name=meta['name'],mime=meta['mime'],payload=base64.b64encode(raw).decode())
            if not accepted.get('ok'):
                submitted=False
                return {'status':'failed','detail':'官方页面未接受发送：'+accepted.get('reason','页面已变化')}
            if meta['kind']!='coupon':
                for _ in range(32):
                    if pending['event'].is_set():break
                    confirmation=await evaluate('native_confirm',kind=meta['kind'],name=meta['name'])
                    if confirmation.get('ok'):break
                    if confirmation.get('reason')!='preview_not_ready':
                        return {'status':'unknown','detail':'官方预览状态变化，已停止确认，请在飞鸽核对'}
                    await asyncio.sleep(.25)
            try:await asyncio.wait_for(pending['event'].wait(),100)
            except asyncio.TimeoutError:return {'status':'unknown','detail':'已交给官方页面处理，但未取得唯一匹配的服务端回声；请核对，勿重发'}
            if pending.get('http_accepted'):
                return {'status':'accepted','detail':'官方发送接口已返回成功；此类卡片没有可唯一关联的消息 ID，不代表买家已读或领券','talk':pending['talk']}
            return {'status':'confirmed','detail':'官方消息回声已确认；不代表买家已读或已领券','message_id':pending.get('message_id',''),'talk':pending['talk']}
        except Exception as exc:
            return {'status':'unknown' if submitted else 'failed','detail':('发送结果待核实，勿重发：' if submitted else '发送未提交：')+(str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__)}
        finally:
            t._native_pending=None
            if pending.get('client_id'):t._pending.pop(pending['client_id'],None)
            try:await evaluate('native_release')
            except Exception:pass
