"""File type detection from content and extensions.

Detects file types using magic bytes and file extensions to determine
the appropriate viewer/editor in the Browse and Edit tabs.
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class FileTypeInfo:
    """Detected file type information."""
    category: str       # 'image', 'audio', 'video', 'text', 'font', 'binary', 'archive'
    mime_type: str       # MIME type string
    description: str     # Human-readable description
    extension: str       # Normalized extension (lowercase, with dot)
    can_preview: bool    # Whether the file can be previewed in Browse tab
    can_edit: bool       # Whether the file can be edited in Edit tab


EXTENSION_MAP = {
    ".png":   FileTypeInfo("image", "image/png", "PNG Image", ".png", True, False),
    ".jpg":   FileTypeInfo("image", "image/jpeg", "JPEG Image", ".jpg", True, False),
    ".jpeg":  FileTypeInfo("image", "image/jpeg", "JPEG Image", ".jpeg", True, False),
    ".bmp":   FileTypeInfo("image", "image/bmp", "BMP Image", ".bmp", True, False),
    ".tga":   FileTypeInfo("image", "image/x-tga", "TGA Image", ".tga", True, False),
    ".dds":   FileTypeInfo("image", "image/vnd-ms.dds", "DDS Texture", ".dds", True, False),
    ".webp":  FileTypeInfo("image", "image/webp", "WebP Image", ".webp", True, False),
    ".gif":   FileTypeInfo("image", "image/gif", "GIF Image", ".gif", True, False),
    ".wav":   FileTypeInfo("audio", "audio/wav", "WAV Audio", ".wav", True, False),
    ".ogg":   FileTypeInfo("audio", "audio/ogg", "OGG Audio", ".ogg", True, False),
    ".mp3":   FileTypeInfo("audio", "audio/mpeg", "MP3 Audio", ".mp3", True, False),
    ".wem":   FileTypeInfo("audio", "audio/x-wem", "Wwise Audio (WEM)", ".wem", True, False),
    ".bnk":   FileTypeInfo("audio", "audio/x-bnk", "Wwise SoundBank (BNK)", ".bnk", True, False),
    ".pasound": FileTypeInfo("audio", "audio/x-pasound", "PA Sound Config", ".pasound", True, False),
    ".flac":  FileTypeInfo("audio", "audio/flac", "FLAC Audio", ".flac", True, False),
    ".aac":   FileTypeInfo("audio", "audio/aac", "AAC Audio", ".aac", True, False),
    ".mp4":   FileTypeInfo("video", "video/mp4", "MP4 Video", ".mp4", True, False),
    ".webm":  FileTypeInfo("video", "video/webm", "WebM Video", ".webm", True, False),
    ".avi":   FileTypeInfo("video", "video/x-msvideo", "AVI Video", ".avi", True, False),
    ".mkv":   FileTypeInfo("video", "video/x-matroska", "MKV Video", ".mkv", True, False),
    ".bk2":   FileTypeInfo("video", "video/x-bink2", "Bink2 Video", ".bk2", True, False),
    ".bik":   FileTypeInfo("video", "video/x-bink", "Bink Video", ".bik", True, False),
    ".usm":   FileTypeInfo("video", "video/x-usm", "CriWare USM Video", ".usm", True, False),
    ".css":   FileTypeInfo("text", "text/css", "CSS Stylesheet", ".css", True, True),
    ".html":  FileTypeInfo("text", "text/html", "HTML Document", ".html", True, True),
    ".thtml": FileTypeInfo("text", "text/html", "Template HTML", ".thtml", True, True),
    ".xml":   FileTypeInfo("text", "application/xml", "XML Document", ".xml", True, True),
    ".json":  FileTypeInfo("text", "application/json", "JSON Data", ".json", True, True),
    ".txt":   FileTypeInfo("text", "text/plain", "Text File", ".txt", True, True),
    ".csv":   FileTypeInfo("text", "text/csv", "CSV Data", ".csv", True, True),
    ".paloc": FileTypeInfo("text", "application/x-paloc", "Localization File", ".paloc", True, True),
    ".ttf":   FileTypeInfo("font", "font/ttf", "TrueType Font", ".ttf", True, False),
    ".otf":   FileTypeInfo("font", "font/otf", "OpenType Font", ".otf", True, False),
    ".woff":  FileTypeInfo("font", "font/woff", "WOFF Font", ".woff", True, False),
    ".woff2": FileTypeInfo("font", "font/woff2", "WOFF2 Font", ".woff2", True, False),
    ".paz":   FileTypeInfo("archive", "application/x-paz", "PAZ Archive", ".paz", False, False),
    ".pamt":  FileTypeInfo("archive", "application/x-pamt", "PAMT Index", ".pamt", False, False),
    ".papgt": FileTypeInfo("archive", "application/x-papgt", "PAPGT Root Index", ".papgt", False, False),
    # 3D Mesh formats
    ".pam":     FileTypeInfo("mesh", "model/x-pam", "PAM Static Mesh", ".pam", True, False),
    ".pamlod":  FileTypeInfo("mesh", "model/x-pamlod", "PAM LOD Mesh", ".pamlod", True, False),
    ".pac":     FileTypeInfo("mesh", "model/x-pac", "PAC Skinned Mesh", ".pac", True, False),
    ".pami":    FileTypeInfo("text", "application/xml", "Mesh Instance XML (encrypted)", ".pami", True, True),
    ".meshinfo": FileTypeInfo("binary", "application/x-meshinfo", "Mesh Info", ".meshinfo", False, False),
    # Havok Physics / Animation
    ".hkx":     FileTypeInfo("binary", "application/x-havok", "Havok Physics/Skeleton", ".hkx", False, False),
    ".paa":     FileTypeInfo("binary", "application/x-paa", "PA Animation", ".paa", False, False),
    ".paa_metabin": FileTypeInfo("binary", "application/x-paa-meta", "Animation Metadata", ".paa_metabin", False, False),
    # Game data
    ".pabgb":   FileTypeInfo("binary", "application/x-pabgb", "Game Data Table", ".pabgb", True, False),
    ".pae":     FileTypeInfo("binary", "application/x-pae", "PA Effect", ".pae", False, False),
    ".prefab":  FileTypeInfo("binary", "application/x-prefab", "Prefab Asset", ".prefab", True, False),
    ".pampg":   FileTypeInfo("binary", "application/x-pampg", "PAM Page Data", ".pampg", False, False),
    ".paseq":   FileTypeInfo("binary", "application/x-paseq", "PA Sequencer", ".paseq", False, False),
    ".paseqc":  FileTypeInfo("binary", "application/x-paseqc", "PA Sequencer Config", ".paseqc", False, False),
    ".pastage": FileTypeInfo("binary", "application/x-pastage", "PA Stage Data", ".pastage", False, False),
    ".paschedule": FileTypeInfo("binary", "application/x-paschedule", "PA Schedule", ".paschedule", False, False),
    ".paschedulepath": FileTypeInfo("binary", "application/x-paschedulepath", "PA Schedule Path", ".paschedulepath", False, False),
    ".palevel": FileTypeInfo("binary", "application/x-palevel", "PA Level Data", ".palevel", False, False),
    ".levelinfo": FileTypeInfo("binary", "application/x-levelinfo", "Level Info", ".levelinfo", False, False),
    ".padxil":  FileTypeInfo("binary", "application/x-padxil", "Shader Cache", ".padxil", False, False),
    ".imp":     FileTypeInfo("binary", "application/x-imp", "Import Definition", ".imp", False, False),
    ".pat":     FileTypeInfo("binary", "application/x-pat", "PA Terrain", ".pat", False, False),
    ".paccd":   FileTypeInfo("binary", "application/x-paccd", "PAC Character Data", ".paccd", False, False),
    ".binarygimmick": FileTypeInfo("binary", "application/x-gimmick", "Gimmick Data", ".binarygimmick", False, False),
    ".roadsector": FileTypeInfo("binary", "application/x-road", "Road Sector", ".roadsector", False, False),
    ".road":    FileTypeInfo("binary", "application/x-road", "Road Data", ".road", False, False),
    ".motionblending": FileTypeInfo("binary", "application/x-motionblend", "Motion Blending", ".motionblending", False, False),
    # Other
    ".paver":   FileTypeInfo("binary", "application/x-paver", "Game Version", ".paver", False, False),
    ".pathc":   FileTypeInfo("binary", "application/x-pathc", "Patch Cache", ".pathc", False, False),
    # Encrypted XML formats
    ".spline2d": FileTypeInfo("text", "application/xml", "2D Spline (encrypted XML)", ".spline2d", True, True),
    ".spline":  FileTypeInfo("text", "application/xml", "Spline Path (encrypted XML)", ".spline", True, True),
    ".mi":      FileTypeInfo("text", "application/xml", "Material Instance (encrypted XML)", ".mi", True, True),
    # Physics / Simulation
    ".pbd":     FileTypeInfo("binary", "application/x-pbd", "Physics Body Data", ".pbd", False, False),
    ".paem":    FileTypeInfo("binary", "application/x-paem", "Particle Emitter", ".paem", False, False),
    ".paac":    FileTypeInfo("binary", "application/x-paac", "Action Chart", ".paac", False, False),
    ".pabc":    FileTypeInfo("binary", "application/x-pabc", "Bone Controller", ".pabc", False, False),
    ".pab":     FileTypeInfo("binary", "application/x-pab", "Skeleton / Bones", ".pab", False, False),
    ".paatt":   FileTypeInfo("binary", "application/x-paatt", "Attachment Data", ".paatt", False, False),
    # Level / World
    ".palevel": FileTypeInfo("binary", "application/x-palevel", "Level Data", ".palevel", False, False),
    ".levelinfo": FileTypeInfo("binary", "application/x-levelinfo", "Level Info", ".levelinfo", False, False),
    ".nav":     FileTypeInfo("binary", "application/x-nav", "Navigation Mesh", ".nav", False, False),
    ".roadsector": FileTypeInfo("binary", "application/x-road", "Road Sector", ".roadsector", False, False),
    ".road":    FileTypeInfo("binary", "application/x-road", "Road Data", ".road", False, False),
    ".roadidx": FileTypeInfo("binary", "application/x-road", "Road Index", ".roadidx", False, False),
    # Sequencer / Animation
    ".paseq":   FileTypeInfo("binary", "application/x-paseq", "Sequencer Data", ".paseq", False, False),
    ".paseqc":  FileTypeInfo("binary", "application/x-paseqc", "Sequencer Config", ".paseqc", False, False),
    ".paseqh":  FileTypeInfo("binary", "application/x-paseqh", "Sequencer Header", ".paseqh", False, False),
    ".seqmt":   FileTypeInfo("binary", "application/x-seqmt", "Sequencer Metadata", ".seqmt", False, False),
    ".pastage": FileTypeInfo("text", "text/plain", "Stage Data", ".pastage", True, True),
    ".paschedule": FileTypeInfo("binary", "application/x-paschedule", "Schedule Data", ".paschedule", False, False),
    ".paschedulepath": FileTypeInfo("binary", "application/x-paschedulepath", "Schedule Path", ".paschedulepath", False, False),
    ".paschedulectx": FileTypeInfo("binary", "application/x-paschedulectx", "Schedule Context", ".paschedulectx", False, False),
    ".uianiminit": FileTypeInfo("binary", "application/x-uianiminit", "UI Animation Init (encrypted)", ".uianiminit", False, False),
    ".motionblending": FileTypeInfo("binary", "application/x-motionblend", "Motion Blending", ".motionblending", False, False),
    # Game Data
    ".pabgb":   FileTypeInfo("text", "application/xml", "Game Data Binary", ".pabgb", True, False),
    ".pabgh":   FileTypeInfo("binary", "application/x-pabgh", "Game Data Header", ".pabgh", False, False),
    ".pabv":    FileTypeInfo("binary", "application/x-pabv", "Game Data Version", ".pabv", False, False),
    ".binarygimmick": FileTypeInfo("binary", "application/x-gimmick", "Gimmick Data", ".binarygimmick", False, False),
    ".binarygimmickcacheddata": FileTypeInfo("binary", "application/x-gimmick", "Gimmick Cache", ".binarygimmickcacheddata", False, False),
    ".binarygimmickframeevent": FileTypeInfo("binary", "application/x-gimmick", "Gimmick Frame Events", ".binarygimmickframeevent", False, False),
    ".binarystring": FileTypeInfo("binary", "application/x-binarystring", "Binary String Table", ".binarystring", False, False),
    # Rendering / Shaders
    ".padxil":  FileTypeInfo("binary", "application/x-padxil", "Shader Cache (DXIL)", ".padxil", False, False),
    ".technique": FileTypeInfo("binary", "application/x-technique", "Render Technique", ".technique", False, False),
    ".material": FileTypeInfo("binary", "application/x-material", "Material Definition", ".material", False, False),
    ".impostor": FileTypeInfo("binary", "application/x-impostor", "Impostor LOD", ".impostor", False, False),
    ".ies":     FileTypeInfo("binary", "application/x-ies", "Light Profile (IES)", ".ies", False, False),
    # Mesh related
    ".meshinfo": FileTypeInfo("binary", "application/x-meshinfo", "Mesh Info", ".meshinfo", False, False),
    ".pampg":   FileTypeInfo("binary", "application/x-pampg", "PAM Page Data", ".pampg", False, False),
    ".parg":    FileTypeInfo("binary", "application/x-parg", "PAR Group Data", ".parg", False, False),
    ".pasg":    FileTypeInfo("binary", "application/x-pasg", "PAS Group Data", ".pasg", False, False),
    ".pcg":     FileTypeInfo("binary", "application/x-pcg", "Procedural Content", ".pcg", False, False),
    ".pashv":   FileTypeInfo("binary", "application/x-pashv", "Shadow Volume", ".pashv", False, False),
    ".paccd":   FileTypeInfo("binary", "application/x-paccd", "Character Data", ".paccd", False, False),
    ".pat":    FileTypeInfo("binary", "application/x-pat", "Terrain Data", ".pat", False, False),
    ".imp":     FileTypeInfo("binary", "application/x-imp", "Import Definition", ".imp", False, False),
    # Project / Save
    ".prefab":  FileTypeInfo("binary", "application/x-prefab", "Prefab Asset", ".prefab", True, False),
    ".save":    FileTypeInfo("binary", "application/x-save", "Save Data", ".save", False, False),
    ".paproj":  FileTypeInfo("text", "application/xml", "Project File (XML)", ".paproj", True, True),
    ".paprojdesc": FileTypeInfo("text", "text/plain", "Project Description", ".paprojdesc", True, True),
    ".papr":    FileTypeInfo("binary", "application/x-papr", "Project Resource", ".papr", False, False),
    ".pappt":   FileTypeInfo("binary", "application/x-pappt", "Project Template", ".pappt", False, False),
    # Audio
    ".mp4":     FileTypeInfo("video", "video/mp4", "MP4 Video", ".mp4", True, False),
    ".pas":     FileTypeInfo("binary", "application/x-pas", "PA Sound Config", ".pas", False, False),
    ".pai":     FileTypeInfo("binary", "application/x-pai", "PA Audio Info", ".pai", False, False),
    # Other
    ".dat":     FileTypeInfo("binary", "application/octet-stream", "Data File", ".dat", False, False),
    ".ani":     FileTypeInfo("binary", "application/x-ani", "Cursor Animation", ".ani", False, False),
    ".cur":     FileTypeInfo("binary", "application/x-cur", "Cursor File", ".cur", False, False),
    ".linkedsceneobject": FileTypeInfo("binary", "application/x-lso", "Linked Scene Object", ".linkedsceneobject", False, False),
    ".questgaugecount": FileTypeInfo("binary", "application/x-questgauge", "Quest Gauge Count", ".questgaugecount", False, False),
    ".pma":     FileTypeInfo("binary", "application/x-pma", "PA Material Archive", ".pma", False, False),
    ".paacdesc": FileTypeInfo("binary", "application/x-paacdesc", "Action Chart Description", ".paacdesc", False, False),
    ".paasmt":  FileTypeInfo("binary", "application/x-paasmt", "Animation State Machine", ".paasmt", False, False),
    ".pamhc":   FileTypeInfo("binary", "application/x-pamhc", "Mesh Hash Cache", ".pamhc", False, False),
}

MAGIC_BYTES = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"BM": ".bmp",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".wav",
    b"OggS": ".ogg",
    b"\xff\xfb": ".mp3",
    b"\xff\xf3": ".mp3",
    b"\xff\xf2": ".mp3",
    b"ID3": ".mp3",
    b"DDS ": ".dds",
    b"fLaC": ".flac",
    b"BIKi": ".bk2",
    b"BIKh": ".bk2",
    b"CRID": ".usm",
    b"\x00\x00\x01\x00": ".ttf",
    b"\x00\x01\x00\x00": ".ttf",
    b"OTTO": ".otf",
    b"wOFF": ".woff",
    b"wOF2": ".woff2",
    b"<?xml": ".xml",
    b"<html": ".html",
    b"<!DOCTYPE": ".html",
}


def detect_file_type(path: str, data: Optional[bytes] = None) -> FileTypeInfo:
    """Detect file type from extension and optionally magic bytes.

    Args:
        path: File path (used for extension matching).
        data: Optional file content for magic byte detection.

    Returns:
        FileTypeInfo describing the detected file type.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in EXTENSION_MAP:
        return EXTENSION_MAP[ext]

    if data and len(data) >= 8:
        for magic, magic_ext in MAGIC_BYTES.items():
            if data[:len(magic)] == magic:
                if magic_ext in EXTENSION_MAP:
                    return EXTENSION_MAP[magic_ext]

    return FileTypeInfo(
        category="binary",
        mime_type="application/octet-stream",
        description="Binary File",
        extension=ext or ".bin",
        can_preview=True,
        can_edit=False,
    )


def get_syntax_type(path: str) -> str:
    """Get the syntax highlighting type for a text file.

    Returns a string suitable for syntax highlighter selection:
    'css', 'html', 'xml', 'json', 'paloc', 'plain'.
    """
    ext = os.path.splitext(path)[1].lower()
    syntax_map = {
        ".css": "css",
        ".html": "html",
        ".thtml": "html",
        ".xml": "xml",
        ".json": "json",
        ".paloc": "paloc",
        ".txt": "plain",
        ".csv": "plain",
    }
    return syntax_map.get(ext, "plain")


def is_text_file(path: str) -> bool:
    """Check if a file is a text file that can be opened in the editor."""
    info = detect_file_type(path)
    return info.can_edit


def is_previewable(path: str) -> bool:
    """Check if a file can be previewed in the Browse tab."""
    info = detect_file_type(path)
    return info.can_preview
