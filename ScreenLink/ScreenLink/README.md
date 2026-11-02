# ScreenLink – PC → tablet mirroring with game mode + multi-touch

## Get the two files (easiest: GitHub builds them for you)
1. Create a GitHub repo, upload this whole folder (keep the `.github` folder).
2. Open the **Actions** tab → "Build ScreenLink" → when done, download
   **ScreenLink-exe** (ScreenLink.exe) and **ScreenLink-apk** (app-debug.apk).

## Or build locally
* PC: put `ffmpeg.exe` in `pc/`, run `pc/build_exe.bat` → `pc/dist/ScreenLink.exe`
* Tablet: open `android/` in Android Studio → Build > Build APK(s)

## Use
1. PC: run ScreenLink.exe, allow it through Windows Firewall (Private network), click **Start server**.
2. Tablet (same Wi-Fi, 5 GHz or USB tethering): install the APK, tap **Find PC automatically** (or type the IP), tick **Game mode**, **Connect**.
3. Touch the tablet: every finger (up to 10) is injected on the PC as real Windows touch.
   Back button on the tablet disconnects.

## Modes
* Mirror: 1080p @ 30 fps.   * Game: 720p @ 60 fps, 1.5x bitrate, low-latency decoder flags.
