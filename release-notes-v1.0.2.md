## Automatic Yeelight power checks and recovery

- Enabled bulbs are checked silently every 30 minutes while the PC is awake,
  with an additional check shortly after startup, wake restoration or a day/night
  change.
- If a responding bulb reports OFF when nighttime lighting should be active,
  the app runs Force System Sync automatically. This also works with the
  Yeelight Chroma Connector integration disabled.
- Overview and Devices show each bulb's last reported power state and flag
  devices that are not responding. Check their power, LAN Control or network;
  an unanswered query never triggers a sync by itself.
- Recovery is limited to once per 30 minutes to prevent repeated restarts.
  Checks pause during sleep and restoration, and stale replies cannot trigger
  recovery after sleep, sunrise or a configuration change.
- Fixed a Windows packaging conflict that could prevent Qt from loading.
- Hardened test isolation so verification cannot stop the real OpenRGB task.

### Downloads

- **Installer:** `YeelightPCCompanion-1.0.2-setup.exe`
- **Portable:** `Yeelight-PC-Companion-1.0.2-portable.zip`
- **Checksums:** `SHA256SUMS.txt`

Exit the running tray app before upgrading. Existing settings are preserved.
The Windows builds remain unsigned.

### Verification

847 automated tests pass. Both real Yeelight devices answered read-only power
queries, and the packaged application passed an isolated startup check with
automation disabled.
