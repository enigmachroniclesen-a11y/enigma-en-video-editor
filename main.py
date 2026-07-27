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
def create_subtitle_image(text, size=(700, 150)):
    """Génère une image transparente avec le texte des sous-titres, ajusté pour mobile."""
    
    # Force le texte en majuscules pour une meilleure lisibilité mobile
    text = text.upper()
    
    # Zone transparente
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Chargement d'une police grasse pour un meilleur rendu
    font = None
    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", # Linux / Render
        "/System/Library/Fonts/HelveticaNeue.ttc",              # MacOS
        "arial.ttf"                                            # Windows
    ]
    
    for f_path in possible_fonts:
        try:
            # Taille réduite à 40 pour éviter les débordements
            font = ImageFont.truetype(f_path, size=40)
            break
        except OSError:
            continue
            
    if font is None:
        font = ImageFont.load_default()

    # Centrage du texte dans la zone
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    
    x = (size[0] - text_w) / 2
    y = (size[1] - text_h) / 2
    
    # Contour noir pour assurer la lisibilité
    stroke_w = 2
    for adj_x in range(-stroke_w, stroke_w + 1):
        for adj_y in range(-stroke_w, stroke_w + 1):
            draw.text((x + adj_x, y + adj_y), text, fill=(0, 0, 0, 255), font=font)
    
    # Texte principal en Jaune (Or)
    draw.text((x, y), text, fill=(255, 215, 0, 255), font=font)
    
    return np.array(img)

def download_file_from_google_drive(url: str, destination: str):
    """Télécharge un fichier Google Drive en gérant les fichiers volumineux et confirmations."""
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url) or re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if not match:
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

        # 1. Téléchargement des médias
        print("Téléchargement de l'image...", flush=True)
        download_file_from_google_drive(data.image_url, "temp_image_raw.jpg")
        
        print("Téléchargement de l'audio...", flush=True)
        download_file_from_google_drive(data.audio_url, "temp_audio.mp3")

        # 2. Redimensionnement préventif pour préserver la RAM
        print("Optimisation de l'image pour la RAM...", flush=True)
        with Image.open("temp_image_raw.jpg") as img:
            img = img.convert("RGB")
            img.thumbnail((720, 1280))
            img.save("temp_image.jpg", "JPEG", quality=85)

        # 3. Préparation des éléments
        audio_clip = AudioFileClip("temp_audio.mp3")
        duration = audio_clip.duration

        image_clip = (ImageClip("temp_image.jpg")
                      .set_duration(duration)
                      .resize(height=1280)
                      .set_position("center"))

        clips = [image_clip]

        # 4. Génération des sous-titres grand format
        if data.script_text:
            print("Génération des sous-titres grand format...", flush=True)
            words = data.script_text.split()
            chunk_size = 2 # Affichage de 2 mots max par groupe
            chunks = [' '.join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
            
            if chunks:
                time_per_chunk = duration / len(chunks)
                for idx, chunk in enumerate(chunks):
                    sub_arr = create_subtitle_image(chunk, size=(700, 150))
                    sub_clip = (ImageClip(sub_arr)
                                .set_start(idx * time_per_chunk)
                                .set_duration(time_per_chunk)
                                .set_position(('center', 950)))
                    clips.append(sub_clip)

        # 5. Rendu final (720x1280, optimisé 512 MB RAM)
        print("Rendu final avec MoviePy...", flush=True)
        final_video = CompositeVideoClip(clips, size=(720, 1280)).set_audio(audio_clip)
        output_filename = "output.mp4"
        
        final_video.write_videofile(
            output_filename,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            threads=1
        )

        audio_clip.close()
        final_video.close()

        print("=== RENDU TERMINE AVEC SUCCES ===", flush=True)

        # 6. Webhook retour
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

        if data.webhook_url:
            requests.post(data.webhook_url, json={
                "status": "error",
                "error": error_msg
            })

# --- ENDPOINT RENDU ---
@app.post("/render")
def render_video(data: RenderRequest, request: Request, background_tasks: BackgroundTasks):
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
