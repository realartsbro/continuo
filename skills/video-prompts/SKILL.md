---
name: video-prompts
description: "Use when writing or repairing a MiniMax H3 video prompt for a Continuo video take."
---

# Video prompts (MiniMax H3 in Continuo)

Every local video target sends your prompt to MiniMax H3 exactly as written. Hosted H3 rewrites requests first; here
nothing does, so write what its rewriter writes (MiniMax-H3 `docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md`,
`..._ref_en.md`). Free prose, camera jargon and mood words make it erratic.

## 1. The target decides the format

| Target | Inputs | Format |
|---|---|---|
| `minimax-h3-t2v` | none | three fields |
| `minimax-h3-frames-v1` | keyframes: frame 0, last frame, or between | alignment line + three fields |
| `minimax-h3-references-v1` | reference pictures, clips, sounds | six fields |
| `minimax-h3-extend-mctx-v1`, `-prepend-`, `-bridge-` | a take with its .mctx sidecar | three fields, new part only |

Three fields, this order, exact spelling, value on the next line: `integrated_multimodal_description:`,
`overall_soundscape:`, `non_diegetic_music:`. Six fields: `subject_definitions:`, `summary:`, `retention_analysis:`,
`detailed_description:`, `overall_soundscape:`, `non_diegetic_music:`. The two never mix.

Frames: one alignment line first, then a blank line. Copy it exactly (the bracket difference is official):
- frame 0 only: `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`
- frame 0 and last: `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.`
- last only: `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.`

`N` = final shot number. `S.SS` = real length, two decimals: the server snaps `duration_seconds` to 17k+5 frames at
24 fps (4 s = 4.46, 5 s = 5.17, 6 s = 6.58, 8 s = 8.00, 10 s = 10.12).

## 2. The description

- Open `[Shot 1]` with medium and composition: `[Shot 1] Live-action, cinematic, a medium-wide shot frames ...`.
- Only what can be seen or heard: not "she feels guilty" but "she glances at the door and tightens her grip".
- Give the action a path and an end state. First frame: picture -> onset -> result. First + last: the path between them, one shot. Last only: a plausible earlier state -> landing exactly on the picture.
- With a keyframe, describe what changes, not the picture. The keyframe must show the prompt's scene, or the take holds it one frame and cuts away.
- `[Shot 1]` has no timestamp; later `[Shot 2] At 00:03.500, the camera cuts to ...`, increasing, inside the length. Every `[Shot N]` is a hard cut that must bring new information; for a size change move the camera.
- Restate light and time of day in every shot.
- Visible text in double quotes, verbatim. A surface that grows large (sign, vehicle front) gets a plain description ("a blank pale panel"), or H3 invents lettering on it.
- An off-screen cause stays in `overall_soundscape`; describing its visible effects pulls it into frame.

## 3. Camera

Closed list: Zoom In/Out, Push In/Pull Out, Pan, Truck, Tilt, Pedestal (Up/Down, Left/Right), Arc Shot, Tracking Shot,
Static Shot, Shake Slightly/Strongly, POV, Roll, plus `with small|large amplitude` and `at slow|fast speed`. Write a
sentence in the shot ("The camera pushes in with small amplitude at slow speed toward the letter."), never a bracket
tag; one move per shot. Dolly, crane, handheld and lens lengths are not in the list.

Or, on Frames or References with no keyframe or only a frame-0 keyframe, pass `camera` {move arc_shot (direction
left|right) | push_in | shake, amplitude, speed} in args: the server writes the sentence after `[Shot 1]` and the
frame-0 line itself. Then write no camera words, no tag, no `[Shot 2]` (refused) and no alignment line. After a camera
take read `continuo_take_freeze_check`. A locked shot with a small moving subject can read as frozen while right: let
the artist look. Frozen although the prompt asked for motion = same args, seed + 1.

## 4. Sound

- Speech: speaker outside the tag, `The old fisherman with a low, rough voice (S1) says: <d>[English] Leave the light on.</d>`. Inside `<d>` only the language tag and the exact words in their own language, no quotation marks (quotes render as subtitles), no ellipses. IDs follow speaking order and stay fixed; two at once `(S1,S2)`. Voiceover: `says in an off-screen voiceover: <d>...</d> while his lips remain completely closed.` A line that runs across a cut gets `<scenetrans>` on both sides; speech cut off by the clip's end gets `<cutoff>`.
- Nobody speaks: write "No one speaks." H3 invents speech that is not addressed, most often when the clip is longer than what is written.
- `overall_soundscape`: 1-4 sentences of concrete sources (rain on glass, a hinge). No dialogue, no music. `N/A` only for total silence.
- `non_diegetic_music`: 1-3 sentences of instruments, tempo, dynamics (no mood words), or `N/A`. For a take without score write `N/A` and keep musical words ("swell", "melody") out of every field. A radio in the scene belongs in the description.
- `reference_audios` (`voice_of` subject k, or `music_bed`): the server writes their definition and retention lines. A `sound_track` replaces the shot's sound from frame 0; keep its words in `<d>`.

## 5. References (six fields)

- All six fields in English except `<d>` and visible text. `subject_definitions`: one line per tracked item and its job: `<Subject 1> is the woman whose face, hair and coat come from <Picture 1>.` A reference the text never names does nothing. A standalone `<Picture N>` line only for a frame or storyboard anchor. Pictures count in `reference_images` order; `<Video N>` (only for a whole clip that is edited, continued or copied for pacing) and `<Audio N>` count separately; guide keyframes get no label.
- `summary`: one paragraph opening with the task type in brackets: `keyframe completion`, `reference generation`, `video editing`, `video continuation`, `audio reuse`, `audio reference`, joined by ` + `. No new labels.
- `retention_analysis`: `<Subject 1> (appears in [Shot 1]): fully_preserved - face, hair and coat stay the same.` Visual labels: `fully_preserved`, `partially_preserved`, `attribute_transfer`, `weak_reference`. Audio: `fully_copy`, `partially_copy`, `reference`, `weak_reference`. Never `(S1)` here.
- `detailed_description`: one or two style sentences, then the shots, 350-500 words. Do not re-describe what a reference supplies. A character sheet is for identity only, not layout.

## 6. Continuation (Extend, Prepend, Bridge)

Three fields, no alignment line, only the new seconds (args `prompt`, `duration_seconds`, `seed`, `steps`, `sampler_name`, `scheduler`, `source`), times from the new part's start. Extend starts from the take's
last state, Prepend ends in its first state, Bridge joins two takes. Repeat only what drifts (identity, costume,
light), then the new action, then a clear end state. A take without score keeps `non_diegetic_music: N/A`.

## 7. Length and retakes

- `duration_seconds` is 4-15 (continuations: 2-14 new seconds). H3 fits the written action into the clip: a fast beat in a long clip plays slow, a calm beat in a short clip turns hectic. Size the clip to the action; trim in the Cut.
- A three-field description of about 100 words works; more beats than seconds get dropped.
- Draft size for trials, Native for keepers. Canvas aspect = keyframe aspect.
- Black and white: a greyscale keyframe; its colour beats every word.
- One change per retake: a prompt fix keeps the seed; a take frozen although the prompt asked for motion keeps the words and takes seed + 1.

## 8. Example (Text to Video, 5 s)

```
integrated_multimodal_description:
[Shot 1] Live-action, cinematic, a wide shot frames an empty ferry deck at dusk under low grey cloud. The camera trucks right with small amplitude at slow speed as a deckhand in a yellow oilskin coils a wet rope, drops it on the bollard and walks out of frame left. No one speaks.

overall_soundscape:
Diesel engine throb under wind gusts; the rope slaps the steel deck; gulls cry far off.

non_diegetic_music:
N/A
```
Frames, frame 0: the frame-0 line, a blank line, `[Shot 1] Live-action, cinematic, the woman in <Picture 1> stays by
the train window. She lifts her eyes from the letter and folds it once. No one speaks.`, both sound fields. Frames 0 and 123 at 5 s: the two-picture line with `Shot 1` and `5.17`; the body
ends "... and settles into the pose and framing of Picture 2 at the end of the shot." References: role line, `[reference generation] ...`
summary, a `fully_preserved` retention line, style sentences + `[Shot 1] ...`, both sound fields.

## 9. H3 video is not P-H3 image

P-H3-Image runs on the same transformer with a LoRA but reads another grammar (`film-suite:image-prompts`): no
fields, shots, sound, camera or alignment line; it opens by naming the kind of picture ("a 35 mm documentary
photograph"), never "cinematic" (a hype word there); edits begin `Edit <Picture 1>.`

## 10. Failures and fixes

- Burned-in subtitles or text on signs: quotes around speech. Use `<d>`.
- Invented speech: silence not stated. Add "No one speaks."
- Music in a score-free take: musical words somewhere. `N/A`, concrete sources only.
- Unwanted cut: a second `[Shot N]`. One shot, move the camera.
- Cut away after frame 0: keyframe and prompt disagree. Describe the keyframe's scene.
- Barely moves although the prompt asked for motion: frozen seed. Seed + 1, same words. A locked shot with small motion is right: leave it.
- References ignored: no role line, or too long. Name each job.
- A thing you excluded appears ("no cars"): naming it pulls it in. Describe what is there.
