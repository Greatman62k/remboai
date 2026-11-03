-- Manual deposit destinations selected during salon registration.
-- These are customer-to-salon transfers; RemboAI does not initiate or hold them.
ALTER TABLE salon_payment
    DROP CONSTRAINT IF EXISTS salon_payment_kind_check;

ALTER TABLE salon_payment
    ADD CONSTRAINT salon_payment_kind_check
    CHECK (kind IN ('till', 'paybill', 'phone'));

ALTER TABLE salon_payment
    ADD COLUMN IF NOT EXISTS phone_type text;

ALTER TABLE salon_payment
    ADD CONSTRAINT salon_payment_phone_type_check
    CHECK (
        (kind = 'phone' AND manual = true AND shortcode IS NULL
            AND phone_type IS NOT NULL
            AND phone_type IN ('pochi', 'send_money', 'airtel_money')
            AND party_b ~ '^254[17][0-9]{8}$')
        OR (kind <> 'phone' AND phone_type IS NULL)
    ) NOT VALID;

ALTER TABLE salon_payment
    ADD CONSTRAINT salon_payment_manual_till_number_check
    CHECK (NOT (manual = true AND kind = 'till') OR party_b ~ '^[0-9]{7}$') NOT VALID;

COMMENT ON COLUMN salon_payment.phone_type IS
    'Manual phone destination: Pochi la Biashara, M-PESA Send Money, or Airtel Money.';

-- A reference can repeat across different payment networks. Scope de-duplication to its provider.
ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'mpesa'
    CHECK (provider IN ('mpesa', 'airtel_money'));

DROP INDEX IF EXISTS payments_receipt;
CREATE UNIQUE INDEX payments_provider_receipt
    ON payments (provider, mpesa_receipt) WHERE mpesa_receipt IS NOT NULL;
