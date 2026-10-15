-- Which system each station is in, for the "station filed under the wrong system" pairing check.
ALTER TABLE facts_station
    ADD COLUMN star_system    text,
    ADD COLUMN system_address bigint,
    ADD COLUMN system_seen    timestamptz;
