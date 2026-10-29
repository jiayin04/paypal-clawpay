-- ClawPay: Channel3 marketplace merchant (real product catalog via the Channel3 API).
--
-- Data only: no tables, functions or grants change. The merchant row is needed
-- because orders reference merchants by id; whether ClawPay actually calls the
-- Channel3 agent is decided by CHANNEL3_API_KEY / CHANNEL3_AGENT_ENDPOINT.

insert into public.merchants (merchant_code, display_name, status) values
  ('channel3', 'Channel3 Marketplace', 'active')
on conflict (merchant_code) do nothing;
