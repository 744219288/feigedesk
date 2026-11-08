"""Read-only order -> SKU order -> aftersale associations from official responses."""
from commerce_data import ident, text

PRECHECK_KEYS={'aftersale_refund_refund_agree','aftersale_return_logistics_agree',
               'presale_agree','aftersale_return_receive_agree','aftersale_refund_refund_refuse'}

def sequence(value):
    if not isinstance(value,list) or len(value)>500:raise ValueError('售后列表结构变化')
    return value

def query_tree(orders):
    result=[];allowed={}
    for order in sequence(orders):
        oid=ident(order.get('order_id') or order.get('shop_order_id'));children=[]
        for sku in sequence(order.get('sku_order_list',[])):
            sid=ident(sku.get('sku_order_id'));leaves=[]
            for case in sequence(sku.get('after_sale_orders',[])):
                aid=ident(case.get('after_sale_id'))
                if aid in allowed:raise ValueError('售后编号重复')
                allowed[aid]=(oid,sid,case)
                leaves.append({k:case[k] for k in ('after_sale_id','after_sale_status') if k in case})
            if leaves:children.append({'sku_order_id':sid,'query_list':leaves})
        if children:
            item={k:order[k] for k in ('order_status','order_biz_type') if k in order}
            item.update(shop_order_id=oid,query_list=children);result.append(item)
    return result,allowed

def precheck_actions(case):
    found={}
    def walk(v,depth=0):
        if depth>10:return
        if isinstance(v,dict):
            key=v.get('action_key');action=v.get('action_type')
            if key in PRECHECK_KEYS and type(action) is int and action>=0:
                found[key]={'key':key,'name':text(v.get('button_name'),100) or key,'action_type':action}
            for x in v.values():walk(x,depth+1)
        elif isinstance(v,list):
            for x in v[:500]:walk(x,depth+1)
    walk(case)
    return list(found.values())

def merge_details(value,allowed):
    result={aid:dict(case) for aid,(_,_,case) in allowed.items()}
    for order in sequence(value.get('info_list')):
        oid=ident(order.get('shop_order_id'))
        for sku in sequence(order.get('info_list')):
            sid=ident(sku.get('sku_order_id'))
            for case in sequence(sku.get('info_list')):
                aid=ident(case.get('after_sale_id'))
                if aid not in allowed or allowed[aid][:2]!=(oid,sid):raise ValueError('售后与当前客户订单归属不符')
                result[aid].update(case)
    items=[]
    fields=('after_sale_status','after_sale_status_desc','status_desc','after_sale_type_desc',
            'refund_amount_desc','refund_amount_str','reason','apply_reason','governance_desc','arbitrate_desc','update_time')
    for aid,case in result.items():
        item={k:str(case[k])[:3000] for k in fields if isinstance(case.get(k),(str,int,float))}
        item.update(after_sale_id=aid,order_id=allowed[aid][0],sku_order_id=allowed[aid][1],precheck_actions=precheck_actions(case))
        items.append(item)
    return items
