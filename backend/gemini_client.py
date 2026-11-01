import os, json
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
client = genai.Client(api_key=os.getenv("LLM_API_KEY"))

MODELS = [os.getenv("LLM_MODEL", "gemini-3.5-flash-lite"),
          "gemini-3.5-flash", "gemini-2.5-flash-lite"]

def ask_gemini(system_prompt, text=None, image_bytes=None,
               image_mime="image/png", pdf_bytes=None):
    parts = []
    if text:
        parts.append(text[:8000])          # cut very long text for speed
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))
    if pdf_bytes:
        parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0,
        response_mime_type="application/json",
    )
    for name in MODELS:
        try:
            r = client.models.generate_content(model=name, contents=parts, config=config)
            return json.loads(r.text)
        except Exception as e:
            print(f"[gemini] {name} failed: {e}")
    return None   # all failed: mark the incident as "unchecked"