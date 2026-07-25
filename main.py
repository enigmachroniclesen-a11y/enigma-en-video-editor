import os
import re
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from moviepy.editor import ImageClip, AudioFileClip, TextClip, CompositeVideoClip

app = FastAPI()

class RenderRequest(BaseModel):
    image_url: str
    audio_url: str
    script_text: str = ""

def get_direct_drive_url(url: str) -> str:
    """Convertit un lien de partage Google Drive en lien de téléchargement direct."""
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url) or re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url

@app.post("/render")
def render_video(data: RenderRequest):
    try:
        # 1. Téléchargement de l'image et de l'audio
        img_direct_url = get_direct_drive_url(data.image_url)
        audio_direct_url = get_direct_drive_url(data.audio_url)

        img_res = requests.get(img_direct_url)
        audio_res = requests.get(audio_direct_url)

        if img_res.status_code != 200 or audio_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Impossible de télécharger l'image ou l'audio.")

        with open("temp_image.jpg", "wb") as f:
            f.write(img_res.content)
        with open("temp_audio.mp3", "wb") as f:
            f.write(audio_res.content)

        # 2. Chargement des clips
        audio_clip = AudioFileClip("temp_audio.mp3")
        duration = audio_clip.duration

        # Format 9:16 vertical (1080x1920) pour Shorts / TikTok
        image_clip = (ImageClip("temp_image.jpg")
                      .set_duration(duration)
                      .resize(height=1920)
                      .set_position("center"))

        clips = [image_clip]

        # 3. Ajout des sous-titres animés au centre (si du texte est fourni)
        if data.script_text:
            words = data.script_text.split()
            chunk_size = 4
            chunks = [' '.join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
            
            if chunks:
                time_per_chunk = duration / len(chunks)
                for idx, chunk in enumerate(chunks):
                    txt_clip = (TextClip(chunk, fontsize=48, color='white', font='Arial-Bold',
                                         method='caption', size=(900, None),
                                         bg_color='black')
                                .set_start(idx * time_per_chunk)
                                .set_duration(time_per_chunk)
                                .set_position(('center', 'center')))
                    clips.append(txt_clip)

        # 4. Assemblage final
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

        # Nettoyage
        audio_clip.close()
        final_video.close()

        return FileResponse(output_filename, media_type="video/mp4", filename="video.mp4")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
