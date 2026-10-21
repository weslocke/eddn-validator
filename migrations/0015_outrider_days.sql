-- Finished days of the private Outrider page, worked out once a night (outrider.refresh_days):
-- other players' messages and the estimated commanders, so the page only works out today.
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS outrider_days (
    app_id      integer     NOT NULL,
    day         date        NOT NULL,      -- local (New York) day
    messages    integer     NOT NULL,      -- other players' messages that day
    commanders  integer     NOT NULL,      -- estimated other commanders that day
    computed_at timestamptz NOT NULL,
    PRIMARY KEY (app_id, day)
);
