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
| Native proxy declarations | Java `DeviceService.getVacuumSettingInfo`, `setVacuumInfo`, `setVacuumAction` |
| Native proxy payload | `VacuumGetInfoReq`, `VacuumInfoReq` (includes selected family `groupID`), `VacuumActionReq` |
| S1 status values | Java `VacuumUtil.getDesText`, `isCleaning`, `isDocking`, `isGoCharging`, `isWarningOrError` |

The wire count is integer **1/2**, not enum indices 0/1. Setting
`WorkRepeatTimes` and sending `CleanTimes` in a start command are distinct
operations. The native Java shortcut `DeviceRepo.startVacuumClean` hardcodes
`CleanTimes:1`; the new S1 path deliberately avoids that shortcut.

JADX reported errors elsewhere in the full APK. Dart AOT tools used an
unverified Dart 2.13.4 profile. The critical literals were checked against
instruction loads, object-pool references and independently recovered string
IDs; generated Dart pseudocode is not treated as original source.

## Implemented transport

The SDK's direct Botslab endpoint needs delegated credentials and SDK signing.
The integration instead reuses its existing SwitchBot App login and regional
endpoint discovery, with these native proxy payloads:

```text
POST /wonder/devicestatus/v1/getstatus
{"items":["DEVICE_MAC"]}

POST /wonder/sweeper360/v1/device/getInfo
{"productKey":"PRODUCT_KEY","deviceName":"DEVICE_MAC",
 "items":["BatteryLevel","WorkingStatus","SuctionPowLevel","WorkRepeatTimes","ErrorCode"]}

POST /wonder/sweeper360/v1/device/setInfo
{"productKey":"PRODUCT_KEY","deviceName":"DEVICE_MAC","groupID":"DEVICE_FAMILY",
 "items":{"WorkRepeatTimes":2}}

POST /wonder/sweeper360/v1/device/setAction
{"productKey":"PRODUCT_KEY","deviceName":"DEVICE_MAC",
 "identifier":"StartDefaultClean","input":{"CleanTimes":2}}
```

**The SDK-to-proxy property combination is not yet hardware-verified.** A generic
`items` DTO does not prove that the server forwards every property on every
region/firmware. Unsupported/missing property reads remain unknown, and a
setInfo statusCode 100 without matching readback is an error. There is no
silent fallback to another cloud, model, numeric protocol, or pass count.

## State and failure behavior

- 0 standby, 1/2/3 cleaning, 4 paused, 5 returning, 6 charging, 7 charged,
  8 dormant, 9 error, 10 remote control, 11 collecting dust, 31 quick mapping.
- Times may change only in states 0/6/7/8, with the device online. This also
  rejects paused tasks, consistent with the App's finish-current-task message.
- Only one write is sent. Up to three readbacks follow (two-second gaps).
  A connection failure never causes an automatic repeat write/start.
- Setting and start operations are serialized per device. Startup polling uses
  the same lock so a poll does not race an in-flight change.
- Real-time status takes precedence over getInfo for shared telemetry. Missing
  optional properties do not prevent basic status polling.
- Current pass count is cloud-reported, not restored from a local desired value.
- S1 has no S10 error-description/problem sensors or summary metrics; the raw
  error code is exposed on the vacuum for future verification.
- New S1 failures expose bounded codes/types, not cloud bodies, signed URLs or
  tokens. Redirects are rejected for the private requests.

## Validation

Tests use actual Home Assistant 2026.10.4 Python APIs with fake HTTP responses.
No SwitchBot account or device is used. The suite covers discovery/config-flow
family retention, model-specific routes, payloads, count selection/readback,
missing counts, validation, active-task rejection, errors, privacy, and existing
K10/S10 behavior. Run with Python 3.14:

```bash
python -m pytest -q
```

Before calling this hardware-verified:

1. Install the fork, sign in, select the real S1; confirm model, battery and state.
2. Note the original count. While docked, set 2; confirm cloud readback and App UI.
3. Set 1 and check again. Reload/restart and check the cloud-reported value.
4. Set 2, explicitly start the custom integration's vacuum; check actual two-pass
   traversal. Merely seeing the property equal 2 is not completion verification.
5. Test pause/stop, return to base, suction, offline behavior, and expired login.
6. Restore the original count. Do not assume the separate built-in Cloud vacuum's
   start action honors this custom integration's selection.

Rollback: restore the original count while the integration is installed, then
remove its config entry and restore the previous custom-component directory.
The built-in `switchbot_cloud` integration is not modified by this fork.
