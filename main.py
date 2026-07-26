import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

import os
import re
import requests
import traceback
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip

app = FastAPI()

# --- HEALTH CHECK ---
@app.api_route("/", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "message": "Serveur de montage vidéo opérationnel !"}

# --- MODÈLE DE DONNÉES ---
class RenderRequest(BaseModel):
    image_url: str
    audio_url: str
    script_text: str = ""
    webhook_url: str | None = None

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

def download_file_from_google_drive(url: str, destination: str):
    """Télécharge un fichier Google Drive en gérant les fichiers volumineux et confirmations."""
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url) or re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if not match:
        # Si ce n'est pas un lien Google Drive, téléchargement direct simple
        res = requests.get(url, stream=True)
        res.raise_for_status()
        with open(destination, "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)
        return

    file_id = match.group(1)
    session = requests.Session()
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    
    response = session.get(download_url, stream=True)
    
    # Gestion du token de confirmation Google Drive pour les gros fichiers
    token = None
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            token = value
            break

    if token:
        download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
        response = session.get(download_url, stream=True)

    response.raise_for_status()
    with open(destination, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

# --- TÂCHE DE FOND ---
def process_video_task(data: RenderRequest, host_url: str):
    try:
        print("=== DEBUT DU TRAITEMENT VIDEO ===", flush=True)

        # 1. Téléchargement sécurisé des médias
        print("Téléchargement de l'image...", flush=True)
        download_file_from_google_drive(data.image_url, "temp_image.jpg")
        
        print("Téléchargement de l'audio...", flush=True)
        download_file_from_google_drive(data.audio_url, "temp_audio.mp3")

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
            print("Génération des sous-titres...", flush=True)
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
                                .set_position(('center', 1400)))
                    clips.append(sub_clip)

        # 4. Rendu final
        print("Rendu final avec MoviePy...", flush=True)
        final_video = CompositeVideoClip(clips, size=(1080, 1920)).set_audio(audio_clip)
        output_filename = "output.mp4"
        
        final_video.write_videofile(
            output_filename,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            threads=1  # Limite l'usage CPU/RAM pour éviter les surcharges
        )

        audio_clip.close()
        final_video.close()

        print("=== RENDU TERMINE AVEC SUCCES ===", flush=True)

        # 5. Envoi du lien vers Make via Webhook
        if data.webhook_url:
            download_link = f"{host_url.rstrip('/')}/download"
            requests.post(data.webhook_url, json={
                "status": "success",
                "message": "Rendu vidéo terminé !",
                "download_url": download_link,
                "filename": output_filename
            })

    except Exception as e:
        error_msg = str(e)
        print(f"!!! ERREUR RENDU VIDEO !!! : {error_msg}", flush=True)
        traceback.print_exc()

        # Envoi de l'erreur détaillée à Make
        if data.webhook_url:
            requests.post(data.webhook_url, json={
                "status": "error",
                "error": error_msg
            })

# --- ENDPOINT RENDU ---
@app.post("/render")
def render_video(data: RenderRequest, request: Request, background_tasks: BackgroundTasks):
    # Récupère l'URL de base du serveur hébergé
    host_url = str(request.base_url)
    
    background_tasks.add_task(process_video_task, data, host_url)
    
    return {
        "status": "processing",
        "message": "Le rendu de la vidéo a été lancé en arrière-plan."
    }

# --- ROUTE DE TÉLÉCHARGEMENT ---
@app.get("/download")
def download_video():
    output_filename = "output.mp4"
    if os.path.exists(output_filename):
        return FileResponse(output_filename, media_type="video/mp4", filename="video.mp4")
    raise HTTPException(status_code=404, detail="Vidéo non trouvée.")
