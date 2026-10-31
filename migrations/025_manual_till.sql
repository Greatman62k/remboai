-- Manual till (two-tier deposits): a salon whose money goes to a personal M-Pesa
-- number with no Daraja API. No STK push is ever sent for these; the customer
-- claims the payment (amount + M-Pesa code) and only the owner's tap confirms.
ALTER TABLE salon_payment ADD COLUMN IF NOT EXISTS manual boolean NOT NULL DEFAULT false;

-- Which rails a payment row uses. The sweeper's "never sent" write-off only
-- applies to STK rows; manual rows wait for the owner, not for Safaricom.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS method text NOT NULL DEFAULT 'stk'
    CHECK (method IN ('stk', 'manual'));
-- What the customer pasted or typed with a manual claim, for the owner's eyes.
-- It is reference only: a claim never confirms a booking by itself.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS claim_ref text;
-- An owner tap confirms just like a callback or a status query.
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_settled_via_check;
ALTER TABLE payments ADD CONSTRAINT payments_settled_via_check CHECK (settled_via IN ('callback', 'query', 'manual'));
