"""POST /api/v1/assistant/messages: server-side tool loop for watch / voice clients."""

import json

import httpx

from app.core.security import generate_llm_token, hash_token
from app.integrations.mcp.assistant import FALLBACK_REPLY, MAX_STEPS
from tests.fakes.app import PEPPER, make_harness


LLM = {
    "llm_proxy_upstream_base_url": "https://llm.test/v1",
    "llm_proxy_upstream_api_key": "upstream-secret",
    "llm_proxy_model": "pinned",
}


class ScriptedLLM:
    """Returns the queued assistant messages in order; records every request body."""

    def __init__(self, *messages):
        self.queue = list(messages)
        self.bodies = []

    def handle(self, request):
        self.bodies.append(json.loads(request.content))
        message = self.queue.pop(0) if self.queue else {"role": "assistant", "content": "done"}
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 5}})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def tool_call(name, arguments, call_id="call_1"):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}]}


def say(text):
    return {"role": "assistant", "content": text}


def setup(*messages, **settings):
    llm = ScriptedLLM(*messages)
    h = make_harness(llm_client=llm.client(), **{**LLM, **settings})
    token, device_id, user_id = h.new_device()
    return h, llm, token, device_id


def send(h, token, **body):
    return h.client.post("/api/v1/assistant/messages", json=body, headers={"Authorization": f"Bearer {token}"})


def tool_messages(body):
    return [m for m in body["messages"] if m["role"] == "tool"]


def test_search_then_reply_uses_mcp_tools_and_pinned_model():
    h, llm, token, _ = setup(tool_call("search_products", {"query": "milk"}), say("Fresh milk is RM6.90."))
    r = send(h, token, text="find milk")
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Fresh milk is RM6.90."
    assert r.json()["order"] is None and r.json()["approval_required"] is False
    assert r.json()["products"][0]["sku"] == "GRC-MILK-1L"
    assert r.json()["products"][0]["price"] == "RM6.90"
    first, second = llm.bodies
    assert first["model"] == "pinned" and first["messages"][0]["role"] == "system"
    assert first["max_completion_tokens"] == 400 and "max_tokens" not in first
    assert {t["function"]["name"] for t in first["tools"]} == {
        "search_products", "create_order", "get_order_status", "get_spending_summary"}
    [result] = tool_messages(second)
    assert result["tool_call_id"] == "call_1" and "GRC-MILK-1L" in result["content"]


def test_deepseek_watch_tool_loop_uses_supported_token_limit():
    h, llm, token, _ = setup(
        tool_call("search_products", {"query": "milk"}), say("Fresh milk is RM6.90."),
        llm_proxy_upstream_base_url="https://api.deepseek.com/v1", llm_proxy_model="deepseek-flash",
    )
    response = send(h, token, text="find milk")
    assert response.status_code == 200 and response.json()["reply"] == "Fresh milk is RM6.90."
    assert len(llm.bodies) == 2
    for body in llm.bodies:
        assert body["model"] == "deepseek-flash" and body["max_tokens"] == 400
        assert "max_completion_tokens" not in body
        assert body["reasoning_effort"] == "none"


def test_create_order_is_bound_to_device_and_needs_approval():
    order_args = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 2}]}
    h, llm, token, device_id = setup(tool_call("create_order", order_args), say("Hold the button to pay RM13.80."))
    r = send(h, token, text="buy two milk")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["approval_required"] is True
    assert out["order"]["status"] == "awaiting_device_approval" and out["order"]["total"] == "RM13.80"
    stored = h.store.orders[out["order"]["order_id"]]
    assert stored["device_id"] == device_id and stored["status"] == "AWAITING_APPROVAL"
    assert len(llm.bodies) == 1  # no LLM round trip just to narrate a validated order
    # The claw's approval task finds it through the existing long-poll.
    pending = h.client.get(f"/api/v1/devices/{device_id}/pending-approvals",
                           headers={"Authorization": f"Bearer {token}"})
    assert [o["order_id"] for o in pending.json()["orders"]] == [out["order"]["order_id"]]


def test_history_is_remembered_per_device_and_reset_clears_it():
    h, llm, token, _ = setup(say("Hi!"), say("Still here."), say("Fresh start."))
    send(h, token, text="hello")
    send(h, token, text="are you there")
    assert [m["content"] for m in llm.bodies[1]["messages"][1:]] == ["hello", "Hi!", "are you there"]
    send(h, token, text="new topic", reset=True)
    assert [m["content"] for m in llm.bodies[2]["messages"][1:]] == ["new topic"]


def test_bad_tool_arguments_are_returned_to_the_model():
    h, llm, token, _ = setup(tool_call("search_products", "{not json"), tool_call("pay_now", {}, "call_2"),
                             say("Sorry."))
    r = send(h, token, text="x")
    assert r.status_code == 200 and r.json()["reply"] == "Sorry."
    assert "invalid_arguments" in tool_messages(llm.bodies[1])[0]["content"]
    assert "Unknown tool" in tool_messages(llm.bodies[2])[1]["content"]


def test_step_limit_returns_fallback():
    loops = [tool_call("search_products", {"query": "milk"}, f"c{i}") for i in range(MAX_STEPS + 1)]
    h, llm, token, _ = setup(*loops)
    r = send(h, token, text="loop")
    assert r.status_code == 200 and r.json()["reply"] == FALLBACK_REPLY
    assert len(llm.bodies) == MAX_STEPS


def test_spending_reply_uses_trusted_totals_without_another_model_call():
    h, llm, token, _ = setup(tool_call("get_spending_summary", {}))
    out = send(h, token, text="What did I spend today?", approver="watch").json()
    assert out["reply"] == "Spent RM0.00 today. RM500.00 available."
    assert out["order"] is None and out["approval_required"] is False
    assert len(llm.bodies) == 1


def test_auth_and_body_validation():
    h, llm, token, device_id = setup()
    llm_token = generate_llm_token()
    h.store.devices[device_id]["llm_token_hash"] = hash_token(PEPPER, llm_token)
    assert send(h, llm_token, text="hi").status_code == 401
    assert h.client.post("/api/v1/assistant/messages", json={"text": "hi"}).status_code == 401
    assert send(h, token, text="").status_code == 400
    assert send(h, token, text="x" * 501).status_code == 400
    assert send(h, token, text="hi", device_id="other").status_code == 400
    assert llm.bodies == []


def test_unconfigured_llm_and_mcp_disabled():
    h = make_harness()
    token, _, _ = h.new_device()
    r = send(h, token, text="hi")
    assert r.status_code == 503 and r.json()["error"]["code"] == "proxy_not_configured"
    off = make_harness(mcp_enabled=False, device_token_pepper=None)
    assert off.client.post("/api/v1/assistant/messages", json={"text": "hi"}).status_code == 404


class FailAfter(ScriptedLLM):
    """Scripted replies, then an upstream status for every later call."""

    def __init__(self, status, *messages):
        super().__init__(*messages)
        self.status = status

    def handle(self, request):
        if not self.queue:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        return super().handle(request)


def test_create_order_does_not_need_another_upstream_reply():
    order_args = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]}
    llm = FailAfter(429, tool_call("create_order", order_args))
    h = make_harness(llm_client=llm.client(), **LLM)
    token, _, _ = h.new_device()
    r = send(h, token, text="buy milk")
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "RM6.90. Hold your device button to approve."
    assert r.json()["approval_required"] is True and len(llm.bodies) == 1
    assert len(h.store.orders) == 1


def test_upstream_400_becomes_502_and_clears_history():
    llm = FailAfter(400, say("Hi!"))
    h = make_harness(llm_client=llm.client(), **LLM)
    token, _, _ = h.new_device()
    assert send(h, token, text="hello").status_code == 200
    r = send(h, token, text="again")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"
    llm.queue.append(say("Fresh."))
    send(h, token, text="third")
    assert [m["content"] for m in llm.bodies[-1]["messages"][1:]] == ["third"]


def test_malformed_tool_calls_are_sanitized_before_going_upstream():
    weird = {"role": "assistant", "content": None, "tool_calls": [
        "not a dict", {"function": {"name": "search_products", "arguments": "[" * 100000}}]}
    h, llm, token, _ = setup(weird, say("ok"))
    r = send(h, token, text="x")
    assert r.status_code == 200 and r.json()["reply"] == "ok"
    sent = llm.bodies[1]["messages"]
    calls = next(m for m in sent if m.get("tool_calls"))["tool_calls"]
    replies = [m for m in sent if m["role"] == "tool"]
    assert [c["id"] for c in calls] == [m["tool_call_id"] for m in replies]
    assert all(isinstance(c, dict) and c["function"]["arguments"] == "{}" for c in calls)
    assert all("invalid_arguments" in m["content"] for m in replies)


def test_history_is_isolated_between_devices():
    h, llm, token, _ = setup(say("A"), say("B"))
    other, _, _ = h.new_device()
    send(h, token, text="from one")
    send(h, other, text="from two")
    assert [m["content"] for m in llm.bodies[1]["messages"][1:]] == ["from two"]


def test_approver_hint_changes_only_the_instructions():
    h, llm, token, _ = setup(say("a"), say("b"))
    send(h, token, text="hi")
    send(h, token, text="hi again", approver="watch", reset=True)
    device_prompt, watch_prompt = (b["messages"][0]["content"] for b in llm.bodies)
    assert "holding the button on their ClawPay device" in device_prompt
    assert "review and confirm it on their watch" in watch_prompt and "{" not in watch_prompt
    assert send(h, token, text="x", approver="phone").status_code == 400


def test_watch_registered_as_own_device_can_approve_its_assistant_order():
    """The watch flow: own key + own token, order via the assistant, signed challenge -> paid."""
    from tests.fakes.app import ADMIN_TOKEN
    from tests.fakes.approvals import generate_p256, get_challenge, sign_canonical
    from tests.fakes.paypal import FakePayPalTransport

    order_args = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 2}]}
    llm = ScriptedLLM(tool_call("create_order", order_args))
    h = make_harness(llm_client=llm.client(), paypal_transport=FakePayPalTransport(),
                     paypal_client_id="fake", paypal_client_secret="fake-secret", **LLM)
    private, pem = generate_p256()
    watch = h.client.post("/api/v1/devices/register", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                          json={"user_id": h.new_user(), "device_name": "apple-watch", "public_key_pem": pem}).json()
    out = send(h, watch["device_token"], text="buy two milk", approver="watch").json()
    assert out["reply"] == "RM13.80. Review and confirm on your watch."
    order_id = out["order"]["order_id"]
    challenge = get_challenge(h, watch["device_token"], order_id)
    body = {"challenge_id": challenge["challenge_id"], "device_id": watch["device_id"],
            "key_version": challenge["key_version"], "nonce": challenge["nonce"], "decision": "approved",
            "signature": sign_canonical(private, challenge["canonical"])}
    auth = {"Authorization": f"Bearer {watch['device_token']}"}
    # A signature from any other key (e.g. the claw's) is rejected and nothing is charged.
    other_private, _ = generate_p256()
    forged = {**body, "signature": sign_canonical(other_private, challenge["canonical"])}
    r = h.client.post(f"/api/v1/orders/{order_id}/gadget-approval", json=forged, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "signature_invalid"
    r = h.client.post(f"/api/v1/orders/{order_id}/gadget-approval", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "paid", r.text
    # A later voice status query must update the watch's card from trusted tool data.
    llm.queue.extend([tool_call("get_order_status", {"order_id": order_id}), say("Paid RM13.80.")])
    status = send(h, watch["device_token"], text="Check my payment", approver="watch")
    assert status.status_code == 200, status.text
    assert status.json()["order"]["order_id"] == order_id
    assert status.json()["order"]["status"] == "paid"
    assert status.json()["order"]["total"] == "RM13.80"
    assert status.json()["approval_required"] is False
    assert status.json()["reply"] == "Paid RM13.80." and len(llm.bodies) == 2


def test_stream_reports_actual_tools_and_one_final_cart_without_reasoning_tokens():
    h, llm, token, _ = setup(
        tool_call("search_products", {"query": "milk"}),
        tool_call("create_order", {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]}),
    )
    response = h.client.post('/api/v1/assistant/messages/stream', json={'text':'Buy one milk', 'approver':'watch'},
                             headers={'Authorization':f'Bearer {token}'})
    assert response.status_code == 200 and response.headers['cache-control']=='no-store'
    events = [json.loads(line) for line in response.text.splitlines()]
    progress = [e for e in events if e['type']=='progress']
    assert [(e['stage'],e['status']) for e in progress] == [
        ('assistant','working'),('search_products','working'),('search_products','complete'),
        ('assistant','working'),('create_order','working'),('create_order','complete')]
    results = [e['data'] for e in events if e['type']=='result']
    assert len(results)==1 and results[0]['approval_required'] is True
    assert results[0]['order']['items']==[{'name':'Fresh milk 1L','qty':1,'line_total':'RM6.90'}]
    assert len(h.store.orders)==1 and len(llm.bodies)==2
    assert 'reasoning_content' not in response.text and 'upstream-secret' not in response.text
    assert h.client.post('/api/v1/assistant/messages/stream',json={'text':'x'}).status_code==401
    assert h.client.post('/api/v1/assistant/messages/stream',json={'text':''},headers={'Authorization':f'Bearer {token}'}).status_code==400


def test_stream_surfaces_upstream_error_without_automatic_resubmission():
    llm=FailAfter(429)
    h=make_harness(llm_client=llm.client(),**LLM)
    token,_,_=h.new_device()
    response=h.client.post('/api/v1/assistant/messages/stream',json={'text':'hello'},headers={'Authorization':f'Bearer {token}'})
    events=[json.loads(line) for line in response.text.splitlines()]
    assert events[-1]['type']=='error' and events[-1]['error']['code']=='rate_limited'
    assert len(llm.bodies)==1 and not h.store.orders


def test_first_progress_reaches_client_while_model_is_still_waiting():
    import anyio

    async def check():
        release=anyio.Event()
        seen=anyio.Event()
        finished=anyio.Event()
        async def provider(_):
            await release.wait()
            return httpx.Response(200,json={'choices':[{'message':say('Hello.')}],'usage':{'total_tokens':2}})
        h=make_harness(llm_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),**LLM)
        token,_,_=h.new_device()
        raw=json.dumps({'text':'hello','approver':'watch'}).encode()
        used=False
        async def receive():
            nonlocal used
            if not used:
                used=True
                return {'type':'http.request','body':raw,'more_body':False}
            await anyio.sleep_forever()
        frames=[]
        async def send_frame(message):
            if message['type']=='http.response.body':
                frames.append(message.get('body',b''))
                if b'"type":"progress"' in message.get('body',b''):seen.set()
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'method':'POST','path':'/api/v1/assistant/messages/stream',
               'raw_path':b'/api/v1/assistant/messages/stream','query_string':b'','scheme':'http','http_version':'1.1',
               'server':('test',80),'client':('127.0.0.1',123),'headers':[(b'authorization',f'Bearer {token}'.encode()),(b'content-type',b'application/json')]}
        async def request():
            await h.app(scope,receive,send_frame)
            finished.set()
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            with anyio.fail_after(2):await seen.wait()
            assert not finished.is_set() and b'"type":"result"' not in b''.join(frames)
            release.set()
            with anyio.fail_after(2):await finished.wait()
            assert b'"type":"result"' in b''.join(frames)
        await h.services.llm.client.aclose()

    anyio.run(check)
