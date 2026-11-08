"""Allowlisted OpenAI request shaping; prompts are never logged."""
from urllib.parse import urlsplit

from app.core.exceptions import ApiError
KEYS = frozenset(('messages','tools','tool_choice','parallel_tool_calls','temperature','top_p','stop','response_format','stream','stream_options','seed','reasoning_effort','max_tokens','max_completion_tokens'))

def shape_request(body, settings):
    def invalid():
        raise ApiError(400,'invalid_request_error','Invalid chat completion request.')
    if not isinstance(body,dict) or not isinstance(body.get('messages'),list) or not body['messages']:
        invalid()
    result = {k:v for k,v in body.items() if k in KEYS}
    result['model'] = settings.llm_proxy_model
    if 'stream' in result and not isinstance(result['stream'],bool):
        invalid()
    for key in ('max_tokens','max_completion_tokens'):
        if key in result:
            if type(result[key]) is not int or result[key] < 1:
                invalid()
            result[key] = min(result[key],settings.llm_proxy_max_tokens)
    if not any(k in result for k in ('max_tokens','max_completion_tokens')):
        result['max_tokens'] = settings.llm_proxy_max_tokens
    # DeepSeek documents max_tokens, while OpenAI uses max_completion_tokens.
    if urlsplit(settings.llm_proxy_upstream_base_url or '').hostname == 'api.deepseek.com':
        result['max_tokens'] = min(result[k] for k in ('max_tokens','max_completion_tokens') if k in result)
        result.pop('max_completion_tokens', None)
        # Voice replies use the non-thinking tool loop; callers can opt into reasoning.
        result.setdefault('reasoning_effort', 'none')
    options = result.get('stream_options',{})
    if not isinstance(options,dict) or ('include_usage' in options and type(options['include_usage']) is not bool):
        invalid()
    usage_requested = options.get('include_usage',False)
    if 'stream_options' in result:
        result['stream_options'] = {k:v for k,v in options.items() if k == 'include_usage'}
    if result.get('stream'):
        result['stream_options'] = {'include_usage':True}
    return result, usage_requested
