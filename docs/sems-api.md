# SEMS portal API notes

GoodWe's SEMS portal has no public API for end users. The history importer
(`src/solar/sems.py`) follows what open-source integrations do as of 2026:

- Home Assistant: [TimSoethout/goodwe-sems-home-assistant](https://github.com/TimSoethout/goodwe-sems-home-assistant) (`sems_api.py`)
- ioBroker: [bueste/ioBroker.goodwe-sems](https://github.com/bueste/ioBroker.goodwe-sems)
- [intelseb/goodwesemsplus](https://github.com/intelseb/goodwesemsplus) (historical curves)

## SEMS+ gateway (current for EU accounts)

| Purpose | Request |
|---|---|
| Login | `POST https://eu-semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login` — body `{account, pwd: base64(md5hex(password)), agreement: 1, isChinese: false, isLocal: false}`; response `data` holds `uid, token, timestamp, api, region` |
| Auth headers | `token: <json of login data, client "semsPlusWeb">`, `X-Signature: base64(sha256("{ms}@{uid}@{token}") + "@{ms}")` — fresh per request |
| Stations | `POST {api}/sems-plant/api/portal/stations/page` `{current: 1, size: 100}` |
| Daily / yearly energy | `POST {api}/sems-plant/api/stations/statistics` with `dimension` `day` (one month per call) or `year` (one year per call); items `proSystemTotalStats` (PV), `proConsumStats` (load), `proGridStats` (export), `proPurchaseStats` (import), `proCharStats`, `proDischarStats` |
| Intraday curves | `POST {api}/sems-plant/api/v1/hems/power/statisticsAndPreV2` — items `pSystem, pConsum, pGrid, pBat, soc`, one local day per call, `timeZone` = UTC offset in hours |

Error codes: `100002` / `C0602` → log in again once; `GY0429` or HTTP 429 → back
off ≥ 5 min. The importer paces requests at one per 5 s and stores a resume cursor
in the database.

Units and signs of the intraday items are not documented. `solar sems-probe` fetches
one day and compares it bucket-by-bucket with locally measured data before a full
import; `SEMS_POWER_SCALE`, `SEMS_GRID_SIGN` and `SEMS_BATTERY_SIGN` correct them.
