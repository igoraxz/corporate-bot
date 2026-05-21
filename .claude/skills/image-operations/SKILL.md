---
description: "AI image generation and editing for corporate visual assets. TRIGGER when: user asks to draw/create/generate an image or picture, make an illustration, edit/modify/retouch a photo, change image style/background, remove objects from photo, apply filters or effects to an image, create corporate visual content. Also trigger for generate_image or edit_image tool usage."
---

# Corporate Image Generation & Editing

Two tools are available for working with images:

## 1. `generate_image` — Create a NEW image from a text description (Imagen 4.0)
- Best for: illustrations, concept art, scenes, objects
- Cannot include real people (person generation disabled)
- Prompt must be in English (translate if user writes in Russian)
- Returns a file path to the generated PNG

## 2. `edit_image` — Edit an EXISTING image using Gemini AI
- Best for: style changes, adding/removing objects, background swap, color correction, retouching
- Takes an image file path + text instructions describing the edit
- By default uses Flash model (fast). Set use_pro=true for complex edits requiring higher quality.
- The user's photo must already be downloaded (from TG/WA media or a previous download)

## Workflow — Generation
1. Craft a detailed English prompt (expand user's brief description)
2. Call `generate_image` with the prompt and a descriptive filename
3. Send the result via `telegram_send_photo`

## Workflow — Editing
1. Ensure you have the source image file path (from media attachment or previous download)
2. Call `edit_image` with image_path, editing instructions, and output filename
3. Send the result via `telegram_send_photo`

## Tips
- For generation: be specific about style, composition, lighting, colors
- For editing: describe what to change, not the whole image ("remove the background", "make it look like a watercolor painting")
- If user sends a photo and asks to modify it → use `edit_image`
- If user asks to "draw" or "create" something from scratch → use `generate_image`
