"""One narration selection contract for server planning and the render worker."""
from pathlib import Path
import re

ENGINE_DIRS={'edge':'_FINAL_AUDIO_EDGETTS','voicer':'_FINAL_AUDIO_VOICER','voicegencore':'_FINAL_AUDIO_VGC','tts':'_FINAL_AUDIO_ELEVENLABS','lumean':'_FINAL_AUDIO_LUMEAN'}
EXTENSIONS={'.mp3','.wav','.m4a','.aac','.ogg','.flac'}
EXCLUDED={'_ASSETS','_RENDER_MEDIA','.remote_jobs','chapters','render','__pycache__'}

def select_narration(project_dir, config=None):
    root=Path(project_dir).resolve();config=config or {}
    def valid(path):
        resolved=path.resolve()
        if root not in resolved.parents or resolved.suffix.lower() not in EXTENSIONS or not resolved.is_file() or resolved.stat().st_size<=0:
            raise ValueError('Файл основной озвучки отсутствует, пуст или находится вне проекта')
        return resolved
    if 'narration_files' in config:
        names=config['narration_files']
        if not isinstance(names,list) or not names or any(not isinstance(x,str) or Path(x).is_absolute() or '..' in Path(x).parts for x in names) or len(set(names))!=len(names):
            raise ValueError('Некорректный закреплённый список основной озвучки')
        return [str(valid(root/x).relative_to(root)) for x in names]
    def scan(base):
        selected=[]
        for path in base.rglob('*'):
            parts=path.relative_to(root).parts
            if any(part in EXCLUDED or part.startswith('.') or 'temp' in part.lower() or 'tmp' in part.lower() for part in parts):continue
            if 'final_video' in path.name.lower() or path.suffix.lower() not in EXTENSIONS:continue
            if path.is_file() and path.stat().st_size>0:selected.append(valid(path))
        return selected
    folders=[p for p in root.glob('_FINAL_AUDIO_*') if p.is_dir() and scan(p)]
    engine=str(config.get('engine') or config.get('tts_engine') or '').strip().lower()
    if folders:
        if engine:
            chosen=root/ENGINE_DIRS.get(engine,'__unknown_audio_engine__')
            if chosen not in folders:raise ValueError('Нет озвучки выбранного движка; завершите её генерацию перед рендером')
        elif len(folders)==1:chosen=folders[0]
        else:raise ValueError('Несколько вариантов озвучки; выберите движок перед рендером')
        paths=scan(chosen)
    else:
        paths=scan(root)
        excluded={Path(x).resolve() if Path(x).is_absolute() else (root/x).resolve() for x in (config.get('background_audio_file'),config.get('end_track_file')) if x}
        paths=[p for p in paths if p not in excluded]
    def order(path):
        values=re.findall(r'\d+',path.name)
        return (int(values[0]) if values else 0,str(path.relative_to(root)))
    paths.sort(key=order)
    return [str(p.relative_to(root)) for p in paths]
