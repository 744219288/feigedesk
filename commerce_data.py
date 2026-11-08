"""Normalize observed official product data; keep static knowledge separate from prices."""
import time

def ident(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not str(value).isdigit():
        raise ValueError('官方业务编号无效')
    return str(value)

def text(value,limit=2000):
    return value.strip()[:limit] if isinstance(value,str) else ''

def amount(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None

def product_rows(value,shop_id):
    if not isinstance(value,list) or len(value)>100:raise ValueError('官方商品列表结构变化')
    result=[]
    for raw in value:
        p=raw.get('product_item') if isinstance(raw,dict) else None
        if not isinstance(p,dict):raise ValueError('官方商品详情结构变化')
        if ident(p.get('shop_id'))!=str(shop_id):raise ValueError('商品店铺归属不一致')
        base=p.get('product_base_info') or {};pid=ident(p.get('product_id'))
        if ident(base.get('product_id'))!=pid:raise ValueError('商品详情编号不一致')
        properties=[]
        for a in base.get('property_value_pair',[]):
            prop=a.get('Property') or {}
            if prop.get('notInConsumer'):continue
            name=text(prop.get('PropertyName'),100)
            vals=[text(v.get('ValueName'),500) for v in a.get('Values',[])]
            if name and vals:properties.append({'name':name,'values':vals})
        si=p.get('sku_info') or {};stock=si.get('sku_stock_info') or {};prices=si.get('sku_marketing_info') or {}
        skus=[]
        for s in base.get('skus',[]):
            sid=ident(s.get('id'))
            if ident(s.get('product_id'))!=pid:raise ValueError('SKU 商品归属不一致')
            specs=[]
            for pair in s.get('sale_property_value_pair',[]):
                specs.append({'name':text((pair.get('Property') or {}).get('PropertyName'),100),
                              'value':text((pair.get('Value') or {}).get('ValueName'),500)})
            price=prices.get(sid) or {};st=stock.get(sid) or {}
            if price and ident(price.get('sku_id'))!=sid:raise ValueError('SKU 价格归属不一致')
            if st and ident(st.get('sku_id'))!=sid:raise ValueError('SKU 库存归属不一致')
            display=((price.get('show_sku_info') or {}).get('show_price') or {}).get('show_price') or {}
            skus.append({'id':sid,'specs':specs,'display_price':text(display.get('show_amount'),100),
                         'price_prefix':text(display.get('price_prefix'),10),'stock':amount(st.get('normal_stock_num_sum'))})
        logistics=(p.get('promise_info') or {}).get('logistics') or {}
        result.append({'id':pid,'shop_id':str(shop_id),'title':text(base.get('title'),300),'properties':properties,
                       'skus':skus,'stock':amount((base.get('stock_info') or {}).get('normal_stock_num_sum')),
                       'shipping_description':text(logistics.get('info'),500),
                       'source':'飞鸽官方商品面板','fetched_at':time.time(),
                       'price_scope':'页面展示价格，不等于客户结算价；库存及发货信息仅代表查询时状态',
                       'knowledge_note':'通用商品属性与销售规格分别保留；数量、尺寸等存在冲突时须核实，不自行合并或推断。'})
    if len({p['id'] for p in result})!=len(result):raise ValueError('商品列表出现重复编号')
    return result

def stable_knowledge(product):
    return {'id':product['id'],'shop_id':product['shop_id'],'title':product['title'],
            'properties':product['properties'],'skus':[{'id':s['id'],'specs':s['specs']} for s in product['skus']],
            'source':product['source'],'fetched_at':product['fetched_at'],'knowledge_note':product['knowledge_note']}

def coupon_rows(data,shop_id):
    if not isinstance(data,dict) or not isinstance(data.get('shop_meta_list'),list):raise ValueError('官方优惠券结构变化')
    result=[]
    for c in data['shop_meta_list']:
        if ident(c.get('shop_id'))!=str(shop_id):raise ValueError('优惠券店铺归属不一致')
        cid=ident(c.get('coupon_meta_id'));stats=(data.get('statistic_map') or {}).get(cid) or {}
        result.append({'id':cid,'name':text(c.get('coupon_meta_name'),300),'status':c.get('status'),
                       'credit_fen':amount(c.get('credit')),'threshold_fen':amount(c.get('threshold')),
                       'start':amount(c.get('start_apply_time')),'end':amount(c.get('end_apply_time')),
                       'remaining':amount(stats.get('left_amount')),'coupon_type':c.get('coupon_type'),
                       'note':'仅发送现有券卡片；领取成功、适用商品和最终结算优惠由官方判断。'})
    return result
