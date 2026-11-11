# Roadmap

What Continuo does today, what is being finished, and what is planned. Generated from the project plan (the release musts only); every line is a user-visible capability or rule.

## Now

Release requirements met and proven on a real install. Each line states the requirement, or the problem it closed.

### Installs and runs on a fresh machine

- A backend package missing from the baseline list ships dead in the installed plugin; packaging must be complete.
- A new user needs a first success before downloading 40+ GB of models.
- API keys must never appear in logs, recipes or jobs; a leak would fail catalog rules.
- Catalog README and tool texts tell the truth: ComfyUI is asked at 127.0.0.1:8188, then 127.0.0.1:8000, or at the address in FILM_SUITE_COMFY_URL, exports land in the project output folder, BUILD-INFO counts the real tree.
- Ctrl+K 'Continuo' finds nothing and the sidebar may need a restart after install: a first-minutes break on a fresh marketplace install.
- Extra files under hermes/film-suite make state_root refuse, which breaks the plugin at load.
- Fresh install needs two restarts before audio analysis works; new users install on a fresh machine.
- License checks of mirrored repos and node packaging must pass before a public catalog release.
- Never touching the Hermes core and never blocking or crashing Hermes is a catalog requirement.
- No credentials or provider calls in the UI is a catalog/security rule.
- Project creation fails on some Windows homes.
- Public catalog data must never contain credentials, private paths or workflows.
- The Sample film opens on Cut (its playable cut) and shows the integration: shot records, a previz set and the song in Audio; its six pictures are named as AI-generated.
- The catalog PR needs a public repo anyway; a clean repo with a roadmap lets invited people review and help.
- The installer must not overwrite local files or silently restart Hermes and must allow rollback; that decides catalog acceptance.
- Waveform and transport must appear even when an analysis dependency is missing; otherwise the page is blank for a fresh user.
- With ~180 filmsuite tools Hermes needs a shipped skill routing intent to tools, or the agent path is unreliable for new users; none in the repo.
- Without ffmpeg/ffprobe, the Sample film says that ffmpeg is missing and how to fix it; no half-made sample is published, and a retry works after ffmpeg is there.

### Nothing gets lost, nothing crashes

- "Image state is unavailable" appears with a picture open, and Export (hand and Hermes) fails: a broken core step.
- A Hermes tool reporting success on failure makes the agent claim false results in the demo.
- A late Cut save never writes one project's cut into another project: edit sessions are fenced per project.
- A picture Hermes submits directly (continuo_image_workflow_submit / image_submit, not through the page) shows on the Image page while it runs and when done, like a hand job: in the running strip/canvas with 'Hermes' as origin, openable on finish.
- A planning change that touches the shot record and the previz set commits both or neither, also across a crash.
- A recent project whose folder is back (drive re-plugged, folder restored) opens on the next click; Start checks the folder again instead of remembering "Folder not found".
- A script reload (Hermes changes scenes) never drops unsaved or conflicting writing and never reports it saved.
- After a Hermes restart, Start shows the recent projects even while the gateway is still starting (it re-reads until the list arrives).
- An old project showed 'All media 0' after update; backend fix exists but the visible result is unproven.
- Data loss while the UI claims it is kept.
- Deliver renders exactly the extent it shows: Entire timeline includes the black/music tail it announces, and an explicit IN-OUT is honoured or refused, never silently shortened.
- Drafts/settings lost on reload, model/page switch or restart is data loss in every core flow.
- Film/FCPXML export must use the original song, not the 44.1 kHz analysis proxy outside the package (data integrity of the delivery).
- Hermes Develop (set colour, reset a group) and the stored Develop state land reliably under load too.
- Hermes actions must arrive and show live even when the window idles, or the demo's Hermes path breaks.
- Media shows the real model and settings of pictures made by catalog workflows (e.g. P-H3 Image LoRA edit): model name from target_id like videos, and the real controls (seed, megapixels, sampler, LoRA strength, references) instead of 'Krea 2 Turbo' with empty steps/CFG.
- Opening the split while the suite is the main page crashes the main page.
- P-H3 Image workflows offer a Steps control the artist can set (manifest slot + descriptor control), like Krea 2.
- Previz Undo restores exactly what the proposal changed: never erases newer figures or sets, and an undone lens stays undone.
- Previz camera edits use the shot record's revision check; a stale edit is refused and shown, never overwrites a newer lens.
- The render queue never creates a missing project folder (an unplugged drive or a moved folder stays absent); it writes only into an existing, opened project.
- Unsaved work must be visible and closing must ask, otherwise data loss.
- With an unsure meter the note 'Beats only · meter unsure' is fully readable (not clipped by the Markers row), and no bar-only controls ('Bar 1' flag, 'Jump n bars') claim bars that do not exist.
- fit_to_beats answers a reason for every skipped clip and anchors each fitted clip to the beat it really lands on (index and residual consistent with the placed song).

### Song to cut (Audio, Music, Cut)

- A character introduced with an article in action ("A FERRYMAN (70s)") is the same person as FERRYMAN, not a third character.
- A library clip dropped on Cut lands on V2 at frame 20 instead of the drop point; a new user hits this in the first minute.
- A still picture from the project's folders shows in Cut's viewer and plays in the cut (not black).
- Every cut (timeline) of the project shows as its own item in Media, like Resolve's Media Pool and Premiere's Project panel: open, duplicate, rename, drag into another cut (nested), by hand and by Hermes.
- Figure controls in Previz change what they show: the selected shot's pose/position, not the whole scene.
- Playback with a moving playhead is a basic expectation of the Audio page.
- Render settings with all formats producing a file is core to delivering a cut.
- Switching the shown cut shows that cut's subtitles at once (ST1 and viewer follow the cut, not the project).
- The sections band shows named song parts (Intro, Verse, Chorus, Bridge, Outro from the song map), not letter segments; inspector Markers and Hermes read the same.
- Track set and Clip/Audio/History inspector are core Cut structure.
- Use in Cut is still gated on analysis, so the no-analysis path is broken in a core flow.
- Waveform reads as a near-solid band; a new user sees an unreadable waveform at once.

### Image

- Right-click Select inverse is not clickable on the selection, a basic edit step a new user hits; the rest of the parity list can wait.

### Video

- A take must be seen large in an app-styled player; small/browser playback is visible in the first minute.
- Every visible H3 setting must actually change the local render, or the main video controls silently lie.
- Video upscale uses RelayVSR (one-step video super-resolution, Comfy-Org repackage, Apache-2.0; x2 LightX2V 5.2 GB + 0.13 GB VAE, x4 the same) as the default method, e.g. 480p/720p H3 takes to 1080p, by hand and by Hermes; SeedVR2 stays for stills.
- Wrong setup record sends users hunting a node that is in core.

### Media and Hermes paths

- Library clips show placeholders instead of poster frames: a visible break when a new user opens Media.

### Hermes can do what the hand does; jobs

- Agent path bypasses the approved-folder rule the hand path enforces.
- Dead or pointer-only buttons are visible breaks a new user clicks in the first minutes.
- Hand/Hermes parity on the same picture is a core flow and was never measured with a real Hermes model.
- Hermes can run audio actions but Undo/Redo of Hermes actions is open; parity on a core flow.
- Hermes marks/rejects reach Media only after a renderer reload: broken hand/agent parity in the review flow.
- The accept/undo pair covers script text during Hermes edits, a visible break in the parity demo.
- The chat shows tool calls as "Filmsuite Suite …"; every Continuo call needs a clean, current name. Visible in the demo.
- Without payload schemas per action Hermes guesses image/suite actions and fails visibly in the demo.

### Polished UI: one component per job, every page

- Media column header is one calm row per DESIGN.md (internal): search, one filter/view menu, type filter as one segmented control, the count tells what is shown (no "All media 39" beside an active filter), no framed button next to ghost icons.
- Side panels: both sides have the same visible resize handle with remembered width on every page; no dead gutter between the media grid and the canvas.

### Other

- H3 as an image model is marked low priority backlog.
- Sample film looks finished on first open: Media shows the real shot pictures (no "…" placeholders) and folder clips without ✕ badges; the Previz banner says the sample was set up, not "Hermes blocked scene"; Pillow is declared in plugin.yaml so storyboard pictures exist on a fresh machine.

## Next

Built; being proven on a real install or finished.

### Installs and runs on a fresh machine

- Cold dependency install unproven; green can hide failed preparation.

### Song to cut (Audio, Music, Cut)

- Words appear for songs mapped before the update, without re-analysing.

### Image

- Replace cuts the new object at the selection edge (sunken boat); a new user would see this broken result in the main edit demo.

### Video

- Generate must work with the project output folder on a fresh project; the 'No output folder' path still exists.

### Other

- H3 as an image/edit model (P-H3-Image, image LoRA): promising for consistency with our video model; after the release, research runs now in VIDEO-H3 section 7.

## Later

Planned for the release.

### Nothing gets lost, nothing crashes

- User feedback: Windows desktop ComfyUI defaults to port 8000; remote ComfyUI hosts are common.

### Song to cut (Audio, Music, Cut)

- Audio is a demo page and still reads as bolted-on parts; one approved coherent layout is needed.
- Switching the shown cut shows that cut's subtitles at once (ST1 and viewer follow the cut, not the project).

### Polished UI: one component per job, every page

- A design gate in run-gate (internal) measures the inventory parts on every page (size, radius, spacing, label placement, handle presence) against DESIGN.md (internal) and fails on a deviation.
- Controls follow one size and label rule: square icon buttons, labels outside fields, one primary action per panel, disabled controls say why, empty states and job states use the shared components.
- Words on screen follow the vocabulary and prose rules on every page (no developer words, one spelling, short labels, truthful counts and times).

---

165 release musts in the plan; 78 of them are internal working notes and are not listed here.
