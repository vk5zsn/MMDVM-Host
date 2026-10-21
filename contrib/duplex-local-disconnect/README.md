# Duplex hotspot TG4000 disconnect workaround

This optional Python companion restores the API workaround used during the
VK5WA/VK2TLQ duplex hotspot investigation. An authorized **TG4000 group voice
call on the same slot as an active network group call** requests these actions:

1. BrandMeister `dropDynamicGroups`: clear dynamic subscriptions on that slot.
2. BrandMeister `dropCallRoute`: stop the current call route on that slot.

The trigger runs locally; the disconnect is performed by BrandMeister's HTTPS
API. This is not an offline RF mute and it needs an existing, valid BM API key
with access to the device. API availability and master behavior still matter.
HTTP 200 is logged as acceptance, not proof that the downlink stopped.

Both TS1 and TS2 are supported independently, including RF late entry. Other
callers, private calls, idle slots, and other TGs are ignored. Normal idle-slot
TG4000 continues through MMDVMHost. Existing MMDVMHost access controls still
apply before an accepted RF header is logged. The explicit caller list here is
an additional restriction; DMR caller IDs are not cryptographic authentication.

Static TG definitions are preserved: subsequent static traffic can resume.
All dynamic subscriptions on the selected slot are cleared, not only the TG
currently speaking. No local voice announcement is generated. Use the journal
and cessation of the downlink to confirm the result.

## Requirements and limits

- Python 3 and systemd on WPSD/Pi-Star, duplex operation, message-level MMDVM
  file logging (`FileLevel=1` or `2`), UTC log timestamps, correct system clock.
- Local DMRGateway with **only one enabled DMR network**, the configured BM
  master using a `*.brandmeister.network` hostname. Multiple networks and slot
  rewrites are unsupported; RF slot numbers must match BM slot numbers.
- The configured callsign/ID must exactly match what MMDVMHost writes in its
  log. The example config deliberately contains placeholders.
- Network calls older than ten minutes are ignored to bound stale log state.
  Logs must contain a network header or late-entry event before the RF command.
- Duplicate requests are suppressed while pending and for three seconds.
  Each action has a five-second timeout, with no delayed retries. Failed API
  actions are logged without exposing the key or response body.
- Detection follows the MMDVM log, so latency includes logging and API delay.
  Existing modem buffering may take a short time to drain.

This branch includes the opt-in forwarding change from PR #897, but this
companion works with the original WPSD binary. **Building or installing the
MMDVMHost binary and setting `DuplexControlTG` are not required.**

## Install

From this directory, on a writable WPSD filesystem (`sudo rpi-rw` if needed):

```sh
sudo install -m 755 disconnect.py /usr/local/libexec/tg4000-local-disconnect.py
sudo install -m 600 disconnect.ini.example /etc/tg4000-local-disconnect.ini
sudo nano /etc/tg4000-local-disconnect.ini
sudo python3 -B /usr/local/libexec/tg4000-local-disconnect.py --check
sudo install -m 644 tg4000-local-disconnect.service /etc/systemd/system/
# Stop the earlier experimental bridge if installed; never run both.
sudo systemctl stop tg4000-midcall.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg4000-local-disconnect.service
sudo journalctl -u tg4000-local-disconnect.service -f
```

Set your full BM device ID including ESSID, allowed callers, and local paths.
The key stays in WPSD's `/etc/bmapi.key`; no credential belongs in this repo.
`--check` reads local settings only and makes no API request. The service
revalidates the device and duplex/network configuration before each disconnect.
It starts automatically after reboot. WPSD updates may require reinstallation.

To stop and disable it:

```sh
sudo systemctl disable --now tg4000-local-disconnect.service
```

No MMDVMHost or DMRGateway restart or configuration change is required.

## Offline checks and live test

```sh
python3 -B -m unittest discover -s . -p 'test_*.py' -v
```

The tests mock all HTTP requests and never key a modem or drop a real route.
For live validation, join a dynamic TG on a duplex slot, wait for a network
transmission, then make a short TG4000 **group call on that same slot** from an
allowed radio. Confirm both API actions in the journal, that the dynamic TG
disappears in BM, and that the active downlink stops. Repeat on the other slot.
Keep the radio on the selected TG long enough to establish that it stays quiet.

API semantics: [BrandMeister sysop actions](https://help.brandmeister.us/repeaters/sysop-dashboard/actions).
The v2 action URLs follow the [Pi-Star BM API implementation](https://github.com/AndyTaylorTweet/Pi-Star_Binaries_sbin/blob/master/pistar-bmapi).
