-- A manual till has no Daraja shortcode. Keep only the till number in party_b.
ALTER TABLE salon_payment ALTER COLUMN shortcode DROP NOT NULL;
UPDATE salon_payment SET shortcode = NULL WHERE manual = true;
ALTER TABLE salon_payment
    ADD CONSTRAINT salon_payment_shortcode_manual_check
    CHECK ((manual = true AND shortcode IS NULL) OR (manual = false AND shortcode IS NOT NULL));

COMMENT ON COLUMN salon_payment.shortcode IS
    'Daraja BusinessShortCode; NULL for manual accounts, which store only the till number in party_b.';
