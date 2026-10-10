/* The master prompts you paste into ChatGPT.
 *
 * These are templates, not fixed text: the shot count and the art direction are
 * chosen in the JSON help dialog and folded in here. The design block is
 * optional and OFF by default, because a script usually specifies its own look
 * and a second opinion stapled on top only fights it.
 *
 * Written as arrays of lines joined with \n — never template literals with
 * backticks, because these strings contain quotes and braces and get pasted
 * around by hand.
 */

const DESIGNS = [
  {
    id: 'none',
    label: 'From the document',
    note: 'No style is imposed — the script decides. This is the default.',
    block: null,
  },
  {
    id: 'pixar',
    label: 'Cinematic 3D animation',
    note: 'Polished feature-film 3D. Expressive adult characters, premium, not childish.',
    block: [
      'Use a polished cinematic 3D animated feature-film style with expressive adult characters, soft stylized facial features, slightly cartoon-stylized proportions, detailed hair and clothing, cinematic lighting, rich but natural colors, and shallow depth of field.',
      '',
      'The result should clearly look animated, not photorealistic. It should be mature and premium, not childish, exaggerated, plastic-looking, or overly cartoonish. Do not name any studio in the prompts.',
      '',
      'For internal body visuals, use a clean stylized cinematic 3D medical-explainer aesthetic. Keep anatomy tasteful, non-graphic, easy to understand, and visually consistent with the character scenes.',
    ],
  },
  {
    id: 'clay',
    label: 'Clay stop-motion',
    note: 'Handmade plasticine figures on miniature practical sets. Fingerprints visible.',
    block: [
      'Use a handmade clay stop-motion style. Characters and sets are sculpted from modelling clay with visible fingerprints, seams and tool marks, slightly uneven surfaces and a matte plasticine texture. Build the world as miniature practical sets from felt, cardboard and painted wood, lit with warm soft studio lighting on a tabletop animation stage, with a shallow macro depth of field.',
      '',
      'Everything must read as a physical object photographed on a real set, never as digital 3D or CGI. Keep proportions charming but grounded, and keep faces readable and expressive rather than cute.',
      '',
      'For internal body visuals, build them as clay cutaway models and felt anatomy on the same tabletop set. Keep them tasteful, non-graphic and easy to read.',
    ],
  },
  {
    id: 'goldenage',
    label: '1930s hand-drawn cartoon',
    note: 'Golden-age rubber-hose animation — inked cels, painted backgrounds, film grain.',
    block: [
      'Use a golden-age 1930s hand-drawn theatrical cartoon style: rubber-hose limbs, bold confident ink outlines of varying weight, simple oval-based character construction, large expressive eyes, gloved four-finger hands, flat cel-painted colour fills with minimal shading, and hand-painted watercolour backgrounds with soft vignetting.',
      '',
      'Add the gentle film grain, slight registration wobble and warm, slightly desaturated palette of an early theatrical short. It must look drawn and inked by hand on cels — never 3D, never digital vector art, never modern anime. Keep it lively and comic but never crude or ugly.',
      '',
      'For internal body visuals, draw them as simple hand-inked cutaway diagrams in the same cel style. Keep them tasteful, non-graphic and easy to read.',
    ],
  },
  {
    id: 'photoreal',
    label: 'Photoreal cinematic',
    note: 'Shot on a real camera. Skin texture, physical light, restrained grade.',
    block: [
      'Use a photorealistic cinematic style: real human skin texture with pores and natural imperfections, physically accurate lighting, anamorphic lens character, shallow depth of field, subtle film grain and a restrained colour grade.',
      '',
      'It must read as footage from a well-shot commercial — never as an illustration, a render or an animation. Keep wardrobe, hair and set dressing ordinary and believable.',
      '',
      'For internal body visuals, use a clean photoreal medical-explainer aesthetic. Keep anatomy tasteful, non-graphic and easy to read.',
    ],
  },
];

function designById(id) {
  return DESIGNS.find((d) => d.id === id) || DESIGNS[0];
}

/* ── the image master prompt ─────────────────────────────────────────── */

function buildImagePrompt({ shots = 16, design = 'none', model = 'nano-banana-2',
                            aspect = '9:16', resolution = '1K', format = 'jpg' } = {}) {
  const n = Math.max(1, Math.min(60, Number(shots) || 16));
  const low = Math.max(1, n - 2);
  const high = n + 2;
  const d = designById(design);

  const style = d.block
    ? ['DEFAULT VISUAL STYLE', ''].concat(d.block)
    : ['VISUAL STYLE', '',
       'Take the visual style from the document itself. Read what it says about look, medium, era and tone, and commit to one consistent interpretation of it.',
       '',
       'Whatever you choose, write the full style description into every single prompt in the same words, so all the images match. If the document says nothing about style, choose one that suits the subject and then keep it identical throughout.'];

  return [
    'Analyze the complete attached script and create one batch of image-generation prompts covering the entire story.',
    '',
    'TASK',
    '',
    'Turn the script into ' + n + ' distinct image shots. You may use ' + low + '–' + high +
      ' shots if the script genuinely requires it, but aim for ' + n + '.',
    '',
    'Read the entire document before choosing the shots. Base each shot primarily on the spoken dialogue, voiceover or sung lyrics. Do not blindly copy the document\'s existing frame or on-screen directions. Develop a clear visual story that makes the spoken meaning easy to understand.',
    '',
    'SHOT SELECTION RULES',
    '',
    'Cover every important story beat from the hook through the ending.',
    'Give separate shots to ideas that require meaningfully different visuals.',
    'Combine adjacent lines only when they can be communicated clearly in one image.',
    'Do not create multiple nearly identical shots.',
    'Change the location, camera angle, framing, pose or visual subject when appropriate.',
    'Use character scenes for emotional and relatable moments.',
    'Use clean internal or educational visuals when the script explains something happening inside the body.',
    'Keep chronological aging visually consistent when the script moves through different years.',
    'Do not show the product before the script\'s product reveal.',
    'Include the hook, problem, failed attempts, mechanism, product reveal, product use, benefit mechanism, proof and closing shot when present.',
    'Do not generate a separate image for a plain text disclaimer or a black title card unless a visual is specifically needed.',
    '',
    'CHARACTER CONSISTENCY',
    '',
    'Choose one clear recurring-character design based on the script\'s persona. Establish the character\'s exact age, facial structure, eye colour, hairstyle, hair colour, clothing palette and body proportions in the first character prompt.',
    '',
    'Repeat the same defining character description inside every later prompt featuring that person, because each image is generated independently without an image reference.',
    '',
    'If time passes, preserve the same identity and change only the age-related details. The person must remain obviously recognisable.',
    '',
  ].concat(style).concat([
    '',
    'IMAGE RULES',
    '',
    'Vertical ' + aspect + ' composition.',
    'One clear visual idea per image.',
    'Prefer one or two subjects.',
    'No split screens or collages unless absolutely necessary to communicate the script.',
    'No text, captions, subtitles, numbers, labels, logos, arrows or watermarks generated inside images.',
    'Leave suitable negative space where text will be added later during editing.',
    'Characters must not appear to speak, sing or lip-sync unless I explicitly request it. Keep mouths closed for silent character scenes.',
    'Avoid unnecessary glowing effects, magical particles or abstract symbolism. Use visual effects only when they clearly explain the spoken line.',
    'Do not confuse throat clearing with coughing.',
    'Avoid graphic anatomy, blood, gore, extreme pain or frightening medical imagery.',
    'Do not invent health claims that are not present in the script.',
    '',
    'PRODUCT RULES',
    '',
    'If the script contains a product:',
    '',
    'Do not show it before the intended product reveal.',
    'Keep it naturally integrated into the environment.',
    'The product must have realistic proportions and normal real-world size. Never create an oversized bottle or product, and do not position it extremely close to the camera.',
    'Describe the packaging carefully using information available in the document. Keep labels clean and undistorted.',
    'Do not invent additional branding, ingredients, certifications or claims.',
    'For the closing product image, leave clean negative space for CTA and disclaimer text added later.',
    '',
    'OUTPUT REQUIREMENTS',
    '',
    'Return TWO things, in this order, and nothing else.',
    '',
    'FIRST: one valid JSON array. No explanations, introductions, markdown fences or comments inside it.',
    '',
    'Create one JSON object for every shot using exactly this structure:',
    '',
    '[',
    '  {',
    '    "title": "Short descriptive shot title",',
    '    "model": "' + model + '",',
    '    "input": {',
    '      "prompt": "Complete self-contained image-generation prompt.",',
    '      "aspect_ratio": "' + aspect + '",',
    '      "resolution": "' + resolution + '",',
    '      "output_format": "' + format + '"',
    '    }',
    '  }',
    ']',
    '',
    'SECOND: after the JSON block, a plain list under the heading REFERENCE IMAGES. One line per shot, using its number and title, saying which reference image I should attach to that shot in my app before generating it — the recurring character, a product photo, a location still, or "none".',
    '',
    'In that list, call out:',
    '- any shot where a real person or a real product must not be invented, so it needs a photo I upload rather than anything generated;',
    '- any shot that should reuse an earlier generated image as its reference to keep the look or the face consistent, naming which shot to take it from;',
    '- any shot that needs nothing, written plainly as "none".',
    '',
    'Keep that list outside the JSON. Do not add an image_input field to any object.',
    '',
    'STRICT JSON RULES',
    '',
    'Output ' + n + ' shot objects in chronological order.',
    'Every title must be unique and concise.',
    'Every prompt must be complete and usable on its own.',
    'Repeat the full visual style inside every prompt.',
    'Repeat the full recurring-character description in every applicable prompt.',
    'Do not include an image_input field. I attach reference images myself.',
    'Do not include timing, notes, explanations or any additional metadata fields.',
    'Do not use trailing commas. Use valid double-quoted JSON strings.',
    'Ensure the result can be parsed directly as JSON.',
    'Generate prompts only. Do not generate the actual images.',
    '',
    'Here is the document:',
  ]).join('\n');
}

/* ── the video master prompt ─────────────────────────────────────────── */

function buildVideoPrompt({ shots = 16, model = 'gemini-omni-video',
                            aspect = '9:16', resolution = '1080p',
                            duration = '8' } = {}) {
  const n = Math.max(1, Math.min(60, Number(shots) || 16));
  return [
    'I already have ' + n + ' still images, one per shot, generated from the attached script. Now write one image-to-video prompt for each of those shots.',
    '',
    'I will paste the image shot list (titles and their prompts) together with the script. Read both before you start.',
    '',
    'TASK',
    '',
    'Return exactly one video prompt per image shot, in the same order, reusing each shot\'s title EXACTLY as given and numbering it with "seq" starting at 1. The numbering is how the prompts get matched to my images, so it must be right.',
    '',
    'MOTION RULES',
    '',
    'The still image is the FIRST FRAME of the clip. Describe only what changes from it.',
    'Describe camera movement and subject movement, and nothing else. No re-description of the scene, no restating the art style, no lighting notes unless the light itself changes.',
    'Do not introduce any character, object, location or text that is not already visible in the image.',
    'One continuous shot per clip. No cuts, no scene changes, no transitions, no montage.',
    'Keep the motion small, physical and believable — a slow push in, a gentle handheld drift, a head turn, a breath, steam rising, fabric settling, a hand lifting an object already in frame.',
    'Name the camera move plainly: slow push in, slow pull back, gentle pan left, subtle handheld sway, locked off.',
    'Characters must not speak, sing or lip-sync unless I explicitly ask. Keep mouths closed.',
    'Avoid morphing, warping, extra limbs, sudden identity changes and anything that would break the face.',
    'No text, captions, subtitles, logos or watermarks.',
    '',
    'DURATION',
    '',
    'Choose 4, 6, 8 or 10 seconds per shot based on how much actually happens. A held emotional beat is 4 or 6. A full gesture or a reveal is 8 or 10. Do not pad a simple shot out to 10.',
    '',
    'OUTPUT REQUIREMENTS',
    '',
    'Return TWO things, in this order, and nothing else.',
    '',
    'FIRST: one valid JSON array. No explanations, no markdown fences, no comments inside it.',
    '',
    '[',
    '  {',
    '    "seq": 1,',
    '    "title": "the image shot title, copied exactly",',
    '    "model": "' + model + '",',
    '    "input": {',
    '      "prompt": "Only the motion and the camera move.",',
    '      "duration": "' + duration + '",',
    '      "resolution": "' + resolution + '",',
    '      "aspect_ratio": "' + aspect + '"',
    '    }',
    '  }',
    ']',
    '',
    'SECOND: after the JSON block, a plain list under the heading EXTRA REFERENCES. One line per shot, using its number and title, saying whether that clip needs any extra reference image on top of its own still — a product photo for a close-up, an earlier shot to hold a face steady — or "none".',
    '',
    'Most shots should be "none": the still is already the first frame. Only name an extra reference where it genuinely changes the result, and say what it is for.',
    '',
    'Keep that list outside the JSON. Do not add an image_urls or image_input field to any object.',
    '',
    'STRICT JSON RULES',
    '',
    'Output ' + n + ' objects in chronological order, seq 1 to ' + n + '.',
    'Every title must match an image shot title exactly.',
    'duration must be the string "4", "6", "8" or "10".',
    'Do not include an image_urls or image_input field. The image comes from my app.',
    'Do not include notes, explanations or any additional metadata fields.',
    'Do not use trailing commas. Use valid double-quoted JSON strings.',
    'Ensure the result can be parsed directly as JSON.',
    '',
    'Here is the document:',
  ].join('\n');
}

const EXAMPLE_IMAGE_JSON = [
  '[',
  '  {',
  '    "title": "Kitchen at dawn",',
  '    "model": "nano-banana-2",',
  '    "input": {',
  '      "prompt": "A warm domestic kitchen in soft morning light. A woman around 60 stands at the counter, medium close-up, slightly side-on. Semi-realistic cinematic 3D animated style, shallow depth of field, warm natural light. No text, no captions, no watermark.",',
  '      "aspect_ratio": "9:16",',
  '      "resolution": "1K",',
  '      "output_format": "jpg"',
  '    }',
  '  }',
  ']',
].join('\n');

const EXAMPLE_VIDEO_JSON = [
  '[',
  '  {',
  '    "seq": 1,',
  '    "title": "Kitchen at dawn",',
  '    "model": "gemini-omni-video",',
  '    "input": {',
  '      "prompt": "Slow push in. She breathes out and turns her head a little toward the window; steam drifts up from the mug. Locked, gentle, no other movement.",',
  '      "duration": "8",',
  '      "resolution": "1080p",',
  '      "aspect_ratio": "9:16"',
  '    }',
  '  }',
  ']',
].join('\n');
