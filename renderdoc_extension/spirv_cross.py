"""
spirv-cross integration for RenderDoc MCP.

When RenderDoc's built-in disassembly targets do not include GLSL/HLSL
(common on Vulkan captures without SPIRV-Cross compiled in), this module
provides a fallback by invoking the spirv-cross CLI tool on raw SPIR-V bytes.

RenderDoc ships spirv-cross.exe under plugins/spirv/ and may register it as
a Shader Processing Tool visible in the UI dropdown -- but that UI-only path
is NOT exposed through the Python GetDisassemblyTargets API.  This module
bridges the gap.

Compatible with Python 3.6 (no f-strings).
"""

import os
import subprocess
import tempfile

_cached_path = None  # None = not searched yet; "" = searched, not found

_LANG_ARGS = {
    "glsl": [],
    "hlsl": ["--hlsl"],
    "msl": ["--msl"],
}

SPIRV_MAGIC = 0x07230203


def find_spirv_cross():
    """Find spirv-cross executable.  Returns absolute path or None.

    Search order:
    1. RENDERDOC_SPIRV_CROSS env var
    2. RenderDoc install directory: plugins/spirv/spirv-cross.exe
    3. Common Windows install locations
    """
    global _cached_path
    if _cached_path is not None:
        return _cached_path or None

    exe_name = "spirv-cross.exe" if os.name == "nt" else "spirv-cross"

    # 1. Env var
    env = os.environ.get("RENDERDOC_SPIRV_CROSS")
    if env and os.path.isfile(env):
        _cached_path = env
        return _cached_path

    # 2. Relative to this extension directory (RenderDoc install tree)
    #    Extension sits in e.g.  <RenderDoc>/extensions/renderdoc_mcp/...
    #    spirv-cross is at       <RenderDoc>/plugins/spirv/spirv-cross.exe
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        candidate = os.path.join(here, "plugins", "spirv", exe_name)
        if os.path.isfile(candidate):
            _cached_path = candidate
            return _cached_path
        here = os.path.dirname(here)

    # 3. Common install locations (Windows)
    for base_var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(base_var)
        if base:
            candidate = os.path.join(base, "RenderDoc", "plugins", "spirv", exe_name)
            if os.path.isfile(candidate):
                _cached_path = candidate
                return _cached_path

    _cached_path = ""
    return None


def is_available():
    """Return True if spirv-cross can be found."""
    return find_spirv_cross() is not None


def parse_lang(target_name):
    """Extract the base language key from a disassembly target name.

    Accepts values like "GLSL", "glsl", "HLSL (spirv-cross)", "MSL" etc.
    Returns the lowercase key usable in _LANG_ARGS, or None.
    """
    if not target_name:
        return None
    key = target_name.lower().split("(")[0].strip()
    return key if key in _LANG_ARGS else None


def is_spirv(raw_bytes):
    """Return True if *raw_bytes* starts with the SPIR-V magic number."""
    if not raw_bytes or len(raw_bytes) < 4:
        return False
    magic = (
        raw_bytes[0]
        | (raw_bytes[1] << 8)
        | (raw_bytes[2] << 16)
        | (raw_bytes[3] << 24)
    )
    return magic == SPIRV_MAGIC


def decompile(raw_bytes, target_lang="glsl", entry_point=None):
    """Decompile SPIR-V bytes using spirv-cross.

    Args:
        raw_bytes: bytes containing SPIR-V module.
        target_lang: "glsl", "hlsl", or "msl".
        entry_point: Shader entry-point name (optional).

    Returns:
        (code_string, None) on success, or (None, error_string) on failure.
    """
    exe = find_spirv_cross()
    if not exe:
        return None, "spirv-cross executable not found"

    lang_key = parse_lang(target_lang) or "glsl"

    fd, tmp_path = tempfile.mkstemp(suffix=".spv")
    try:
        os.write(fd, bytes(raw_bytes))
        os.close(fd)

        cmd = [exe] + _LANG_ARGS[lang_key] + [tmp_path]
        if entry_point:
            cmd.extend(["--entry", entry_point])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate()

        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace"), None
        else:
            err = stderr.decode("utf-8", errors="replace").strip()
            return None, "spirv-cross failed (rc=%d): %s" % (proc.returncode, err)
    except Exception as e:
        return None, "spirv-cross error: %s" % str(e)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
