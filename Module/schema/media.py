from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioData:
    file_path: str
    url: Optional[str] = None
    duration: Optional[float] = None
    format: str = "wav"
    transcript: str = ""
    voice_id: str = ""


@dataclass
class VideoData:
    file_path: str
    url: Optional[str] = None
    duration: Optional[float] = None
    format: str = "mp4"
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class ImageData:
    source: str
    detail: str = "auto"
    width: Optional[int] = None
    height: Optional[int] = None
    mime_type: str = "image/png"


@dataclass
class FileData:
    file_name: str
    file_path: str
    file_size: int
    mime_type: str
