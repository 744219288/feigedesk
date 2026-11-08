"""Read-only adapters for observed Feige HTTP message protobuf responses.

The browser owns login, signing, cursors and heartbeat. No credential export and
no guessed write endpoint. Both HTTP and WebSocket ingress use one message schema.
"""
from urllib.parse import urlsplit
from vendor.feige import ws_reader, ws_sender
import re
from media_store import validate_url

MESSAGE_PATHS = frozenset(('/pigeon_im/v1/message/get_message_by_init',
                           '/pigeon_im/v3/message/get_user_message',
                           '/pigeon_im/v1/message/get_by_conversation'))
MAX_BODY = 8*1024*1024


def message_endpoint(url):
    try:
        p = urlsplit(url)
        if p.scheme == 'https' and p.hostname == 'fxg.jinritemai.com' and p.port in (None,443) and not p.username and not p.password and p.path in MESSAGE_PATHS:
            return p.path
    except (TypeError,ValueError):
        pass
    return None


def extract_api_messages(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_BODY:
        raise ValueError('message_api_size')
    dec=ws_reader.decode(raw)
    # Captured response envelope: field 3 is the status; zero means success.
    if not dec or ws_reader._all(dec,3) != [0]:
        raise ValueError('message_api_status')
    result=[]
    visited=0
    def walk(fields,depth=0):
        nonlocal visited
        visited+=1
        if visited>20000 or depth>10:
            raise ValueError('message_api_complexity')
        kv=ws_reader._kvmap(fields)
        if kv.get('sender_role') in ('1','2') and kv.get('talk_id'):
            mid=ws_reader._all(fields,3)
            ts=ws_reader._all(fields,10)
            if len(mid)!=1 or not isinstance(mid[0],int) or mid[0]<=0 or len(ts)!=1 or not isinstance(ts[0],int) or ts[0]<=0:
                return
            msg=lambda n,v:(n,2,('msg',v))
            # Reuse the tested message-field parser; only the HTTP envelope differs.
            wrapped=ws_sender.encode([msg(8,[msg(6,[msg(500,[msg(5,fields)])])])])
            result.extend(ws_reader.extract_messages(wrapped))
            return
        for _,_,value in fields:
            sub=ws_reader._sub(value)
            if sub:
                walk(sub,depth+1)
    for value in ws_reader._all(dec,6):
        sub=ws_reader._sub(value)
        if sub:
            walk(sub)
    return sorted(result,key=lambda m:(m.ts_ms,m.msg_id))


async def resolve_video(request, vid):
    """Official front-end flow, verified on this shop: token -> play metadata."""
    if not isinstance(vid,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,128}',vid):
        raise ValueError('invalid_video_id')
    response = await request.get('https://pigeon.jinritemai.com/backstage/video/getPlayToken',
                                 params={'vid':vid},timeout=15000,max_redirects=0)
    try:
        if response.status!=200:
            raise ValueError('video_authorization_failed')
        raw=await response.body()
        if len(raw)>65536:
            raise ValueError('video_response_too_large')
        import json
        envelope=json.loads(raw)
        token=envelope.get('data',{}).get('token') if envelope.get('code')==0 else None
        if not isinstance(token,str) or not token or len(token)>16000 or any(c in token for c in '\r\n#'):
            raise ValueError('video_authorization_failed')
    finally:
        await response.dispose()
    response=await request.get('https://open.bytedanceapi.com/?'+token,timeout=15000,max_redirects=0)
    try:
        if response.status!=200:
            raise ValueError('video_metadata_failed')
        raw=await response.body()
        if len(raw)>1024*1024:
            raise ValueError('video_response_too_large')
        details=json.loads(raw).get('Result',{}).get('Data',{})
        if details.get('Status')!=10:
            raise ValueError('video_not_ready_or_expired')
        for info in details.get('PlayInfoList',[]):
            url=info.get('MainPlayUrl')
            try:
                validate_url(url)
                return url
            except (ValueError,TypeError):
                continue
        raise ValueError('video_source_unavailable')
    finally:
        await response.dispose()
