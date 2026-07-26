import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

import os
import re
import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip

app = FastAPI()

# --- HEALTH CHECK (Résout le problème 405 / Cold Start sur Render) ---
@app.api_route("/", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "message": "Serveur de montage vidéo opérationnel !"}

# --- MODÈLE DE DONNÉES ---
class RenderRequest(BaseModel):
    image_url: str
    audio_url: str
    script_text: str = ""
    webhook_url: str | None = None  # URL du Webhook Make pour la notification de fin

# --- FONCTIONS UTILITAIRES ---
def create_subtitle_image(text, size=(900, 200)):
    """Génère une image transparente avec le texte des sous-titres centré."""
    img = Image.new('RGBA', size, (0, 0, 0, 160)) # Fond semi-transparent
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    
    x = (size[0] - text_w) / 2
    y = (size[1] - text_h) / 2
    
    draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)
    return np.array(img)

def get_direct_drive_url(url: str) -> str:
    """Convertit un lien de partage Google Drive en lien de téléchargement direct."""
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url) or re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url

# --- TÂCHE DE FOND (Traitement lourd de la vidéo) ---
def process_video_task(data: RenderRequest):
    try:
        # 1. Téléchargement des médias
        img_direct_url = get_direct_drive_url(data.image_url)
        audio_direct_url = get_direct_drive_url(data.audio_url)

        img_res = requests.get(img_direct_url)
        audio_res = requests.get(audio_direct_url)

        if img_res.status_code != 200 or audio_res.status_code != 200:
            raise Exception("Impossible de télécharger l'image ou l'audio.")

        with open("temp_image.jpg", "wb") as f:
            f.write(img_res.content)
        with open("temp_audio.mp3", "wb") as f:
            f.write(audio_res.content)

        # 2. Préparation des éléments
        audio_clip = AudioFileClip("temp_audio.mp3")
        duration = audio_clip.duration

        image_clip = (ImageClip("temp_image.jpg")
                      .set_duration(duration)
                      .resize(height=1920)
                      .set_position("center"))

        clips = [image_clip]

        # 3. Génération des sous-titres
        if data.script_text:
            words = data.script_text.split()
            chunk_size = 4
            chunks = [' '.join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
            
            if chunks:
                time_per_chunk = duration / len(chunks)
                for idx, chunk in enumerate(chunks):
                    sub_arr = create_subtitle_image(chunk)
                    sub_clip = (ImageClip(sub_arr)
                                .set_start(idx * time_per_chunk)
                                .set_duration(time_per_chunk)
                                .set_position(('center', 1400))) # Placés vers le bas
                    clips.append(sub_clip)

        # 4. Rendu final
        final_video = CompositeVideoClip(clips, size=(1080, 1920)).set_audio(audio_clip)
        output_filename = "output.mp4"
        final_video.write_videofile(
            output_filename,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile="temp-audio.m4a",
            remove_temp=True
        )

        audio_clip.close()
        final_video.close()

        # 5. Envoi de la confirmation à Make via Webhook
        if data.webhook_url:
            requests.post(data.webhook_url, json={
                "status": "success",
                "message": "Rendu vidéo terminé !",
                "filename": output_filename
            })

    except Exception as e:
        # Envoi de l'erreur à Make si le traitement échoue
        if data.webhook_url:
            requests.post(data.webhook_url, json={
                "status": "error",
                "error": str(e)
            })

# --- ENDPOINT RENDU (Réponse instantanée < 1s) ---
@app.post("/render")
def render_video(data: RenderRequest, background_tasks: BackgroundTasks):
    # Lance la vidéo en arrière-plan
    background_tasks.add_task(process_video_task, data)
    
    # Retourne immédiatement pour éviter le timeout (502 Bad Gateway)
    return {
        "status": "processing",
        "message": "Le rendu de la vidéo a été lancé en arrière-plan."
    }

# --- ROUTE DE TÉLÉCHARMENT DE LA VIDÉO ---
@app.get("/download")
def download_video():
    output_filename = "output.mp4"
    if os.path.exists(output_filename):
        return FileResponse(output_filename, media_type="video/mp4", filename="video.mp4")
    raise HTTPException(status_code=404, detail="Vidéo non trouvée.")
