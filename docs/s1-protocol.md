# S1 / S1 Plus implementation notes

## Evidence boundary

Analyzed artifact: user-provided SwitchBot Android 9.11.15.13 / versionCode
40000051. Base APK SHA-256:
`cd186f0a3e47c7ec8ec6f53882bae5f680eb05e90f66ab6b8aa698c44dfa3304`.
ARM64 `libapp.so` SHA-256:
`27d5d4f2ef738ebc5b650e020b65a45d0dc936e91839851c73be29dabb30615c`.

Static analysis, not hardware testing:

| Observation | Shipped code |
|---|---|
| S1 type / product key | `DeviceType.WoVacuumS1 = WoSweeper`; `SWEEPER360.PRODUCT_KEY_S1 = 189c56a889be` |
| S1 Plus type / product key | `WoSweeperPlus`; `0f698f178cba` |
| Count toggle | `SweeperPropertyModel.setCleanTimes`, entry `0x4077c4`, async body `0x40793c` |
| Device setting | `SweeperUseCase.setWorkRepeatTimes`, entry `0x3bab68`, async body `0x3bace4`; `0x3badc4` loads `WorkRepeatTimes` (PP+0x1f4e8, string ref 0x15d83) |
| Property transport | `SweeperRepositoryImpl.setIntProperty` (`0x2903b0`) → `TslUseCase.setIntProperty` (`0x28a048`) → TSL remote datasource |
| SDK HTTP route | `0x1e1ab4`: `/v1/iot/device/set_property`; content type at `0x1e1a7c`: form-urlencoded |
| Task-specific input | `startDefaultClean` async body: `0x28cc64` loads `CleanTimes`, `0x28ccc0` loads `StartDefaultClean`, `0x28ccd0` calls `TslUseCase.executeService` |
| SDK region discovery | Java `VacuumUtil` region initialization; `DeviceService`: GET `/wonder/sweeper360/v1/user/getRegion`; first returned `hosts` entry |
| Delegated authorization | Java `VacuumUtil` / `VacuumTokenRep`; POST `/wonder/sweeper360/v1/auth/applyToken`, selected family `groupID`, `token` and absolute Unix-seconds `valid_time` |
| Region evidence | Java `RegionConfig`: JP default `ap1-sapp-api.botslab.com`; runtime discovery takes precedence |
| SDK signing constants | Java `QhsFatSdkManager.initVacuum360Config`, `SdkConfigs` construction: same SDK app key/secret as the existing K10 signer |
| Signature construction | Dart `CommonParamInterceptor`: AppUtilChannel metadata; `0x27cef0` onward concatenates app key, m2, timestamp, nonce and SDK secret before MD5 |
| SDK authorization header | Dart `UserSessionStore.get_authorizationString` (`0x1ff06c`), literal `tokens ` at `0x1ff1c4` |
| SDK property read | Dart `TslRemoteDataSource` at `0x2b9bf0`: `/v1/iot/device/get_property`, GET at `0x2b9bfc` |
| SDK service call | Dart `TslHttp` at `0x1e1540`: `/v1/iot/device/invoke_service`, form-urlencoded at `0x1e1508` |
| S1 status values | Java `VacuumUtil.getDesText`, `isCleaning`, `isDocking`, `isGoCharging`, `isWarningOrError` |

The wire count is integer **1/2**, not enum indices 0/1. Setting
`WorkRepeatTimes` and sending `CleanTimes` in a start command are distinct
operations. The native Java shortcut `DeviceRepo.startVacuumClean` hardcodes
`CleanTimes:1`; the new S1 path deliberately avoids that shortcut.

JADX reported errors elsewhere in the full APK. Dart AOT tools used an
unverified Dart 2.13.4 profile. The critical literals were checked against
instruction loads, object-pool references and independently recovered string
IDs; generated Dart pseudocode is not treated as original source.

## Transport correction in v0.5.1

User hardware evidence: v0.5.0 read docked status and battery successfully, but
the cleaning-pass selection stayed unknown and its write returned `statusCode
190`. That code by itself does not establish which server validation failed.
Native `getInfo`/`setInfo`/`setAction` declarations did not establish that those
proxies support the S1 SDK's property payload. They are no longer used here.

Keep the existing SwitchBot App login and account-discovered regional endpoint.
Only basic status uses its native proxy. Follow the APK's regional SDK setup for
all S1 property reads, writes and actions:

```text
POST /wonder/devicestatus/v1/getstatus
{"items":["DEVICE_MAC"]}

GET /wonder/sweeper360/v1/user/getRegion
→ body.hosts[0] selects the SDK host

POST /wonder/sweeper360/v1/auth/applyToken
{"groupID":"DEVICE_FAMILY"}
→ body.token and body.valid_time

Regional SDK, Authorization: tokens DELEGATED_TOKEN
GET /v1/iot/device/get_property
query: product_key=PRODUCT_KEY, device_name=DEVICE_MAC, signed common parameters
→ code=0, data.result is a property object or a JSON-encoded property object

POST /v1/iot/device/set_property
form: product_key=PRODUCT_KEY, device_name=DEVICE_MAC,
      items={"WorkRepeatTimes":2} (items is a JSON string)

POST /v1/iot/device/invoke_service
form: product_key=PRODUCT_KEY, device_name=DEVICE_MAC,
      identifier=StartDefaultClean, input={"CleanTimes":2} (JSON string)
```

SDK common query parameters include `appkey`, a coordinator-lifetime installation `m2`,
`sign_ts`, fresh `sign_no`, and `sign = MD5(appkey + m2 + sign_ts + sign_no +
SDK_SECRET)`. The secret is the SDK's embedded shared signing constant, not a
user credential. Region `code`, when present, supplies `ci_cy`. No hardcoded EU
K10 endpoint, room payload, OSS upload or S10 numeric property is used for S1.
Both SDK forms also carry signed common query parameters.

SwitchBot credentials go only to HTTPS SwitchBot API hosts. Delegated tokens
go only to the discovered HTTPS Botslab host (or the observed CN vendor host).
Redirects and arbitrary hosts are rejected. The delegated session is cached in
memory and refreshed before expiry; changing App session or family invalidates
its context. A failing action is never automatically replayed.

The SDK routes were recovered statically; the v0.5.3 SSH investigation below
also verified a successful real S1 property read. Missing `WorkRepeatTimes`
after a successful read selects the explicitly labelled local next-start path.
Failed reads and invalid reported counts do not silently become local defaults.
For firmware reporting the setting, even `code=0` without matching readback
remains a setting error. No other cloud, proxy or numeric protocol is tried.

## State and failure behavior

- 0 standby, 1/2/3 cleaning, 4 paused, 5 returning, 6 charging, 7 charged,
  8 dormant, 9 error, 10 remote control, 11 collecting dust, 31 quick mapping.
- Times may change only in states 0/6/7/8, with the device online. This also
  rejects paused tasks, consistent with the App's finish-current-task message.
- Remote setting writes require a valid reported `WorkRepeatTimes` first.
  With the successfully observed absent-property capability, selection instead
  persists a local next-start preference; it sends no setting request to the robot.
- Only one write is sent. Up to three readbacks follow (two-second gaps).
  A connection failure never causes an automatic repeat write/start.
- Setting and start operations are serialized per device. Startup polling uses
  the same lock so a poll does not race an in-flight change.
- Real-time status takes precedence over SDK properties for shared telemetry. Missing
  optional properties do not prevent basic status polling.
- `cleaning_passes_source: device` means reported telemetry. `integration`
  means config-entry option `s1_cleaning_passes`, default 1, used as the next
  `StartDefaultClean.CleanTimes` input. Those semantics are exposed on both
  entities. The local setting is not synchronized with SwitchBot App.
- S1 has no S10 error-description/problem sensors or summary metrics; the raw
  error code is exposed on the vacuum for future verification.
- New S1 failures name `getRegion`, `applyToken`, or the failed SDK route with
  bounded codes/types. Since v0.5.2, an SDK error can include a message string
  (at most 160 characters), with known sessions, identifiers, addresses, URLs and
  opaque strings redacted. Arbitrary response bodies/objects are never echoed.
  Redirects are rejected for the private requests.

## v0.5.2: `207009` investigation and evidence limits

The user reported v0.5.1 `set_property` code **207009** and confirmed that its
current count had always been **unknown**, including before attempting a write.
This establishes neither the GET response code nor the meaning of 207009. No
matching vendor error definition was found in the retained APK evidence.

Two client/APK discrepancies were identified and corrected:

1. The App calls `DeviceUtil.getProductKey(deviceType)` →
   `Sweeper360Util.getProductKey`, selecting S1/S1 Plus SDK keys by type.
   v0.5.1 instead preferred the native discovery/config entry's `product_key`
   when nonempty. v0.5.2 always uses the observed model mapping for S1.
   The actual value in the user's installed entry has not been inspected, so
   this is not proof that their key was wrong.
2. `TslHttp.setProperty` assembles identity and `items` in the form; common
   signing parameters are added separately. v0.5.1 duplicated identity into
   POST query parameters. v0.5.2 puts identity only in the form for POST and
   keeps it in the query for GET. Server tolerance of duplication is unknown.

Additional non-account evidence: two read-only requests to the observed JP SDK
`get_property` route, with dummy device identity and either no Authorization or
an intentionally invalid token, both returned HTTP 200, code **100003**, message
`Login verification failed` on 2026-11-03 UTC. These requests neither used the
user's credentials nor accessed the user's device. They establish only that
this login-failure case differs from 207009, not that 207009 guarantees valid
authorization or has a particular device/property meaning. No write probe ran.

The select now exposes the bounded read error as `sdk_property_error`, including
the distinction between SDK rejection, transport failure and a successful
response without valid `WorkRepeatTimes`. SDK error messages are redacted before
truncation and display. A login-verification code 100003 invalidates the in-memory
delegate for a subsequent call, without automatically replaying an action.

**v0.5.2 is a candidate correction plus diagnostic improvement, not a verified
resolution of 207009.** After restart, first check the count and this attribute.
If it remains unknown, collect that bounded error before trying more settings.

## v0.5.3: real S1 SSH investigation

The installed v0.5.2 entry used the correct S1 SDK key and retained a family ID.
No key migration was needed; the prior wrong-key hypothesis does not explain
this installation. A read-only authenticated probe executed on the HA host,
with the existing integration credentials kept there, found:

- Native state: online, WorkingStatus 7, battery 100%.
- Region discovery selected `ap1-sapp-api.botslab.com`.
- Delegation succeeded; `get_property` returned `code=0`.
- `data.result` was a JSON string containing an object with real
  `WorkingStatus`, `SuctionPowLevel` and `BatteryLevel`, but **no WorkRepeatTimes**.
- HA's select error was therefore the client's missing-property guard, not a
  failed GET or broken JSON decoder. The numeric meaning of 207009 is still
  undocumented; this does not invent a vendor error-code definition.

The earlier interpretation of the generic `SweeperPropertyModel` property path
as universally available on S1 was too broad. The same APK also contains:

- `SweeperBottomControlModel._getCleanTimes`, async body 0x543ba8: accesses
  SharedPreferences using `sweeper_cleanTimes_` at 0x543cc8 plus the device ID.
- `_saveCleanTimesToLocal`, async body 0x54407c: builds the same per-device key
  at 0x54419c and writes the count preference.
- `setTotalCleanTimes` 0x3294fc: updates the in-memory count; its special device
  branch calls `setTotalCleanTimesS10` separately rather than always writing
  `WorkRepeatTimes`.
- `SweeperBottomControlModel.startDefaultClean`, async body 0x549e80: reads
  `getTotalCleanTimes` at 0x549f98 and calls `SweeperUseCase.startDefaultClean`
  at 0x54a0b8. The service layer constructs `StartDefaultClean.CleanTimes`.

v0.5.3 implements a per-device HA equivalent of this local next-task input when
successful SDK telemetry omits `WorkRepeatTimes`. It persists the preference in
config-entry options through HA's API, without editing live `.storage` files.
A source of `integration` is not device telemetry or App synchronization. The
reported-setting path remains for firmware that really supplies the property.
The count is never chosen from the last failed write. Explicit local starts
validate 1/2 and authorize the SDK before one service call. Offline/active-task
selection guards and per-device serialization remain in effect.

No cleaning start was used in the SSH investigation. Reading properties and
changing an HA preference do not establish actual two-pass traversal. Live
HA deployment/selection checks and the final commit are recorded separately
from fake HTTP unit tests; no account tokens or raw private response bodies
belong in this repository.

Live validation on HA 2026.10.4 after deploying v0.5.3: the core configuration
check passed, core restart completed, and the count showed 1 with source
`integration` and no property error. Selecting 2 through HA's select service
succeeded and persisted integer 2 in config-entry options. Reloading the config
entry retained 2. The test restored 1 and confirmed its persistence. The vacuum
remained `docked` at all four checks. No `vacuum.start`, SDK start action, or
physical two-pass traversal test was performed. The 310-test local suite covers
the explicit `CleanTimes: 2` start payload using fake HTTP/SDK calls.

## Validation

Tests use actual Home Assistant 2026.10.4 Python APIs with fake HTTP responses.
No SwitchBot account or device is used. The suite covers discovery/config-flow
family retention, model-specific routes, regional authorization, expiry/cache,
credential separation, form payloads, count selection/readback, missing counts,
validation, active-task rejection, errors, privacy, and existing K10/S10 behavior.
These mocks validate client behavior, not actual cloud acceptance. Run with Python 3.14:

```bash
python -m pytest -q
```

On-device validation checklist:

1. Install v0.5.3 and restart HA. Keep the existing entry; confirm model, battery,
   state, count and `cleaning_passes_source`.
2. While docked, select 2. For `integration`, confirm HA option persistence, not
   App synchronization; for `device`, require matching device readback.
3. Reload and check that the correct source/count persists. Restore the prior
   HA preference after a selection-only test.
4. Set 2, explicitly start the custom integration's vacuum; check actual two-pass
   traversal. Merely seeing the property equal 2 is not completion verification.
5. Test pause/stop, return to base, suction, offline behavior, and expired login.
6. Restore the original count. Do not assume the separate built-in Cloud vacuum's
   start action honors this custom integration's selection.

Rollback: restore the original count while the integration is installed, then
remove its config entry and restore the previous custom-component directory.
The built-in `switchbot_cloud` integration is not modified by this fork.
