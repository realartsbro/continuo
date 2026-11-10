# Continuo

Write the script, generate images and video takes, cut them to music and deliver the film: a film workstation that you and your AI agent use together.

Continuo is a plugin for [Hermes Agent](https://hermes-agent.nousresearch.com), the open-source AI agent by Nous Research.

![License: MIT](https://img.shields.io/badge/License-MIT-0969da)
![Tested on Windows 11](https://img.shields.io/badge/Tested%20on-Windows%2011-0969da)
![Hermes Agent >=0.22.7](https://img.shields.io/badge/Hermes%20Agent-%3E%3D0.22.7-0969da)

<!-- HERO IMAGE: one screenshot or short GIF of the Cut page with the Sample film goes here (not yet recorded). -->

- **Write** a screenplay in industry format, with Hermes writing in the same script.
- **Generate** images and video takes with local models through ComfyUI, then edit a selection or continue a take.
- **Cut** to music: analyse a song's beats and parts, place it under the cut and fit clips to the beats.
- **Deliver** an MP4, a Film master (MOV, Apple ProRes), subtitles, or the timeline as FCPXML, FCP 7 XML, OpenTimelineIO or EDL.

**Hermes and you work on the same project.** Hermes can do the same things you do by hand: write a scene, place a song,
trim a clip, start a render. It sees your changes immediately, and its own changes appear live on the open page as a
"Hermes did …" entry with before and after; <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes them.

**You stay in control of your files.** Hermes cannot give itself access to a folder; you approve each one.

[Install](#install) ·
[First success](#first-success-no-models-needed) ·
[Pages](#pages) ·
[Models](#models-optional) ·
[Talk to Hermes](#talk-to-hermes) ·
[Transparency](#what-this-plugin-does-on-your-machine)

## Quick start

### Requirements

| Part | Requirement | Notes |
|:---|:---|:---|
| Platform | Windows 11 (tested) | Linux is supported in the code but not yet tested. macOS is not supported yet: some safe-save steps use Linux-only system calls. Reports are welcome, see [Feedback](#feedback). |
| Hermes&nbsp;Agent | `>=0.22.7` | With the Hermes desktop app. |
| ffmpeg | Required | For Cut, Deliver and Audio; tested with 7.1.1. Looked up in `FFMPEG_PATH`, then `PATH`, then `C:\ffmpeg\bin`. |
| ComfyUI | Optional | Generates images, video, music and voice. Continuo talks to it at `http://127.0.0.1:8188`. |
| GPU | Optional | Script, Cut, Audio analysis and Deliver run on the CPU; a GPU is optional for them. Generating images, video or audio needs one. |
| Disk | 15&nbsp;MB&nbsp;+&nbsp;511&nbsp;MB | Measured: the plugin 15 MB; its Python packages 511 MB, set up by Hermes. Models need extra space, see [Models](#models-optional). |

### Install

```
hermes plugins install https://github.com/realartsbro/continuo
hermes plugins enable film-suite
```

No GitHub login is needed. The plugin's id in Hermes is `film-suite`; Continuo is its name in the app.

> [!NOTE]
> Hermes scans every plugin before it installs it. Continuo starts programs (ffmpeg) and talks to ComfyUI on your own
> machine (`127.0.0.1`), so the scan rates it **caution** and asks you to confirm. This is expected; answer `y` to
> continue. Details are under [Install scan](#install-scan).

1. Restart Hermes. On the first start Hermes sets up the plugin's Python packages (numpy, librosa, soundfile, httpx,
   Pillow), which takes about 2.5 minutes on a new Hermes installation.
2. Restart Hermes once more to load them. This is a one-time setup step.
3. Open **Continuo** from the Hermes desktop sidebar. Hermes also gets the skill `film-suite:continuo`, so it knows
   which tool does what.

### First success (no models needed)

After the install, the Sample film needs only ffmpeg. It holds three scenes, six drawn pictures, a music pad and a cut,
all CC0 (free to reuse).

1. On **Start**, choose **Sample film**. Continuo copies it into a new project folder,
   `Documents\Continuo\Sample film`, and opens it on **Cut**.
2. Play the cut. Move or trim a clip.
3. Open **Deliver**, choose the format **MP4** and click **Add to render queue**.
4. The file is saved to the project's output folder: `output/` inside the project folder, unless you chose another
   folder in the project settings.

Or ask Hermes to do it in one sentence:

```text
Open the sample film in Continuo, swap the first two shots and export an MP4.
```

## Pages

The bar at the bottom of Continuo holds the pages in working order.

| Page | What you can do |
|:---|:---|
| **Script** | Write in screenplay format with a scene list; Hermes writes in the same script, and you can undo its changes. |
| **Audio** | Play a song, set IN and OUT, analyse beats and song parts, and **Use in Cut**. |
| **Image** | **Create** images from a prompt and **Edit** a selection or mask; **Use these settings** restores any result. |
| **Video** | Generate MiniMax H3 takes locally, continue a take, compare A/B, upscale and send to **Cut**. |
| **Cut** | Edit one timeline with music, song parts and beats; **Fit to beats**, with music lowered under dialogue. |
| **Deliver** | Render MP4 or Film master, export FCPXML, FCP 7 XML, OTIO, EDL and subtitles, after **Check before render**. |
| **Media** | Browse the project's media on every page, with bins, filters, ratings and compare. |
| **Jobs** | See and stop every running job in one list, the same list Hermes works with. |

<details>
<summary>More on each page</summary>

- **Script**: Enter and Tab pick the next element, `int.` or `ext.` starts a scene heading, Fountain bold and italic
  show while you type. A strip of the whole script shows act markers. Hermes' changes are marked.
- **Audio**: waveform, playback, IN and OUT, markers. Analysis runs only when you ask: beats, bars and named song parts
  (Intro, Verse, Chorus). Lyrics are transcribed from the vocals or taken from text you paste. **Use in Cut** places the whole song or
  the IN-OUT range.
- **Image**: several images in one run with a local model. Edit with selection tools, numbered boxes and **Remove**. Every
  image keeps the prompt and settings that made it.
- **Video**: takes from keyframes or reference images, run on your machine. Continue a take from a chosen frame, keep
  the best one.
- **Cut**: a large viewer (Timeline and Source), tracks V1, V2, A1 to A3 and Music. Clips come from your folders
  through Source IN/OUT. Each cut of the project is its own item in **Media**.
- **Deliver**: MP4, Film master (MOV, Apple ProRes), FCPXML 1.9, FCP 7 XML, OpenTimelineIO, EDL (CMX 3600), SRT and
  VTT. **Check before render** refuses offline clips; the render queue can stop and re-render, and it survives a
  restart.
- **Jobs**: all jobs from every page that run in the background, with stop. Hermes can also repeat them.

</details>

## Models (optional)

No model ships with Continuo, and the Sample film needs none.

- **Without any model**: Script, Cut, Deliver, Media and Jobs, plus Audio playback, IN and OUT, markers, beat analysis
  with the built-in librosa engine, and **Use in Cut**.
- **With ComfyUI and models**: Image **Create** and **Edit**, Video takes and upscaling, stems, music and voice generation.

Each generation feature runs a ComfyUI workflow. Install only the models of the workflows you want to use:

| Page | Workflows | Size per workflow |
|:---|---:|---:|
| **Image › Create** | 9 | 5.6–31.2 GB |
| **Image › Edit** | 9 | 11.0–31.2 GB |
| **Video** | 8 | 4.5–45.6 GB |
| **Audio** (5 in progress) | 7 | 0.5–40.1 GB |

<details>
<summary>All workflows and model sizes</summary>

Sizes come from the plugin's own source list (`dashboard/workflows/model-sources.json`); files shared between
workflows count once per row. Rows marked "in progress" are wired but not yet proven in the app. Repos are on Hugging
Face, custom nodes on GitHub.

| Workflow | For | Size | Files | Hugging Face repos, custom nodes |
|:---|:---|:---|:---|:---|
| yue2-song-v1 | Audio: a song with lyrics (in progress) | 3.96 GB | 1 | Comfy-Org/YuE2 |
| omnivoice-design-v1 | Audio: a voice from a description (in progress) | 2.45 GB | 1 | Bgeorge/OmniVoice<br>Nodes: Saganaki22/ComfyUI-OmniVoice-TTS |
| drama3-clone-v1 | Audio: a voice from a reference recording (in progress) | 40.14 GB | 2 | PedroMarinhoDev/Drama-3-ComfyUI<br>Nodes: PedroMarinhoDev/ComfyUI-Drama3 |
| omnivoice-clone-v1 | Audio: a voice from a reference recording (in progress) | 2.45 GB | 1 | Bgeorge/OmniVoice<br>Nodes: Saganaki22/ComfyUI-OmniVoice-TTS |
| foundation1-loop-v1 | Audio: loops and keybeds (in progress) | 5.75 GB | 3 | RoyalCities/Foundation-1<br>google-t5/t5-base |
| sa3-small-music-v1 | Audio: music from a prompt (Stable Audio 3 Small) | 3.46 GB | 2 | Comfy-Org/stable-audio-3 |
| audio-stems-v1 | Audio: split a song into stems | 0.46 GB | 1 | Kijai/MelBandRoFormer_comfy |
| flux3-klein-sd-create-v1 | Image › Create | 20.77 GB | 4 | Comfy-Org/Flux-2-Dev<br>Comfy-Org/Krea-2<br>KoalaNation/FLUX-3-KLEIN-MODELS<br>black-forest-labs/flux-3-image-klein-comfy |
| anima-turbo-create-v1 | Image › Create (Anima Turbo) | 5.63 GB | 3 | Comfy-Org/Qwen-Image<br>circlestone-labs/Anima |
| flux2-klein-9b-t2i-v1 | Image › Create (FLUX.2 Klein 9B) | 18.82 GB | 3 | Comfy-Org/Flux-2-Dev<br>Comfy-Org/Flux-2-Klein-9B<br>black-forest-labs/FLUX.2-klein-9b-kv-fp8 |
| ideogram4-create-v1 | Image › Create (Ideogram 4) | 30.09 GB | 4 | Comfy-Org/Flux-2-Dev<br>Comfy-Org/Ideogram-4<br>Comfy-Org/Qwen3-VL |
| krea2-raw-t2i-create | Image › Create (Krea 2 Raw) | 18.99 GB | 3 | Comfy-Org/Krea-2<br>Comfy-Org/Qwen-Image<br>Comfy-Org/Qwen3-VL |
| krea2-turbo-t2i-create | Image › Create (Krea 2 Turbo) | 18.99 GB | 3 | Comfy-Org/Krea-2<br>Comfy-Org/Qwen-Image<br>Comfy-Org/Qwen3-VL |
| moss-image-sft-create-v1 | Image › Create (MOSS-Image) | 20.25 GB | 3 | Comfy-Org/Flux-2-Dev<br>Comfy-Org/MOSS-Image |
| p-h3-image-lora-t2i-v1 | Image › Create (P-H3-Image) | 31.19 GB | 4 | Comfy-Org/MiniMax-H3<br>PrunaAI/P-H3-Image-Comfy |
| qwen-image-2.1-create-pe-v3-v2 | Image › Create (Qwen-Image 2.1) | 27.09 GB | 5 | Comfy-Org/Qwen-Image-2.1<br>Comfy-Org/Qwen3-VL<br>PrunaAI/Pruna-Q-Image-2.1 |
| flux3-klein-sd-edit-v1 | Image › Edit | 20.77 GB | 4 | Comfy-Org/Flux-2-Dev<br>Comfy-Org/Krea-2<br>KoalaNation/FLUX-3-KLEIN-MODELS<br>black-forest-labs/flux-3-image-klein-comfy |
| flux2-klein-9b-kv-edit-v1 | Image › Edit (FLUX.2 Klein 9B) | 18.82 GB | 3 | Comfy-Org/Flux-2-Dev<br>Comfy-Org/Flux-2-Klein-9B<br>black-forest-labs/FLUX.2-klein-9b-kv-fp8 |
| krea2-edit-turbo-v1 | Image › Edit (Krea 2 Turbo) | 20.82 GB | 4 | Comfy-Org/Krea-2<br>Comfy-Org/Qwen-Image<br>Comfy-Org/Qwen3-VL<br>conradlocke/krea2-identity-edit |
| krea2-general-edit-v2 | Image › Edit (Krea 2 with the general edit LoRA) | 20.82 GB | 4 | Comfy-Org/Krea-2<br>Comfy-Org/Qwen-Image<br>Comfy-Org/Qwen3-VL<br>conradlocke/krea2-identity-edit |
| krea2-edit-kroma-v1 | Image › Edit (Krea 2, Kroma) | 21.46 GB | 4 | Comfy-Org/Qwen-Image<br>Comfy-Org/Qwen3-VL<br>conradlocke/krea2-identity-edit<br>silveroxides/Kroma-Quant |
| p-h3-image-lora-edit-v1 | Image › Edit (P-H3-Image) | 31.19 GB | 4 | Comfy-Org/MiniMax-H3<br>PrunaAI/P-H3-Image-Comfy |
| qwen-image-2.1-edit-pe-v3-v2 | Image › Edit (Qwen-Image 2.1) | 27.09 GB | 5 | Comfy-Org/Qwen-Image-2.1<br>Comfy-Org/Qwen3-VL<br>PrunaAI/Pruna-Q-Image-2.1 |
| qwen-image-2.1-edit-pe-v3 | Image › Edit (Qwen-Image 2.1, earlier pin) | 27.09 GB | 5 | Comfy-Org/Qwen-Image-2.1<br>Comfy-Org/Qwen3-VL<br>PrunaAI/Pruna-Q-Image-2.1 |
| krea2-identity-edit-v2 | Image › Edit that keeps a person's identity (Krea 2 with the identity edit LoRA) | 20.82 GB | 4 | Comfy-Org/Krea-2<br>Comfy-Org/Qwen-Image<br>Comfy-Org/Qwen3-VL<br>conradlocke/krea2-identity-edit |
| flux3-klein-box-edit-v1 | Image › Edit: the Box tool | 11.02 GB | 3 | Comfy-Org/Flux-2-Dev<br>Comfy-Org/Krea-2<br>KoalaNation/FLUX-3-KLEIN-MODELS |
| minimax-h3-t2v | Video: a take from a prompt | 42.47 GB | 4 | Comfy-Org/MiniMax-H3<br>Nodes: chanon/comfyui-obvpm-timeline<br>kijai/ComfyUI-KJNodes<br>ComfyUI core ResolutionSelector (ComfyUI >= 0.25.0) |
| minimax-h3-frames-v1 | Video: a take from keyframes | 41.05 GB | 5 | Comfy-Org/MiniMax-H3<br>Kijai/MiniMax-H3-experimental<br>Momoking/MiniMax-H3-Turbo-Lora-ComfyUI<br>Nodes: chanon/comfyui-obvpm-timeline |
| minimax-h3-references-v1 | Video: a take from reference pictures, clips and sound | 45.59 GB | 6 | Comfy-Org/MiniMax-H3<br>Kijai/MiniMax-H3-experimental<br>Momoking/MiniMax-H3-Turbo-Lora-ComfyUI<br>Nodes: chanon/comfyui-obvpm-timeline |
| minimax-h3-extend-mctx-v1 | Video: continue a take | 40.43 GB | 4 | Comfy-Org/MiniMax-H3<br>Kijai/MiniMax-H3-experimental<br>Nodes: chanon/comfyui-obvpm-timeline |
| minimax-h3-bridge-mctx-v1 | Video: fill the gap between two takes | 40.43 GB | 4 | Comfy-Org/MiniMax-H3<br>Kijai/MiniMax-H3-experimental<br>Nodes: chanon/comfyui-obvpm-timeline |
| minimax-h3-finish-v1 | Video: finishing pass (latent upscale, frame interpolation) | 4.49 GB | 4 | Comfy-Org/Frame-Interpolation<br>Comfy-Org/MiniMax-H3<br>KENIC-1/minimax_h3_latent_upscaler_3d_fp16_V2<br>Kijai/MiniMax-H3-experimental<br>Nodes: chanon/comfyui-obvpm-timeline<br>LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler |
| minimax-h3-prepend-mctx-v1 | Video: lead into a take | 40.43 GB | 4 | Comfy-Org/MiniMax-H3<br>Kijai/MiniMax-H3-experimental<br>Nodes: chanon/comfyui-obvpm-timeline |
| relayvsr-video-upscale-v1 | Video: upscale ×2 or ×4 (RelayVSR) | 10.58 GB | 4 | Comfy-Org/RelayVSR |

</details>

To see what a workflow needs, ask Hermes: "Which model files am I missing for &lt;workflow&gt;?" The tool
`continuo_models_missing` compares the list with your ComfyUI folders and names each missing file with its folder, size, sha256, license and
pinned download link. It never downloads anything.

### Other external parts

Not installed by this plugin.

| Part | Needed for, where, measured size |
|:---|:---|
| ffmpeg | required for Cut, Deliver and Audio; found through FFMPEG_PATH, then PATH, then C:\ffmpeg\bin. Tested with ffmpeg 7.1.1 (gyan.dev full build, 0.44 GB measured). |
| ComfyUI | optional; only for generating images, video, music and voice. Root: FILM_SUITE_COMFY_ROOT (default Documents\ComfyUI-Easy-Install\ComfyUI), server http://127.0.0.1:8188 (fixed: Continuo talks to a ComfyUI on this machine at this address only). The ComfyUI folder without models, custom nodes and outputs measured 0.27 GB; its Python environment, custom nodes and models need extra space (model sizes above). |
| optional engines | beat tracking (measured 4.3 GB with its torch environment), scene analysis (0.67 GB), song map (0.16 GB), speech detection (2.3 MB), inpainting. Not installed by this plugin; features that need one show it as unavailable. |

### Optional analysis engines

Run these from the plugin directory. Beat, song and scene create their own Python environments; they never modify the Hermes environment. Sizes below are the existing measurements, not necessarily the total download or disk use of a new installation.

| Engine | Improves | Installer | Measured size |
|:---|:---|:---|:---|
| Beat | Beat tracking | `python scripts/install-beat-engine.py` (optional `--cpu-only`) | 4.3 GB, including torch environment |
| Song | Song-map analysis | `python scripts/install-song-engine.py` (optional `--cpu-only`) | 0.16 GB |
| Scene | Scene analysis | `python scripts/install-scene-engine.py` (optional `--cpu-only`) | 0.67 GB |
| LaMa | Inpainting for Remove | `python scripts/install-lama-engine.py --reuse-venv VENV --model BIG_LAMA_PT` | Not measured |
| Silero VAD | Speech detection for dialogue ducking | `python scripts/install-speech-engine.py` | 2.3 MB |

Song and scene also require their candidate model files; see each installer's `--help`. LaMa downloads nothing and reuses an existing separate environment with torch, numpy and Pillow (for example the scene engine's), rather than creating one; provide the required TorchScript model. Do not use the Hermes environment for LaMa.

Silero installs the model only; dialogue ducking also needs `onnxruntime` importable by the Continuo backend Python process. It does not use a separate engine venv.

**Transcription:** `faster-whisper` is provided by Hermes' local speech-to-text (voice) option. Continuo uses `large-v3` offline only: cache `Systran/faster-whisper-large-v3` beforehand, for example by running `python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-large-v3')"` in the same user/cache environment. Without the package or cached model, transcription shows as unavailable.

On Linux/macOS the default engine location is `~/AppData/Local/hermes/film-suite/engines/` unless `LOCALAPPDATA` or the engine lookup override `FILM_SUITE_BEAT_ENGINE_DIR`, `FILM_SUITE_SONG_ENGINE_DIR`, `FILM_SUITE_SCENE_ENGINE_DIR`, or `FILM_SUITE_INPAINT_ENGINE_DIR` is set; the installers do not read these engine-specific overrides, so use their `--engine-dir` flag to match one.

Thanks to @ShawnCRMer for reporting the missing installer (#1).

## Talk to Hermes

Continuo gives Hermes 209 `continuo_*` tools that cover all pages. Hermes picks the right ones from a plain
sentence, and the page you have open shows the result live.

| You say | Hermes uses |
|:---|:---|
| "Open the sample film in Continuo, swap the first two shots and export an MP4." | `continuo_project_sample`, `continuo_timeline_apply`, `continuo_timeline_export_check`, `continuo_timeline_export` |
| "Rewrite scene 2 so the ferryman speaks first." | `continuo_script_read`, `continuo_script_scene_write` |
| "Analyse the song and put it under the cut." | `continuo_music_analyse_start`, `continuo_use_in_cut` |
| "What is running in Jobs? Stop the render." | `continuo_jobs_list`, `continuo_timeline_export_cancel` |
| "Which model files am I missing for minimax-h3-frames-v1?" | `continuo_models_missing` |
| "Is Continuo ready, or still preparing?" | `continuo_dependencies` |

## What this plugin does on your machine

Everything Continuo does outside its own window: the install scan, the network, programs, background work, data and private Hermes access.

### Install scan

Hermes' install scan rates Continuo as **caution** because Continuo starts programs (ffmpeg, ffprobe and the optional
local engines listed under [Programs it starts](#programs-it-starts)), talks to `127.0.0.1` (ComfyUI and those
engines), reads `/proc/self/fd` on Linux to re-open a file it already holds, and ships a large bundled
`desktop/plugin.js` (encoded data and `../` import paths inside it).

Once Continuo is listed in the Hermes plugin catalog, it installs by name: `hermes plugins install film-suite`.

### Network

Continuo calls only these hosts:

| Host | Why |
|:---|:---|
| `127.0.0.1` | local ComfyUI (HTTP + websocket) and the plugin's own loopback routes |
| `civitai.com` | LoRA lookup by file sha256, on the user's request |
| `image.civitai.com` | LoRA preview images of that lookup |
| `imagecache.civitai.com` | LoRA preview images of that lookup |
| `optim-images.krea.ai` | style-browser thumbnails of an uncached board |
| `cloud.cp.jku.at` | Beat This! checkpoint download when the optional beat installer is run |
| `download.pytorch.org` | PyTorch wheels when optional hash-locked engine installers are run |
| `pypi.org` | Python packages when optional hash-locked engine installers are run |
| `raw.githubusercontent.com` | pinned Silero VAD model when its optional installer is run |

Never fetched:

- Links in documentation and metadata only: `hermes-agent.nousresearch.com`, `github.com`, `fountain.io`, `www.w3.org`, `ns.adobe.com`, `huggingface.co`, `docs.comfy.org`, `www.minimax.io`, `ffmpeg.org`.
- Source citations in the bundled music reference list: `blogs.loc.gov`, `downloads.bbc.co.uk`, `imslp.org`, `mtosmt.org`, `songdata.io`, `www.musicnotes.com`.

The language model Hermes uses is your Hermes setting, not part of this plugin; it may be remote.

### Programs it starts

| Program | Why |
|:---|:---|
| `ffmpeg` | decode, render and export pictures, clips and sound (Cut, Deliver, Audio) |
| `ffprobe` | read the length, size and streams of media you add |
| `engine venv pythons` | optional analysis engines (beat tracking, song map, scene analysis, speech detection, inpainting), each in its own Python environment, only when you start that analysis and only if the engine is installed |
| `explorer/open/xdg-open` | "Show in folder" for a file you asked to reveal |

### Background work

Only while something you started runs:

- export jobs (one thread per Deliver export; cancellable from Deliver)
- control-map extraction jobs (Video controls)
- voice-take jobs (Audio voice)
- ComfyUI job watchers (websocket to 127.0.0.1 while a generation you submitted runs)
- picture-mask jobs (asyncio tasks for a mask you asked for)
- style-board thumbnail downloads (a small thread pool while a style board you opened has no cached thumbnails)

### Where it keeps data

- your project folders: project.json, script/, state/, pictures/, clips/, sound/, output/ (Deliver) and exports/ (script, picture and previz exports) inside the folder you chose
- <HERMES_HOME>/plugin-data/film-suite/: recent projects, job databases, image edits, LoRA cache, style thumbnails
- <HERMES_HOME>/cache/film-suite/: staging copies of results before they are published to a project
- %LOCALAPPDATA%\hermes\film-suite\engines\ (on Linux and macOS ~/AppData/Local/hermes/film-suite/engines/ unless LOCALAPPDATA is set): optional analysis engines, only if you install them
- a per-profile receipt key inside the Hermes home, protected with a user+SYSTEM-only ACL on Windows (0600 on POSIX)

### Private Hermes access

Hermes has no public plugin interface for the caller identity Continuo needs, so it uses Hermes internals:

- **Hermes' own session check**, so Continuo can verify that a request to its image and film routes really comes from
  your signed-in Hermes desktop app. Continuo never reads, holds or forwards the session token: it asks Hermes'
  verifier for a yes or no. Hermes' tool calls reach the same routes in-process, and they work only after your desktop
  app has been verified once. The desktop app does this automatically when Continuo opens.

<details>
<summary>The internals by name</summary>

- `hermes_cli.web_server._has_valid_session_token (caller_scope.py)`
- `hermes_cli.dashboard_auth.base.Session (caller_scope.py)`

</details>

## Troubleshooting

| You see | Do this |
|:---|:---|
| The install asks about a **caution** rating | Expected for this plugin (see [Install](#install)). Answer `y`, or run the install again with `--force`. |
| Continuo is not in the sidebar | Restart Hermes after `hermes plugins enable film-suite`. |
| "Preparing the Audio engine, or restart Hermes" or "Preparing picture import, or restart Hermes" | A Python package is unavailable in this process. Hermes may still be setting it up, or may need a restart after setup. Hermes can check with `continuo_dependencies`. |
| The Sample film tile says "ffmpeg not found" | Install ffmpeg and put it on `PATH`, set `FFMPEG_PATH`, or place it in `C:\ffmpeg\bin`; then choose **Sample film** again. No half-made sample is left behind. |
| "ComfyUI is not running." | Start ComfyUI on this machine at `http://127.0.0.1:8188`. Continuo uses no other address. |
| A model is missing | Ask Hermes "Which model files am I missing for &lt;workflow&gt;?" (`continuo_models_missing`), which names each file with its folder, size, sha256 and pinned download link; put the file into that ComfyUI folder. |
| An analysis shows as unavailable | It may need an optional engine. See [Optional analysis engines](#optional-analysis-engines). |

## Roadmap

Next: script import and export (FDX, Fountain, PDF), voice, song and loop generation on **Audio**, color on clips, and
a final pass over the shared UI components. Details are in `ROADMAP.md`.

## Feedback

Issues are welcome at [github.com/realartsbro/continuo/issues](https://github.com/realartsbro/continuo/issues). Please include:

- the page (Script, Audio, Image, Video, Cut, Deliver, Media, Jobs) and the steps that led there
- a screenshot
- your Hermes version (`hermes --version`)
- the Continuo build: `source_commit` from `BUILD-INFO.json` in the plugin folder

Pull requests are welcome through a fork. This repository is a built tree: the maintainer carries an accepted change
over into the source, and it comes back here with the next build.

## License and credits

MIT License, Copyright (c) 2026 Arts Bro. See `LICENSE`. Continuo is made and maintained by Arts Bro.

Third-party code and assets are listed in `THIRD_PARTY_NOTICES.md`: CodeMirror and three.js (bundled), Lucide icons,
the timeline edit and interchange logic ported from FilmCraft, ComfyUI workflow templates by Comfy Org, and the Krea
moodboard style prompts from [ComfyUI-Krea-Moodboards](https://github.com/Andro-Meta/ComfyUI-Krea-Moodboards) (MIT, unofficial, not affiliated with Krea AI). Models you
install keep their own licenses. The license named on each model card is recorded in
`dashboard/workflows/model-sources.json`.
Hermes Agent is by Nous Research.

Continuo is an entry to the Nous Research Hermes Agent Plugin Hackathon.
