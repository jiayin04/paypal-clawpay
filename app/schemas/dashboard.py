"""Public column allowlists shared by dashboard repositories."""
CONVERSATION_COLUMNS = 'id,device_id,source,status,created_at,updated_at'
EVENT_COLUMNS = 'id,order_id,correlation_id,event_type,summary,created_at'
A2A_COLUMNS = 'id,merchant_code,direction,method,outcome,latency_ms,summary,created_at'
AUDIT_COLUMNS = 'id,actor_type,event_type,entity_type,entity_id,created_at'
METHOD_COLUMNS = 'id,provider,status,payer_email_masked,created_at,updated_at,revoked_at'
# Summary values were validated when persisted. An allowlist also protects older rows.
SUMMARY_KEYS = frozenset(('tool','tool_name','status','reason','message','merchant_code','query','product_count','result_count','total_minor','currency','order_id','outcome','count','error_code','error','name','max_results','deduplicated','result','amount_minor'))

def safe_summary(value):
    if isinstance(value,dict):
        return {k:safe_summary(v) for k,v in value.items() if k in SUMMARY_KEYS}
    if isinstance(value,list):
        return [safe_summary(v) for v in value]
    return value

def public_rows(rows):
    return [{k:(safe_summary(v) if k == 'summary' else v) for k,v in row.items()} for row in rows]
