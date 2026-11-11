---
name: image-prompts
description: "Use when writing or repairing a prompt for a Continuo image model (create or edit)."
---

# Image prompts (Continuo image models)

Each image model reads a different grammar; the catalog names it per model (`prompt_grammar` in
`continuo_image_catalog` / `continuo_targets_catalog`). Pick the model, then write in its grammar. Never carry one
model's syntax into another. Only models the picker marks runnable can render.

## 1. Which model, which grammar

| Model (target) | Use it for | Grammar, length |
|---|---|---|
| Krea 2 Turbo / Raw (`krea2-turbo-t2i-create`) | photographic and painted pictures | prose; long and detailed is best |
| Krea 2 Identity / General Edit V2 (`krea2-identity-edit-v2`, `krea2-general-edit-v2`) | restage a person / general edits | short instruction |
| Qwen-Image 2.1 Create / Edit | general pictures, text, up to 10 references | elaborate prose; edit = instruction |
| FLUX.2 Klein (`flux2-klein-9b-t2i-v1`) | fast drafts, exact colours | prose, subject first, 40-70 words |
| FLUX.3 Klein (`flux3-klein-sd-create-v1`) | detailed scenes and layouts | one caption block, 100-350 words |
| MOSS-Image SFT (`moss-image-sft-create-v1`) | text to image, dense text | English prose or tags + negative |
| Anima Turbo (`anima-turbo-create-v1`) | anime and illustration | tags + short prose |
| Ideogram 4 (`ideogram4-create-v1`) | layout, posters, typography | JSON caption only |
| P-H3-Image Create / Edit (`p-h3-image-lora-t2i-v1`, `p-h3-image-lora-edit-v1`) | editorial photos, film frames, prints; edits with up to 7 references | one full scene; `Edit <Picture 1>.` |

## 2. Prose models (Krea 2, Qwen, FLUX.2/3 Klein, MOSS)

- Subject and action first, then clothes and materials, composition, place, light (source, direction, what it hits), medium.
- Name things concretely ("oxblood wool coat", "brushed aluminium"); counts as numbers; an exact colour as `#RRGGBB` with its name.
- State what you want, not what you do not want: these models take no useful negative (MOSS is the exception). "no people" adds people; say "the empty platform".
- No quality tags ("masterpiece, 8k"), no `(word:1.3)` weights.
- Text to render goes in double quotes, short, with where it sits: `a shop sign above the door reading "OPEN"`.
- Enhancers are off unless the artist asks: Qwen `prompt_enhancer` and FLUX.3 `prompt_upsampler` rewrite the prompt; show the rewritten prompt the job returns. With them off, write what they would write:
  - FLUX.3 Klein (trained on upsampled captions): one block opening with the medium ("Full-color outdoor photograph, ..."), then scene, light, camera, then each subject with colours, materials and frame position (frame-left, foreground); people get age, skin tone, hair, clothes; no mood words.
  - Qwen-Image 2.1 needs far more description without its enhancer than Klein does. Transparent picture: `This is an RGBA image with transparency. <subject>. The image has alpha channel and the background is transparent.`
- MOSS: English works best; tags work too. Short prompts vary by seed: write it out. The editable negative is prefilled with its guide's tags; keep it. Put real line breaks in multi-line text.

## 3. P-H3-Image (not H3 video)

P-H3-Image is H3's video transformer with an image LoRA, 8 steps, no CFG. Its prompt upsampler is not installed: your
words reach it as written. Full, specific prompts render far better than short ones; very long ones worse. Unlike H3
video: no fields, shots, sound or camera moves.
- Open with one sentence that says what the picture is: "A 35 mm documentary photograph by a street photographer", "A frame from a Japanese anime TV series", "A two-colour risograph print". This sets the look better than style words.
- One scene: a viewpoint, one main subject caught in a moment, a few supporting things, light with a source and a direction.
- Every visible word in quotes, short, placed, then "no other text". No text wanted: do not mention signs or labels.
- No hype words: stunning, epic, cinematic, vibrant, hyper-detailed, 8k, masterpiece make it glossy and oversaturated.
- No negative exists. Best at 8 MP; at 2-4 MP faces, hands and small background people break more often (default 8 MP, 16:9; go lower only when the artist asks).
- Edit: `Edit <Picture 1>. <the change, described as the finished result>. Keep <what must stay> exactly as it is.` Name the exact thing and where ("the old man's faded blue cap", not "his hat"); give new things a size next to something present; list faces, poses, background and light that stay, or it redraws the whole picture. Without `Edit <Picture` at the start the node adds `Edit <Picture 1>. ` itself. References are `<Picture 2>` ... in the order sent; the server appends "<Picture N> is the <role> reference."; say what each one gives: "The woman from <Picture 2> sits on the bench in <Picture 1>."

## 4. Anima (tags)

Lowercase tags with spaces (`blunt bangs`); only score tags keep underscores. Order: quality and safety
(`masterpiece, best quality, score_7, safe,`), count (`1girl`), character, series, artist (`@name`, the `@` is
required), general tags; a short sentence may follow. Weights need about 2. The negative is fixed; fight a trait
positively (`short hair`). No edit model.

## 5. Ideogram 4 (JSON only)

Trained only on JSON captions: prose is refused here and elsewhere triggers its safety filter. Keys in this order:
```
{"high_level_description":"One full sentence about the whole picture.",
 "style_description":{"aesthetics":"...","lighting":"...","photo":"35mm, eye level","medium":"photograph","color_palette":["#1B2230","#C9A86A"]},
 "compositional_deconstruction":{"background":"...","elements":[
   {"type":"obj","bbox":[300,250,700,550],"desc":"...","color_palette":["#5B6573"]},
   {"type":"text","bbox":[80,100,230,900],"text":"NIGHT FERRY","desc":"bold white sans serif headline"}]}}
```
- `style_description` has `photo` (photographs) or `art_style` (anything else), never both. Photo order: aesthetics, lighting, photo, medium, color_palette. Non-photo: aesthetics, lighting, medium, art_style, color_palette.
- `bbox` is `[top, left, bottom, right]` on 0-1000 from the top-left (y first). Colours uppercase `#RRGGBB`.
- Text elements: `text` = exact characters, `desc` = how they look. `high_level_description` is one full sentence (20+ words); fragments have rendered as visible text. Send the artist's own words as `title`.

## 6. Edits

Instruction edits say the change and what stays, then stop.

`continuo_image_edit` (area edits) wraps your prompt in the operation's sentence unless `options.instruction_mode` is
`raw`. In template mode the prompt is the THING, not a sentence:

| Operation | Server sends | Your prompt |
|---|---|---|
| fill | "Fill the grey area with {prompt}. Keep everything else ..." | `a wooden rowing boat with a red hull` |
| replace | "Replace {target} completely with {prompt}, ..." | the new object; `options.target` names the old one |
| remove | background-only fill | optional |
| restyle | "Redraw the whole picture as {prompt}. ..." | `a loose ink-and-wash drawing` |
| relight | "Relight the photo with {prompt}. ..." | `low warm sunset light from the left` |
| outpaint | "Extend the picture into the grey area ..., with {prompt}" | optional |

Read `continuo_image_edit_capabilities` first. Box edits: boxes carry `desc`; refer to a box as `<id>`; read
`continuo_image_box_prompt` before running.

Catalog edits (`continuo_image_workflow_submit` with references):
- P-H3-Image Edit: section 3.
- Qwen-Image 2.1 Edit: `<image1>` is the target, `<image2>` ... references; the server adds a "Reference roles" list.
- Krea 2 Edit V2: image 1 is the picture (in two-image Identity Edit: the scene), image 2 the person. Say how much of the person is visible, name the new outfit and what is gone ("remove her coat"), and describe light by where it lands ("a magenta sign paints the left half of her face pink"). Identity Edit ignores removals, recolours and camera changes: use General Edit. Removals: Raw model, 20 steps, CFG 3.
- Every reference needs a stated job; an unnamed one is copied loosely or ignored. Fewer hold better. Never a whole character sheet as identity: one clean view.

## 7. Examples

P-H3-Image Create: `A 35 mm documentary photograph by a harbour photographer. From deck height, a deckhand in a yellow
oilskin coils wet rope on a small ferry at dusk, his breath visible. Rust streaks on the white railing, a pale grey sea
behind. Cold light from the overcast sky on the left, one warm cabin window on the right. A life ring on the railing
reads "MARIA", no other text.`

P-H3-Image Edit (+ `identity` reference): `Edit <Picture 1>. The man from <Picture 2> stands at the railing, in the
same yellow oilskin, looking out to sea, about as tall as the cabin door. Keep the deck, the sea, the light and the
framing of <Picture 1> exactly as they are.`

## 8. Failures and fixes

- Wrong subject leads: it is not first. Move it to the start.
- Unwanted thing appears: you named it to forbid it. Describe the positive state.
- Garbled text: long or unquoted. Quote it, keep it short; Ideogram for real layouts.
- Glossy P-H3 picture: hype words, or no "what the picture is" opener.
- P-H3 edit redrew everything: no keep list. Use the edit form.
- Ideogram refuses or draws text: prose, a fragment or words outside the braces. Valid JSON, a `bbox` per element.
- Unasked small print (credits, prices) on Qwen: check corners and backgrounds.
- MOSS softens backgrounds even when told "sharp": a model trait.
- Fill sentence reads twice ("Fill the grey area with fill the area with ..."): a sentence in template mode. Send the thing only, or `instruction_mode: raw`.
- Identity drifts: identity reference missing or unnamed, or a sheet as identity. Name each picture's job.
