# iPhone shortcuts for the stop log (offline-safe)

The car shortcuts only **save a small file on the phone**, so no cell service is needed. A fourth shortcut, **Upload Stops**,
sends the files to the mini and deletes each one only when the mini says OK. A file that fails to upload stays and is
tried again the next time Upload Stops runs.

Names and menus differ a little between iOS versions; where I could not check the exact wording on your phone I say so.
Use **straight** quotes only where a quote is needed (this plan needs none).

## 0. Once

1. Files app > On My iPhone (or iCloud Drive) > make a folder called **Stops**.
2. Your `carphone` stops token must already be in the URL of the old shortcut (`stoptoken=...`). The new URL is the same
   one **without** `event=`, `lat=` or `lon=`:
   `http://100.74.199.18:8780/display/stop?stoptoken=<carphone token>`
3. Tailscale on the phone: turn on **Connect On Demand** (Tailscale app > Settings), so a shortcut that fires in the
   background finds the VPN up.

## 1. Shortcut `Car Parked`

Actions, in order:

1. **Get Current Location**
2. **Get Latitude** of *Current Location* (blue variable, as in your working shortcut)
3. **Get Longitude** of *Current Location*
4. **Current Date**
5. **Format Date**: Date Format *Custom*, format `yyyy-MM-dd'T'HH:mm:ssxxx`. If the quotes around the T give you trouble, use
   Date Format *ISO 8601* instead; the mini accepts either. Name the result `Stamp`.
6. **Format Date** (second one) on Current Date: Custom `yyyyMMdd-HHmmss`. Name it `Name`.
7. **Text**: `park|Stamp|Latitude|Longitude` (insert the three variables, with the `|` characters between them, no spaces)
8. **Set Name** of that Text to `Name-park.txt` (time first, so files sort oldest to newest; in some iOS versions you can name the file in Save File instead)
9. **Save File** to the **Stops** folder. **Ask Where to Save: off.** Overwrite: off.
10. **Run Shortcut** `Upload Stops` (last, so a failure there can never lose the event that is already saved)

Hiking offline: steps 1-9 need no network (GPS works without cell service). Step 10 fails quietly; the file waits.

## 2. Shortcut `Car Driving`

Same as above, without the location steps:

1. **Current Date** > **Format Date** (`Stamp`, as in 1.5) and **Format Date** (`Name`, as in 1.6)
2. **Text**: `drive|Stamp`
3. **Set Name** to `Name-drive.txt` (time first again) > **Save File** to **Stops** (Ask Where to Save off)
4. **Run Shortcut** `Upload Stops`

## 3. Shortcut `Upload Stops`

1. **Get Contents of Folder**: Stops
2. **Sort** the result by **File Name**, A to Z. Because every name starts with its time, that is oldest first. Order
   matters: a drive that reaches the mini before its park is lost, because the park is then older than the newest event
   the mini has applied. Hence the time-first names, the sort, and the "stop at the first failure" below.
3. **Repeat with Each** item:
   1. **Get Contents of URL**: the URL from step 0, Method **POST**, Request Body **File**, File: *Repeat Item*
   2. **If** *Contents of URL* **contains** `"ok":true` (type the characters `ok":true`; no spaces)
      - **Delete Files**: *Repeat Item* (Delete Immediately: off)
      - **Otherwise**: **Stop This Shortcut** (the failed file and everything after it wait for the next run, in order)
   3. **End If**
4. **End Repeat**

If there is no connection, Get Contents of URL fails, the shortcut stops, and nothing is deleted. That is the retry.

## 4. Automations (Shortcuts app > Automation)

| When | Run | Notes |
|---|---|---|
| Bluetooth **Chevrolet Volt** is **Disconnected** | `Car Parked` | Run Immediately, Notify When Run: off. Replace the old automation, which still holds the old shortcut. |
| Bluetooth **Chevrolet Volt** is **Connected** | `Car Driving` | Run Immediately, notify off |
| Wi-Fi **Low Star** is **Joined** | `Upload Stops` | add **Wait 10 seconds** first, so Tailscale is up |
| **Time of Day**, once a day (pick a time you are usually home) | `Upload Stops` | a safety net |

The uploader also runs after every car event, so whichever has signal **and** a reachable mini first empties the queue.
The mini must be on: while it is off, files simply wait (their times are kept, so nothing is lost).

## 5. Test it (about 5 minutes)

1. Airplane mode on. Run `Car Parked` by hand from the Shortcuts app. Look in Files > Stops: one `park-....txt` file whose
   text is `park|2026-...|45...|-123...`. Open it and check the time has an offset (`-07:00`) and the numbers are blue-variable
   numbers, not the word "Latitude".
2. Run `Car Driving`. A second file appears.
3. Airplane mode off, Tailscale connected. Run `Upload Stops`. Both files should disappear.
4. On the phone menu, Tools: **Stop log: last upload just now (2 new)**. Send the same files twice (copy them out of the
   folder first): the second time says duplicate and changes nothing.
5. On the mini: `grep stops tvserver.log | tail` (shows any dropped events and why).
6. Real test: on the mini set `TV_STOP_MINUTES=2` in `tv.env`, run `bash install-tvserver.sh | grep -v '^Token:'`, park 3+ minutes
   away from the RV with airplane mode on, drive off, turn airplane mode off and run `Upload Stops`. A white pin appears within
   about 15 seconds. Put `TV_STOP_MINUTES=90` back afterwards.

## Things I could not verify from here

- Whether Shortcuts can read your location from a **background automation** with only "While Using the App" allowed
  (the old plan had this problem too). If `Car Parked` saves a file with an empty or wrong location, that is the cause.
- That **Get Contents of URL** with *Request Body: File* sends the raw file on your iOS version. If the mini answers 400, open the
  Tools page: the last problem is shown there. The fallback is the old form, with the line in the URL:
  `.../display/stop?event=park&lat=<Latitude>&lon=<Longitude>&at=<Stamp>&stoptoken=...` for each file.
- That **Tailscale is up** when an automation fires on cellular. If uploads only ever work on Wi-Fi, that is it.

## What the mini does with a file

It reads the time in the file (not the arrival time), applies events in time order, ignores one it has seen before, skips
one that is older than the newest it has applied, and drops data it cannot use (with a line in `tvserver.log`). A parked
stay of 90 minutes or more becomes a white pin; parking beside the RV (judged by where the mini was at that time) is ignored.
