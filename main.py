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
from moviepy.editor import ImageClip, AudioFileClip, CompositeVideoClip, CompositeAudioClip, concatenate_videoclips

app = FastAPI()

# --- HEALTH CHECK ---
@app.api_route("/", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "message": "Serveur de montage vidéo opérationnel !"}

# --- MODÈLE DE DONNÉES ---
class RenderRequest(BaseModel):
    image_urls: list[str] = [] # Accepte une liste de 3 images
    image_url: str | None = None # Rétrocompatibilité si 1 seule image est envoyée
    audio_url: str
    script_text: str = ""
    webhook_url: str | None = None

# --- FONCTIONS UTILITAIRES ---
def create_subtitle_image(text, size=(700, 150)):
    """Génère une image transparente avec le texte des sous-titres, ajusté pour mobile."""
    text = text.upper()
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = None
    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", # Linux / Render
        "/System/Library/Fonts/HelveticaNeue.ttc",              # MacOS
        "arial.ttf"                                            # Windows
    ]

    for f_path in possible_fonts:
        try:
            font = ImageFont.truetype(f_path, size=40)
            break
        except OSError:
            continue

    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (size[0] - text_w) / 2
    y = (size[1] - text_h) / 2

    stroke_w = 2
    for adj_x in range(-stroke_w, stroke_w + 1):
        for adj_y in range(-stroke_w, stroke_w + 1):
            draw.text((x + adj_x, y + adj_y), text, fill=(0, 0, 0, 255), font=font)

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
    bg_music = None
    try:
        print("=== DEBUT DU TRAITEMENT VIDEO ===", flush=True)

        # 1. Gestion de la liste des images
        urls_to_download = data.image_urls
        if not urls_to_download and data.image_url:
            urls_to_download = [data.image_url]

        # 2. Téléchargement des voix et musiques
        print("Téléchargement de l'audio de voix off...", flush=True)
        download_file_from_google_drive(data.audio_url, "temp_audio.mp3")

        voice_clip = AudioFileClip("temp_audio.mp3")
        total_duration = voice_clip.duration

        # 3. Traitement et optimisation des images
        num_images = len(urls_to_download)
        duration_per_image = total_duration / num_images if num_images > 0 else total_duration

        image_clips = []
        for i, url in enumerate(urls_to_download):
            raw_path = f"temp_img_raw_{i}.jpg"
            opt_path = f"temp_img_{i}.jpg"

            print(f"Téléchargement de l'image {i+1}/{num_images}...", flush=True)
            download_file_from_google_drive(url, raw_path)

            with Image.open(raw_path) as img:
                img = img.convert("RGB")
                img.thumbnail((720, 1280))
                img.save(opt_path, "JPEG", quality=85)

            clip = (ImageClip(opt_path)
                    .set_duration(duration_per_image)
                    .resize(height=1280)
                    .set_position("center"))
            image_clips.append(clip)

        # Concaténation des images
        background_video = concatenate_videoclips(image_clips, method="compose")
        clips = [background_video]

        # 4. Traitement de la musique d'ambiance (format .wav recommandé)
        music_file = "dark_ambient.wav"
        if not os.path.exists(music_file):
            music_file = "dark_ambient.mp3"  # Fallback si le wav n'a pas encore été mis à jour

        final_audio = voice_clip

        if os.path.exists(music_file):
            try:
                print(f"Ajout de la musique d'ambiance ({music_file})...", flush=True)
                bg_music = AudioFileClip(music_file).volumex(0.12)  # Volume réduit à 12%

                if bg_music.duration < total_duration:
                    bg_music = bg_music.loop(duration=total_duration)
                else:
                    bg_music = bg_music.subclip(0, total_duration)

                bg_music = bg_music.audio_fadeout(2)
                final_audio = CompositeAudioClip([voice_clip, bg_music])
            except Exception as audio_err:
                print(f"Avertissement : Erreur avec la musique d'ambiance ({audio_err}). Traitement avec la voix seule.", flush=True)
        else:
            print("Musique d'ambiance non trouvée, conservation de la voix off seule.", flush=True)

        # 5. Génération des sous-titres grand format
        if data.script_text:
            print("Génération des sous-titres...", flush=True)
            words = data.script_text.split()
            chunk_size = 2
            chunks = [' '.join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]

            if chunks:
                time_per_chunk = total_duration / len(chunks)
                for idx, chunk in enumerate(chunks):
                    sub_arr = create_subtitle_image(chunk, size=(700, 150))
                    sub_clip = (ImageClip(sub_arr)
                                .set_start(idx * time_per_chunk)
                                .set_duration(time_per_chunk)
                                .set_position(('center', 950)))
                    clips.append(sub_clip)

        # 6. Rendu final optimisé pour la RAM sur Render
        print("Rendu final avec MoviePy...", flush=True)
        final_video = CompositeVideoClip(clips, size=(720, 1280)).set_audio(final_audio)
        output_filename = "output.mp4"

        final_video.write_videofile(
            output_filename,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            bitrate="2000k",
            ffmpeg_params=["-crf", "28", "-pix_fmt", "yuv420p"],
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            threads=1
        )

        # Nettoyage des objets en mémoire
        voice_clip.close()
        if bg_music:
            bg_music.close()
        final_video.close()

        print("=== RENDU TERMINE AVEC SUCCES ===", flush=True)

        # 7. Webhook retour
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
