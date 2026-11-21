# oneui-smooth

**Make a laggy Samsung Galaxy smooth again: no root, nothing deleted, every change reversible.**

Samsung's One UI can make a perfectly capable phone stutter in everyday use: unlocking to make a call, pulling down the notification shade, switching apps. Usually the chip isn't the problem. The software running on it is. This tool **measures** where your phone loses frames, **shows** what is causing it, and **asks you** before turning anything off.

```
python oneui_smooth.py
```

---

## What it does

| Step | What happens | Changes anything? |
|---|---|---|
| **Measure** | Records the real frame times of the home screen and the notification shade / lock screen, plus memory and background load. | No |
| **Scan** | Lists what's loading System UI (accessibility services, apps reading your notifications, Good Lock plugins) and which preinstalled extras are installed and running, with their RAM use. | No |
| **Fix** | Goes through those findings one at a time and asks before every change. It asks which features you use (Bixby, SmartThings, Samsung Pass…) and only disables what you don't. | Yes, after you confirm |
| **Sleep advice** | Sorts *your* apps into Never sleeping / Sleeping / Deep sleeping, based on what they are and when you last used them. | No |
| **Compare** | Shows before and after side by side. | No |
| **Undo** | Reverses every change the tool made. | Yes, after you confirm |

## Real results (mid-range Galaxy, One UI 9)

Measured with this tool's own frame test. "Janky" means frames that missed the 8.3 ms deadline of a 120 Hz screen, which you see as stutter.

| Notification shade / lock screen | Stock | After |
|---|---|---|
| Janky frames | 26.4% | **10.5%** |
| Typical frame | 17 ms (worse than 60 Hz) | **6 ms (real 120 Hz)** |
| Slow frames (p90) | 65 ms | **27 ms** |
| Worst frames (p99) | 250 ms | **61 ms** |

Both runs were taken with the phone running for about a day, not right after a restart. Battery life also improved noticeably for the owner. That's one person's experience, not a lab measurement, so measure your own phone.

**What made the difference, in order:**
1. **An unused accessibility service** (Select to Speak) plus **six unneeded apps with notification access.** Both force extra work in System UI on every screen update and every notification.
2. **Good Lock plugins that run inside System UI.** QuickStar, NotiStar and friends together cost a lot; LockStar alone measured at zero cost. Keep the ones you love.
3. **An animated lock screen clock:** switching to a still clock style cut shade stutter by about a third (10% → 6.5% janky).
4. **Background bloat** (Facebook preloads, telemetry, unused Samsung services): less memory pressure after hours of use.

What did *not* help much: "Reduce transparency and blur" (small gain), and killing cached apps (makes things worse, see FAQ).

## Setup

1. **Python 3.8+**: [python.org](https://www.python.org/downloads/). On Windows, tick "Add Python to PATH".
2. **adb** (Android platform-tools):
   - Windows: [download platform-tools](https://developer.android.com/tools/releases/platform-tools), unzip, and add the folder to PATH (or run the tool from inside that folder)
   - Linux: `sudo apt install adb` / `sudo pacman -S android-tools` / `sudo dnf install android-tools`
   - macOS: `brew install android-platform-tools`
3. **USB debugging on the phone:**
   - Settings → About phone → Software information → tap **Build number** 7 times
   - Settings → Developer options → **USB debugging** on
   - Plug the phone in, tap **Allow** on the prompt, and check that `adb devices` shows it as `device`
4. **Get the tool** and open a terminal in its folder:
   ```
   git clone https://github.com/Tarnished-Af/oneui-smooth.git
   cd oneui-smooth
   ```
   No git? Use Code → Download ZIP on the GitHub page, unzip it, and open a terminal inside the folder.

## Usage

```
git clone https://github.com/Tarnished-Af/oneui-smooth.git
cd oneui-smooth
```

The easiest way is the menu:
```
python oneui_smooth.py
```

Or run commands directly:
```
python oneui_smooth.py measure before      # 1. baseline (follow the on-screen frame test)
python oneui_smooth.py scan                # 2. see what's slowing it down (read-only)
python oneui_smooth.py fix                 # 3. step by step, asks before each change
adb reboot                                 # 4. restart so System UI reloads
                                           # 5. use the phone normally for a day
python oneui_smooth.py measure after       # 6. measure again
python oneui_smooth.py compare before after
python oneui_smooth.py sleep-advice        # which apps should sleep
python oneui_smooth.py undo                # put everything back
```

**Measuring tips:**
- Do the frame test the same way each time: swipe between home screens, open the app drawer, pull the shade down and up about 10 times, lock and unlock once or twice.
- Compare like with like. Right after a restart, every phone looks better than after a day of use. `compare` warns you when the uptimes differ a lot.
- Back-to-back runs vary by about 2–3 points of janky %. Smaller differences are noise.

`measure --app com.instagram.android` adds a frame test inside one app.

## Safety

- **No root, no bootloader unlock, no Knox trip.** Banking apps and Samsung Wallet keep working.
- **Nothing is uninstalled.** Packages are disabled with `pm disable-user`, which you can undo at any time.
- **Only packages on the curated list in [`packages.json`](packages.json) are ever offered,** and only if they're installed. Core components (System UI, Settings, phone, keyboard, Play services, Samsung Wallet, Find My Mobile…) are never on that list.
- **Every change is logged** (in `~/.oneui-smooth/<device>.json`), and `undo` reverses them all.
- **Back up anyway** (Smart Switch or Google backup) before changing anything on a phone you depend on.
- A factory reset also re-enables everything.

If you use **Samsung Wallet / Samsung Pay**, open it after the fix and check that your payment screen loads. The tool reminds you if it disabled Samsung Pass.

## Sleep advice: don't put everything in deep sleep

Deep sleep feels like it should help, but on its own it makes a phone *slower*. Every app has to start from scratch each time you open it, which is slow on UFS 2.x storage, and messages arrive late. `sleep-advice` suggests:

- **Never sleeping:** messaging, payments/UPI, banking/OTP, authenticators, password managers, VPN, smartwatch/earbud apps, ride-hailing, alarms
- **Sleeping:** apps you use every week but that don't need to be instant (social, shopping, food, streaming)
- **Deep sleeping:** apps not opened for 1–4 weeks
- **Deep sleep or uninstall:** not opened in over a month

Set these on the phone in **Settings → Battery → Background usage limits.**

## FAQ

**Why not just kill background apps like Instagram?**
Most of them are *cached*: frozen in memory, using no CPU. Android removes them on its own when memory is needed. Killing them only makes the next launch slower, and they restart themselves when a notification comes in. `scan` and `measure` show which apps are actually *running* in the background; those are the ones worth restricting.

**My friend's Motorola/Pixel has the same chip and is smoother. Why?**
Same hardware, heavier software. One UI's System UI does more work per frame (blur, layered animations, Now Bar, lock screen effects) and runs more background services. This tool removes the load you don't need. It can't change Samsung's CPU tuning; that needs root.

**Which phones does it work on?**
It's written for Samsung Galaxy phones on One UI 6–9, and tested on a mid-range Galaxy running One UI 9. The measuring part works on any Android phone. Package names can differ by model and region. The tool only offers what's actually installed. If something on your phone should be on the list (or shouldn't), please open an issue.

**Something broke after the fix.**
Run `python oneui_smooth.py undo`, or re-enable a single package with `adb shell pm enable <package>`.

**It also told me to change some settings by hand?**
Some of the biggest wins (still lock screen clock, auto restart, Connected devices toggles) are phone settings with no reliable adb equivalent, so the tool lists them for you to change on the phone.

## Contributing

The package list lives in [`packages.json`](packages.json). To add an entry, open a PR with:
- the package name and what it does
- which feature stops working when it's disabled
- the tier: `safe` (no visible feature), `ask` (tied to a feature question) or `test` (try it and check)
- the phone model and One UI version you verified it on

Frame-test results from other phones (`summary.txt` before and after) are very welcome too.

## Disclaimer

Not affiliated with or endorsed by Samsung. Samsung, Galaxy, One UI, Bixby and Good Lock are trademarks of Samsung Electronics. Use at your own risk. Everything this tool does is reversible, but you're responsible for your device.

## License

MIT, see [LICENSE](LICENSE).
