---
name: image-prompts
description: "Use when writing or repairing a prompt for a Continuo image model (create or edit)."
---

# Image prompts (Continuo image models)

Each image model reads a different grammar; the catalog names it per model (`prompt_grammar` in
`continuo_image_catalog` / `continuo_targets_catalog`). Pick the model, then write in its grammar. Never carry one
model's syntax into another. What you mean stays the same; how you write it changes.

## 1. Which model, which grammar

| Model (target) | Use it for | Grammar |
|---|---|---|
| Krea 2 Turbo / Raw (`krea2-turbo-t2i-create`) | photographic and painted pictures from text | plain prose |
| Krea 2 Identity Edit V2 (`krea2-identity-edit-v2`) | one person into another scene, pose, outfit, light | short instruction |
| Krea 2 General Edit V2 (`krea2-general-edit-v2`) | remove, recolour, relight, background, sign text, outfit, restyle | short instruction |
| Qwen-Image 2.1 Create / Edit | general pictures, text, multi-reference edits | prose; edit = instruction |
| FLUX.2 Klein (`flux2-klein-9b-t2i-v1`) | fast drafts, materials, exact colours | prose, subject first |
| FLUX.3 Klein (`flux3-klein-sd-create-v1`) | detailed captions | long caption prose |
| MOSS-Image SFT (`moss-image-sft-create-v1`) | text to image | prose (+ editable negative) |
| Anima Turbo (`anima-turbo-create-v1`) | anime and illustration | tags + short prose |
| Ideogram 4 (`ideogram4-create-v1`) | layout, posters, typography | JSON caption only |
| P-H3-Image Create / Edit | full scenes from long prompts; edits with up to 7 references | literal prose, `<Picture N>` |

Only models the picker marks runnable can render; read `continuo_image_catalog` before choosing.

## 2. Prose models (Krea 2, Qwen, FLUX.2/3 Klein, MOSS, P-H3 Create)

- Describe the finished picture as you would to a photographer: subject and action first (what comes first reads as the subject), then clothes and materials, composition and camera height, place, light (source, direction, colour, what it hits), medium.
- Name things concretely: "oxblood wool coat", "brushed aluminium", not "nice red coat". Give counts as numbers.
- State what you want, not what you do not want. These models have no useful negative; "no people" adds people. Say "the empty platform".
- No quality-tag soup ("masterpiece, 8k") and no `(word:1.3)` weights. Repeat an important trait in other words instead.
- Text to render goes in double quotes, short: `a shop sign reading "OPEN"`.
- Length: Krea 2 and Klein do well at 40-120 words; FLUX.3 Klein and P-H3 reward full, specific prompts (100-300 words). P-H3 has no prompt upsampler: a short request stays short.
- Enhancers are off unless asked: Qwen `prompt_enhancer` and FLUX.3 `prompt_upsampler` rewrite the prompt; switch them on only when the artist wants it, and show the rewritten prompt the job returns.

## 3. Anima (tags)

Lowercase tags with spaces (`blunt bangs`, not `blunt_bangs`); only score tags keep underscores. Order: quality and
safety (`masterpiece, best quality, score_7, safe,`), count (`1girl`, `1boy`, `1other`), character, series, artist
(`@name`, the `@` is required), then general tags; a short sentence may follow. Weights work but need 1.5-2.0. The
negative prompt is fixed in this workflow; fight a trait positively (`short hair`). Anima has no edit model.

## 4. Ideogram 4 (JSON only)

The prompt must be one JSON object (prose is refused). Keys in this order:
```
{"high_level_description":"One full sentence about the whole picture.",
 "style_description":{"aesthetics":"...","lighting":"...","photo":"35mm, eye level","medium":"photograph","color_palette":["#1B2230","#C9A86A"]},
 "compositional_deconstruction":{"background":"...","elements":[
   {"type":"obj","bbox":[300,250,700,550],"desc":"...","color_palette":["#5B6573"]},
   {"type":"text","bbox":[80,100,230,900],"text":"NIGHT FERRY","desc":"bold white sans serif headline"}]}}
```
- `style_description` has `photo` (photographs) or `art_style` (anything else), never both. Photo order: aesthetics, lighting, photo, medium, color_palette. Non-photo: aesthetics, lighting, medium, art_style, color_palette.
- `bbox` is `[top, left, bottom, right]` on 0-1000 from the top-left (y first). Colours uppercase `#RRGGBB`, palette last.
- Text elements: `text` = exact characters, `desc` = how they look.
- `high_level_description` is one full descriptive sentence (20+ words); terse fragments there have rendered as visible text. Send the artist's own words as `title` so Jobs and Media show them.

## 5. Edits

Instruction edits say the change, not the whole scene again, and stop. The appearance comes from the picture.

`continuo_image_edit` (area edits on a picture) wraps your prompt in the operation's own sentence unless
`options.instruction_mode` is `raw`. So in template mode the prompt is the THING, not a sentence:

| Operation | Server sends | Your prompt |
|---|---|---|
| fill | "Fill the grey area with {prompt}. Keep everything else ..." | `a wooden rowing boat with a red hull` |
| replace | "Replace {target} completely with {prompt}, ..." | the new object; `options.target` names the old one |
| remove | background-only fill | optional |
| restyle | "Redraw the whole picture as {prompt}. ..." | `a loose ink-and-wash drawing` |
| relight | "Relight the photo with {prompt}. ..." | `low warm sunset light from the left` |
| outpaint | "Extend the picture into the grey area ..., with {prompt}" | optional |

Read `continuo_image_edit_capabilities` first: per model it lists which operations are wired, whether references and
strength apply and any measured warning (e.g. a model that leaves outpaint margins grey). Box edits (`box`): boxes
carry `desc`; refer to a box as `<id>` in the prompt; read `continuo_image_box_prompt` to see the final text before
running it.

Catalog edits (`continuo_image_workflow_submit` with references) label pictures per model:
- P-H3-Image Edit: the picture to edit is `<Picture 1>`, references are `<Picture 2>` ... in the order sent; the server appends "<Picture N> is the <role> reference." Say which picture gives what: "The woman from <Picture 2> sits on the bench in <Picture 1>." Name the region concretely ("the man on the left wears a green shirt", not "he wears").
- Qwen-Image 2.1 Edit: `<image1>` is the target, `<image2>` ... references; the server adds a "Reference roles" list.
- Krea 2 Edit V2: image 1 is the picture (in two-image Identity Edit: the scene), image 2 the person. One person, how much of them is visible, the outfit named concretely. Removals work best with the Raw model, 20 steps, CFG 3.
- Every reference needs a stated job; an unnamed reference is copied loosely or ignored. Fewer references hold better: add one only for a job no other covers. A colour reference makes the output colour.
- Never feed a whole character sheet as identity; use a single clean view of the person.

## 6. Example (Krea 2 Turbo)

`A deckhand in a yellow oilskin coat coils wet rope on the steel deck of a small ferry, seen from a low camera at
deck height. Rust streaks on the white railing, a pale grey sea behind, low cloud at dusk. Soft cold light from the
overcast sky, one warm cabin window glowing on the left. Photograph, 35 mm, fine grain.`

Edit (P-H3-Image, target + one `identity` reference): `The man from <Picture 2> stands at the railing of the ferry in
<Picture 1>, in the same yellow oilskin, looking out to sea. Keep the deck, the light and the framing of <Picture 1>.`

## 7. Failures and fixes

- Wrong subject leads: it is not first. Move it to the start.
- Unwanted thing appears: you named it to forbid it. Describe the positive state.
- Garbled text: long or unquoted. Quote it, keep it short; Ideogram for real layouts.
- Ideogram refused or draws text in the image: prose or a fragment summary. Valid JSON, full sentence.
- Edit changed the whole picture or the face: too much description. State only the change.
- Fill sentence reads twice ("Fill the grey area with fill the area with ..."): a full sentence in template mode. Send the thing only, or `instruction_mode: raw`.
- Identity drifts: identity reference missing or unnamed, or a sheet as identity. Name the job of each picture.
- Same result every seed in an edit: the references fix the composition. Fewer references, or change the prompt.
