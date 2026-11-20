-- Businesses with similar or identical names are told apart by an area ("Zuri Cuts (Westlands)"), set by the owner with `area <text>`.
ALTER TABLE salons ADD COLUMN IF NOT EXISTS area text;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'salons_area_len') THEN
        ALTER TABLE salons ADD CONSTRAINT salons_area_len CHECK (area IS NULL OR char_length(area) BETWEEN 2 AND 40);
    END IF;
END $$;
