# Changelog

## 2.3.0

### Picture

* **Exact colours in repaired outputs.** Interpolate, Cut keep the sound and Cut speed the sound
  up converted every frame to RGB and back, which made the whole output about 2 to 3 levels
  darker and slightly tinted, repaired frames or not. Frames now travel as 8 bit YUV 4:2:0 from
  the decoder to the encoder; only the computed frames go through RGB. The colour tags of the
  source (range, matrix, primaries, transfer) are written to the output. Outputs made with 2.0
  to 2.2 should be redone.
* **Portrait phone clips.** A clip with a rotation tag (iPhone in portrait) was read with the
  wrong width and height by the repair, which scrambled the picture. The size of the decoded
  (upright) frames is now used.
* A clip with an odd width or height is refused with a clear message instead of an encoder error.

### Preview and manual corrections

* One set of video controls (the duplicate play button and seek bar are gone), plus buttons to
  step one frame at a time and the current frame number. Seeking lands in the middle of a frame,
  so it shows the right frame even on irregular (60 Hz grid) timestamps.
* Hand edits of the held frames are no longer replaced by the detection when the clip is
  analysed again or the automatic analysis is switched off and on. A badge says "edited by hand"
  and "Back to detection" drops the edits.
* The preview is unloaded when the list is cleared or the clip removed.

### Security and privacy

* The preview server needs a random secret URL (new for every clip, revoked on remove, clear
  and quit), checks the `Host` header, answers range requests correctly (suffix ranges, clamped
  ends, 416, a malformed header can no longer make it read the whole file in memory), serves
  several requests at once and sends the right type for `.mov` and `.m4v`.
* The update check works: one request when the window opens (none from Python at start), a real
  version comparison (no "update" to an older release), translated texts, a link that opens the
  release page in the browser. Only the project's release pages can be opened.
  `FRAMESNAP_NO_UPDATE_CHECK=1` switches it off.
* `fetch_rife.py` refuses an archive entry that would be written outside its folder.
* Release workflow: read only token by default, write access for the publish step only, the
  Release is created with the GitHub CLI (no third party action) and only when every platform
  built and passed its selftest.

### Robustness

* Closing the window stops the analysis and the encoders, kills their ffmpeg processes and
  waits for them: no encoder keeps running in the background, no `.part` file is left behind.
* ffmpeg's error output is read continuously: a damaged file that prints thousands of decoding
  errors no longer freezes the analysis or the repair, and a decoder that fails is reported.
* A Cancel pressed right after Recode is no longer lost.
* Removing a clip, clearing the list or changing the rate stops the analysis in progress.
* An output is checked before it replaces the previous file (a failed check used to delete a
  good earlier output); copies are atomic too, so an interrupted copy can no longer leave a
  truncated file that later runs would skip.
* "Skip clips already recoded" only skips a file made with the same settings. The settings of
  each output are kept in `recoded/.framesnap-manifest.json`; files from earlier versions are
  kept as before.
* The held frame detection is vectorised (an hour at 60 fps: from about 10 s to well under a
  second) and the GUI is no longer updated while the controller lock is held.
* The analysis progress is right for converted clips (it stopped at 50 % for 50 to 25 fps).
* Temporary folders of the selftest and of the macOS build are removed.

### Tests and CI

* 66 new Python tests (colour, rotation, preview server, update check, processes, cancellation,
  manifest, detection, packaging) and 11 new interface tests. The colour, rotation and preview
  tests fail on 2.2.2.
* CI runs on macOS as well as Linux and Windows, caches RIFE, and uses the Node 24 versions of
  the GitHub actions. The GPU check of the tests runs only when a RIFE test is selected.
