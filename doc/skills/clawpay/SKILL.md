---
{
  "name": "clawpay",
  "description": "Shop from ClawPay merchants and pay with PayPal after on-device button approval.",
  "metadata": {
    "cap_groups": [
      "cap_mcp_client"
    ]
  }
}
---
# ClawPay Shopping Skill

Use this skill when the user wants to search for products, buy items, check order status, or check daily spending.

## MCP Server Connection
Always call ClawPay with `mcp_call_tool` using:
- **`server_url`**: `https://<YOUR-TUNNEL-OR-HOST-URL>` (e.g. `https://clawpay.yourdomain.com` or your cloudflared/ngrok URL, without trailing slash)
- **`endpoint`**: `mcp/<YOUR_DEVICE_TOKEN>` (for path-token authentication) or `mcp` (if using Authorization bearer header)

---

## Available Tools & Arguments

### 1. `search_products`
Search merchant catalogs across active demo merchants (`grocer`, `electronics`).
- **Arguments:**
  - `query` *(string, required)*: Search keywords (e.g., `"milk"`, `"cable"`, `"coffee"`).
  - `max_results` *(integer, optional, 1–10, default 5)*: Number of items to return.
  - `merchant_id` *(string, optional)*: Filter by merchant code (`"grocer"` or `"electronics"`).
- **Example Call:**
  ```json
  {
    "tool_name": "search_products",
    "arguments": {
      "query": "milk",
      "max_results": 5,
      "merchant_id": "grocer"
    }
  }
  ```

### 2. `create_order`
Create an unpaid order with items from a single merchant.
- **Arguments:**
  - `merchant_id` *(string, required)*: `"grocer"` or `"electronics"`.
  - `items` *(array of objects, required)*:
    - `sku` *(string)*: Exact SKU from `search_products`.
    - `qty` *(integer, 1–20)*: Quantity to purchase.
  - `currency` *(string, optional)*: `"MYR"` (defaults to MYR).
- **Example Call:**
  ```json
  {
    "tool_name": "create_order",
    "arguments": {
      "merchant_id": "grocer",
      "items": [
        {"sku": "GRC-MILK-1L", "qty": 2}
      ],
      "currency": "MYR"
    }
  }
  ```
- **Response contains:** `order_id`, `subtotal`, `total`, `quote_expires_at`.

### 3. `get_order_status`
Check the authoritative status of an order.
- **Arguments:**
  - `order_id` *(string UUID, required)*: The `order_id` returned from `create_order`.
- **Example Call:**
  ```json
  {
    "tool_name": "get_order_status",
    "arguments": {
      "order_id": "7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11"
    }
  }
  ```
- **Statuses:**
  - `awaiting_device_approval`: Waiting for user approval on the ESP-Claw device.
  - `payment_pending`: Approval received; PayPal payment is processing.
  - `paid`: Payment successfully captured and completed.
  - `payment_failed`: Payment declined or failed.
  - `cancelled` / `expired` / `denied`: Order concluded without payment.

### 4. `get_spending_summary`
Query daily spending against user spending limits.
- **Arguments:**
  - `period` *(string, optional)*: `"today"`.
- **Example Call:**
  ```json
  {
    "tool_name": "get_spending_summary",
    "arguments": {
      "period": "today"
    }
  }
  ```

---

## Operating Instructions & Conversation Rules

1. **Catalog Integrity:**
   - Only use `sku` and `merchant_id` values returned by `search_products`. Never invent products, SKUs, prices, or totals.
   - Always confirm the item details, quantity, and total price with the user before calling `create_order`.

2. **Single Merchant Constraint:**
   - Orders can only contain items from **one** merchant at a time. If the user wants items from different merchants, place separate orders.

3. **Approval Procedure:**
   - Immediately after `create_order` succeeds, notify the user:
     > *"Your order for [Items] totalling [Total] has been created. Please check the screen on your Claw device and hold the button to approve the payment."*
   - If the firmware tool `clawpay_request_approval` is present on the device, call it with `{"order_id": "<order_id>"}`.

4. **Payment Confirmation Rule:**
   - **NEVER** state that payment succeeded unless `get_order_status` returns `status: "paid"`.
   - If status is `payment_pending`, inform the user that payment is still being processed and check back shortly.

5. **Security & Privacy:**
   - Never expose device tokens, private keys, hashes, or internal IDs in chat responses or tool arguments.
