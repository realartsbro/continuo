---
name: continuo
description: "Use when the artist works on a film, script, shot, cut or song in Continuo."
---

# Continuo

Continuo is the artist's film workstation inside Hermes: Script, Image, Video (Shoot), Audio, Cut and Deliver.
Its tools all start with `continuo_`. Use this skill only for work in Continuo, not for films or music in general.

## First: read suite state

Call `continuo_suite_state` once before anything else. It names the open window, page (`start`, `script`, `music` = Audio,
`image`, `shoot` = Video, `timeline` = Cut, `deliver`), the open project and the Media column. Every page tool works on
what the artist sees, so the answer decides the tool family below. No connected window means the artist has no Continuo
page open: say so, or use the route tools (they work on the project files without a page).

## Intent → tools

| Intent | Tools |
|---|---|
| Which page and project are open; open a page or project, filter or select Media, hide an inspector | `continuo_suite_state`, `continuo_suite_action` (payloads in its description) |
| Projects: list, open, create, rename, settings; is an engine still being prepared | `continuo_projects_list`, `continuo_project_*`, `continuo_dependencies` |
| Screenplay: read, write scenes, move, acts, numbers, export | `continuo_script_*`, `continuo_hermes_changes` |
| The open Image page: prompt, generate, keep, edit, develop | `continuo_image_state`, `continuo_image_action`, `continuo_image_catalog` |
| Pictures as files and jobs: submit, poll, keep, import, export, upscale, masks, LoRAs, styles | `continuo_image_*`, `continuo_styles_*`, `continuo_style_categories_*`, `continuo_recipe_read` |
| A picture as a video keyframe or Film image | `continuo_use_in_shoot`, `continuo_film_image_*` |
| Video takes: targets, submit, poll, continue, finish, chains | `continuo_targets_catalog`, `continuo_target_health`, `continuo_models_*`, `continuo_video_*`, `continuo_h3_*`, `continuo_take_*`, `continuo_chain_*` |
| The open Audio page: play, IN-OUT, markers, grid, stems | `continuo_music_state`, `continuo_music_action` |
| A song: open, analyse, beats, sections, map, stems | `continuo_music_*`, `continuo_song_map_*`, `continuo_audio_stems_*` |
| Make music | `continuo_music_gen_*` |
| Voices and spoken lines | `continuo_voice_*` |
| The Cut: read, edit verbs, sound tracks, footage; a song or its IN-OUT into the cut (Use in Cut) | `continuo_timeline_*`, `continuo_use_in_cut` |
| Deliver: check, export, show the package | `continuo_timeline_export*` |
| Media: list, probe, marks, references, control layers, shot analysis | `continuo_media_*`, `continuo_references_read`, `continuo_reference_use`, `continuo_control_*`, `continuo_scene_*` |
| Shots, elements and Previz: shot record (size, framing, lens, camera), characters/places/props with versions, a scene's previz set, shot cameras, figures, block a scene as an undoable proposal | `continuo_shot_plan_*`, `continuo_elements_*`, `continuo_previz_*` |
| Jobs and the GPU line | `continuo_jobs_*`, `continuo_gpu_status` |
| Review notes and decisions | `continuo_review_*` |
| Subtitles of the cut (ST1): read, edit lines and style (same verbs as the hand), create from audio, SRT / WebVTT | `continuo_subtitles_*` |

Load a tool's schema before the first call (`tool_describe`); the description names its payload and refusals.

Before writing a prompt, load the matching skill: `film-suite:video-prompts` for a video take, `film-suite:image-prompts`
for a picture or an edit. Each model reads its own prompt format.

## Image page

- `continuo_image_action` takes the `window_id` that `continuo_image_state` lists, not the one from
  `continuo_suite_state`; leave it out when one Image pane is open.
- A batch shows as one job in `continuo_image_state` and `continuo_jobs_list`; `continuo_media_feed` (kind image)
  lists every picture of it with its id and seed.
- To look at a picture, write a small copy with `continuo_image_export` (jpeg, size long_edge 768; the answer's file
  lies under `project_folder`) and open that file; `continuo_image_picture` returns the full picture as base64,
  megabytes per call.

## Previz (block a scene)

1. `continuo_script_read`: the scene id (`s-...`) and its shot ids. 2. `continuo_previz_block_scene {scene}`: the
heading's place from the set kit, a figure per person posed by the action, a camera per shot. 3. Read the answer
(`scenes[scene]`, `cameras`) and fix only what the artist asked for: set pieces with `continuo_previz_write` (the
full `boxes` list; a kit piece is a box named with a kit word: pier, water, ferry, hull, lighthouse, rocks, shore,
cabin, lamp, console, bench, table, chair, crate, suitcase, bollard, rope), a figure with `continuo_previz_figure`
(pos [x, z], yaw 0 faces the camera side +z, pose stand|sit|walk|coil|lean), a camera with `continuo_previz_camera`
(`expected_revision` = the read's `shot_plan_revision`). Metres, y up, the floor is y = 0. Each write is one
proposal the artist can undo.

## Budget: read once, change once, check once

- Read the state you need once (suite state, then the one read of that family, e.g. `continuo_timeline_read` detail
  `summary`). Do not re-read to be sure.
- Change with one call that carries the whole change (`continuo_timeline_apply` takes many verbs at once; script
  writes take the revision you read). Pass `label` to `continuo_timeline_apply`: one short sentence in the artist's
  words saying what the change does; the artist reads it next to Undo and in History.
- Check once: the change's own answer is the check. Read again only when the answer says pending or asks you to.
- A refusal names a reason code and usually the fix: apply the fix once; after two refusals of the same kind stop and
  tell the artist what was refused.

## Never report done without the receipt

Never say an action is done unless the tool's answer is its receipt: `ok: true` with `status: acknowledged` (page
actions), a new `revision` (script, cut, marks), a `job_id` whose poll reached a finished state, or the written file.
`pending`, `withdrawn`, `queued`, `running`, a missing answer or a timeout are not done: say what is still open.
Generation (video, image, music, voice) runs on the artist's local GPU: submit only when the artist asked for it.

## Consent

The artist picks folders, files, ratings, keeps and names. Never approve a folder, choose a file, rate or keep on the
artist's behalf, and never claim an export was imported into another program. The one exception: when the artist asks
you to pick ("keep the best one"), do it: look at the pictures, keep one under a short name that describes it, and
say which one and why, so the artist can change it.

A folder is always the artist's to approve. A file the artist names in chat is the artist's choice: copy that one file
with your own file tools into the project's media folder (`project_folder` from `continuo_project_state`, then
`media/`), add it with the matching tool (`continuo_image_import`, `continuo_timeline_footage_add`,
`continuo_timeline_audio_add`, `continuo_music_open`) and say where you put it. Never ask the artist to move files by
hand, never pick a file the artist did not name, and never copy a whole folder.
