"""User-written GLSL, stored as files and rendered by the browser.

There is a tempting version of this that builds a GStreamer GL pipeline —
gltestsrc into glshader into the display — and it is the wrong one. It needs an
EGL context on a headless Pi, a new source type in the runner, and a rendering
path that exists nowhere else in this codebase, all to put pixels somewhere
Chromium is already putting pixels perfectly well.

So a shader is a web page. Web mode already runs Chromium full-screen on the
DRM display; a shader is served as a page that fills the viewport with one
quad, and playing it is web mode pointed at this node. That has a second
benefit worth more than the saving: the GUI's editor previews with the *same*
page, so what you see while writing it is what goes on the wall, rather than
two renderers that agree until they do not.

Shaders are written the Shadertoy way — `mainImage(out vec4 fragColor, in vec2
fragCoord)` with iTime, iResolution and iMouse — because that is the dialect
anything you might paste in is already written in. A shader that defines its
own `main()` is left alone.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from . import config

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,60}$")
SUFFIX = ".frag"
MAX_BYTES = 256 * 1024

# GLSL has no file access, no network and no way out of its own fragment, so
# hostile source is a broken picture rather than a broken node. These are the
# things that are not GLSL at all — an attempt to break out of the <script>
# the source is embedded in, which is a real escape and the only one available.
_FORBIDDEN = re.compile(r"</\s*script", re.IGNORECASE)

DEFAULT_SOURCE = """\
// Shadertoy-style: iTime, iResolution and iMouse are provided.
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    vec3 col = 0.5 + 0.5 * cos(iTime + uv.xyx + vec3(0.0, 2.0, 4.0));
    fragColor = vec4(col, 1.0);
}
"""


def directory() -> Path:
    return config.STATE_DIR / "shaders"


def valid_name(name: str) -> bool:
    """A usable shader name.

    The character set alone is not enough: ".." matches it, and although it
    resolves to a harmless dotfile inside the directory rather than escaping
    it, relying on that is accidental safety. Requiring one real character
    makes ".", ".." and "   " obviously invalid instead of incidentally
    harmless.
    """
    name = name or ""
    return bool(_NAME_RE.match(name)) and any(ch.isalnum() for ch in name)


def path_for(name: str) -> Optional[Path]:
    """Resolve a name to a file inside the shader directory, or None.

    Same shape as media.resolve for the same reason: the name arrives from a
    browser, and a name that escapes the directory is a file read.
    """
    if not valid_name(name):
        return None
    root = directory()
    candidate = (root / (Path(name).name + SUFFIX)).resolve()
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    if root_resolved not in candidate.parents:
        return None
    return candidate


def check_source(source: str) -> str:
    """Why this source cannot be stored, or "" if it can."""
    if not source.strip():
        return "the shader is empty"
    if len(source.encode()) > MAX_BYTES:
        return f"the shader is larger than {MAX_BYTES // 1024} KB"
    if _FORBIDDEN.search(source):
        return "the shader cannot contain a closing script tag"
    if "mainImage" not in source and not re.search(r"\bvoid\s+main\s*\(", source):
        return ("the shader needs either mainImage(out vec4, in vec2) or its "
                "own void main()")
    return ""


def all_shaders() -> List[Dict[str, object]]:
    root = directory()
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob(f"*{SUFFIX}"), key=lambda p: p.name.lower()):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"name": path.stem, "size": stat.st_size,
                    "modified": stat.st_mtime})
    return out


def get(name: str) -> Optional[str]:
    path = path_for(name)
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def save(name: str, source: str) -> str:
    if not valid_name(name):
        raise ValueError(
            "a name may only contain letters, numbers, spaces, dots, dashes "
            "and underscores, up to 60 characters")
    problem = check_source(source)
    if problem:
        raise ValueError(problem)
    path = path_for(name)
    if path is None:
        raise ValueError(f"not a usable shader name: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(source, encoding="utf-8")
    tmp.replace(path)
    return name


def delete(name: str) -> bool:
    path = path_for(name)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def needs_wrapper(source: str) -> bool:
    """A shader with its own main() is complete; one with mainImage is not."""
    return not re.search(r"\bvoid\s+main\s*\(", source)
