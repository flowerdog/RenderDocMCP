"""
Export service for RenderDoc MCP Bridge.
Exports textures/shaders/meshes to files, serving them via HTTP.

Compatible with Python 3.6 (no f-strings).
"""

import os
import struct
import time
import zlib

import renderdoc as rd

from ..utils import Parsers
from .. import spirv_cross


# ======================================================================
# Source-engine coordinate / UV conventions for mesh export (OBJ target).
#
# A capture's graphics API (OpenGL / Vulkan / D3D) does NOT uniquely
# determine object-space handedness or UV orientation of the vertex
# data -- the source ENGINE does. Examples:
#   - Unity always outputs LH object space + V-up UVs, no matter whether
#     the backend is GL ES, Vulkan, or D3D. Android emulators (MuMu /
#     LDPlayer / NoxPlayer) using ANGLE to translate GL ES to Vulkan
#     produce "Vulkan" captures whose vertex data still follows Unity's
#     conventions (and also strip all identifiers, making post-hoc
#     detection harder).
#   - Unreal uses LH + V-down.
#   - Native GL engines typically use RH + V-up (OpenGL convention).
#   - Native D3D / native Vulkan engines typically use LH + V-down.
#
# Fields:
#   flip_hand : negate X + reverse winding, converting LH object space
#               into OBJ's right-handed format. Unity's OBJ importer
#               negates X on its side, so flip_hand=True round-trips
#               cleanly into Unity.
#   flip_uv_v : V -> 1 - V, converting top-left-origin UVs (D3D / Vulkan
#               native / Unreal) into OBJ's bottom-left-origin
#               convention (also what Unity uses at runtime).
# ======================================================================
_ENGINE_CONVENTIONS = {
    "unity":         {"flip_hand": True,  "flip_uv_v": False},
    "unreal":        {"flip_hand": True,  "flip_uv_v": True},
    "native_gl":     {"flip_hand": False, "flip_uv_v": False},
    "native_d3d":    {"flip_hand": True,  "flip_uv_v": True},
    "native_vulkan": {"flip_hand": True,  "flip_uv_v": True},
}
_VALID_ENGINES = tuple(_ENGINE_CONVENTIONS.keys())

# High-confidence substrings that identify Unity in shader reflection
# or loaded module lists. Case-sensitive match (Unity's internal names
# are mixed-case; lowercasing would create false positives on common
# words like "unity").
_UNITY_MODULE_MARKERS = (
    "libunity.so", "UnityPlayer.dll", "UnityPlayer.dylib",
    "libmain.so",  # Android Unity activity wrapper
)
_UNITY_SHADER_MARKERS = (
    # Global / built-in constant block names
    "UnityPerDraw", "UnityPerCamera", "UnityPerFrame", "UnityPerMaterial",
    "UnityShadows", "UnityLighting", "UnityInstancing",
    "UnityStereoGlobals", "UnityShaderVariables", "UnityPerDrawSprite",
    # Built-in variable names (survive more toolchain passes than block
    # names because they're referenced directly in shader bodies)
    "unity_ObjectToWorld", "unity_WorldToObject",
    "unity_MatrixVP", "unity_MatrixV", "unity_MatrixP", "unity_MatrixInvV",
    "unity_CameraProjection", "unity_WorldTransformParams",
    "_WorldSpaceCameraPos", "_WorldSpaceLightPos0",
    "_MainLightPosition", "_MainLightColor",
    # Texture / sampler names (URP + Built-in RP)
    "_CameraDepthTexture", "_CameraOpaqueTexture",
    "_CameraColorTexture", "_MainLightShadowmapTexture",
)
_UNREAL_MODULE_MARKERS = (
    "libUE4.so", "libUnreal.so", "libUE5.so",
    "UE4Game-", "UE5Game-", "UnrealEngine",
)
_UNREAL_SHADER_MARKERS = (
    "View_WorldToClip", "View_ClipToWorld",
    "View_TranslatedWorldToClip", "View_WorldCameraOrigin",
    "Primitive_LocalToWorld", "Primitive_WorldToLocal",
    "ResolvedView", "FViewUniformShaderParameters",
    "View_PreViewTranslation", "View_ViewRectMin",
)


class ExportService(object):
    """Handles texture, shader and mesh export to files."""

    def __init__(self, ctx, invoke_fn, export_dir, file_server_base_url):
        self.ctx = ctx
        self._invoke = invoke_fn
        self.export_dir = export_dir
        self.base_url = file_server_base_url

    def _ensure_export_dir(self):
        if not os.path.isdir(self.export_dir):
            os.makedirs(self.export_dir)

    def _build_url(self, filename):
        return "%s/%s" % (self.base_url, filename)

    def _get_capture_tag(self):
        """Get a short tag from the current capture filename for use in export filenames."""
        try:
            cap_path = self.ctx.GetCaptureFilename()
            if cap_path:
                basename = os.path.basename(cap_path)
                name, _ = os.path.splitext(basename)
                # Sanitize: keep only alphanumeric, dash, underscore, dot
                safe = ""
                for ch in name:
                    if ch.isalnum() or ch in ("-", "_", "."):
                        safe += ch
                    else:
                        safe += "_"
                return safe
        except Exception:
            pass
        return "capture"

    # ======================== Engine Detection ========================

    @staticmethod
    def _first_match(haystack, needles):
        """Return the first needle found in haystack, or None."""
        for n in needles:
            if n in haystack:
                return n
        return None

    def _collect_module_names(self, controller):
        """Extract loaded module / executable name strings from the
        structured file chunk metadata. Returns a single joined string
        for substring matching, plus the raw list for diagnostics.
        """
        names = []
        try:
            sdfile = controller.GetStructuredFile()
            # Chunks live in sdfile.chunks (SDChunk objects). The first
            # chunks typically include driver init / image load metadata.
            # We stringify each chunk's top-level scalar strings so that
            # we can do substring matching without depending on exact
            # chunk field layouts (which vary across APIs).
            chunks = getattr(sdfile, "chunks", None) or []
            # Cap the scan for perf -- module names always appear early.
            scan_limit = 64
            for i, chunk in enumerate(chunks):
                if i >= scan_limit:
                    break
                try:
                    s = str(chunk)
                except Exception:
                    continue
                names.append(s)
        except Exception:
            pass
        joined = "\n".join(names)
        # Also include the capture filename as a weak hint.
        try:
            cap_path = self.ctx.GetCaptureFilename()
            if cap_path:
                joined = joined + "\n" + cap_path
        except Exception:
            pass
        return joined

    @staticmethod
    def _collect_reflection_tokens(reflection):
        """Gather a single joined string of all identifier-like fields
        from a ShaderReflection for substring matching.
        """
        parts = []
        if reflection is None:
            return ""
        try:
            parts.append(str(getattr(reflection, "entryPoint", "") or ""))
        except Exception:
            pass
        try:
            for cb in (getattr(reflection, "constantBlocks", None) or []):
                parts.append(str(getattr(cb, "name", "") or ""))
                for v in (getattr(cb, "variables", None) or []):
                    parts.append(str(getattr(v, "name", "") or ""))
                    vtype = getattr(v, "type", None)
                    if vtype is not None:
                        for m in (getattr(vtype, "members", None) or []):
                            parts.append(str(getattr(m, "name", "") or ""))
        except Exception:
            pass
        try:
            for attr in ("readOnlyResources", "readWriteResources", "samplers"):
                lst = getattr(reflection, attr, None) or []
                for res in lst:
                    parts.append(str(getattr(res, "name", "") or ""))
        except Exception:
            pass
        return " ".join(parts)

    @staticmethod
    def _extract_spirv_generator(reflection):
        """Try to extract the SPIR-V Generator magic string from raw
        bytes, if the reflection is SPIR-V. Purely informational.
        """
        try:
            if getattr(reflection, "encoding", None) != rd.ShaderEncoding.SPIRV:
                return None
            raw = getattr(reflection, "rawBytes", None)
            if not raw:
                return None
            # SPIR-V module header is 5 uint32 little-endian:
            # magic, version, generator_magic, bound, schema.
            # We can't map generator_magic to a human string here, but
            # the OpSource / OpSourceExtension / OpString instructions
            # in the module often contain the generator name. Simple
            # heuristic: look for known substrings in the raw bytes.
            data = bytes(raw)
            known = (
                "ANGLE Shader Compiler",
                "Google Shaderc",
                "Microsoft (R) HLSL",
                "glslang",
                "SPIR-V Tools",
                "spirv-cross",
            )
            for marker in known:
                if marker.encode("utf-8", errors="ignore") in data:
                    return marker
        except Exception:
            pass
        return None

    def _detect_engine(self, controller, event_id):
        """Layered engine detection. Returns (engine_or_None, info_dict).

        Only returns a non-None engine when detection is high-confidence:
          - Layer 1: module / capture-metadata substring hit
          - Layer 2: shader reflection identifier substring hit
        Layer 3 (SPIR-V generator) is surfaced in info but never used
        as the sole basis for classification.
        """
        info = {
            "api": None,
            "capture_filename": None,
            "shader_generator": None,
            "module_markers_found": [],
            "reflection_markers_found": [],
            "stages_scanned": [],
        }
        try:
            info["api"] = str(controller.GetAPIProperties().pipelineType)
        except Exception:
            pass
        try:
            info["capture_filename"] = self.ctx.GetCaptureFilename() or None
        except Exception:
            pass

        # ---- Layer 1: capture-metadata / module names ----
        module_text = self._collect_module_names(controller)
        hit = self._first_match(module_text, _UNITY_MODULE_MARKERS)
        if hit:
            info["module_markers_found"].append(hit)
            return "unity", info
        hit = self._first_match(module_text, _UNREAL_MODULE_MARKERS)
        if hit:
            info["module_markers_found"].append(hit)
            return "unreal", info

        # ---- Layer 2: shader reflection identifiers ----
        # Check both vertex and pixel stages at the exported event.
        try:
            pipe = controller.GetPipelineState()
            for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Pixel):
                try:
                    refl = pipe.GetShaderReflection(stage)
                except Exception:
                    refl = None
                if refl is None:
                    continue
                info["stages_scanned"].append(str(stage))
                # Capture generator (informational)
                if info["shader_generator"] is None:
                    gen = self._extract_spirv_generator(refl)
                    if gen:
                        info["shader_generator"] = gen
                tokens = self._collect_reflection_tokens(refl)
                hit = self._first_match(tokens, _UNITY_SHADER_MARKERS)
                if hit:
                    info["reflection_markers_found"].append(hit)
                    return "unity", info
                hit = self._first_match(tokens, _UNREAL_SHADER_MARKERS)
                if hit:
                    info["reflection_markers_found"].append(hit)
                    return "unreal", info
        except Exception:
            pass

        return None, info

    # ======================== Texture Export ========================

    def export_texture(self, resource_id, event_id, mip=0, slice_index=0,
                       flip_y=None):
        """Export a texture to PNG file and return download URL.

        Args:
            resource_id: The resource ID of the texture to export.
            event_id: The event ID at which to capture the texture state.
            mip: Mip level to export (default 0).
            slice_index: Array slice / cube face (default 0).
            flip_y: Flip image vertically.
                None = auto-detect: flip only render targets when the
                       API / viewport indicates inverted rendering
                       (OpenGL framebuffers, Vulkan with negative viewport height).
                True = always flip.  False = never flip.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._ensure_export_dir()

        numeric_id = Parsers.extract_numeric_id(resource_id)
        tag = self._get_capture_tag()
        filename = "%s_tex_%d_eid%d_mip%d.png" % (tag, numeric_id, event_id, mip)
        output_path = os.path.join(self.export_dir, filename)

        result = {"data": None, "error": None}
        opts = {"flip_y": flip_y}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                api = controller.GetAPIProperties().pipelineType

                # Find the texture resource and check if it is a render target
                target_id = numeric_id
                tex_rid = None
                is_rt = False
                for tex in controller.GetTextures():
                    tex_id = Parsers.extract_numeric_id(str(tex.resourceId))
                    if tex_id == target_id:
                        tex_rid = tex.resourceId
                        try:
                            flags = int(tex.creationFlags)
                            rt_bits = int(rd.TextureCategory.ColorTarget) | int(rd.TextureCategory.DepthTarget)
                            is_rt = bool(flags & rt_bits)
                        except Exception:
                            is_rt = False
                        break

                if tex_rid is None:
                    result["error"] = "Texture not found: %s" % resource_id
                    return

                # Determine whether to flip
                do_flip = opts["flip_y"]
                if do_flip is None:
                    do_flip = self._detect_need_flip_y(
                        controller, api, is_rt)

                texsave = rd.TextureSave()
                texsave.resourceId = tex_rid
                texsave.destType = rd.FileType.PNG
                texsave.mip = mip
                texsave.slice.sliceIndex = slice_index
                texsave.alpha = rd.AlphaMapping.Preserve

                controller.SaveTexture(texsave, output_path)

                if not os.path.isfile(output_path):
                    result["error"] = "SaveTexture did not produce output file"
                    return

                # Flip the saved PNG if needed
                if do_flip:
                    ok = self._flip_png_vertical(output_path)
                    if not ok:
                        print("[ExportTexture] WARNING: PNG flip failed for %s"
                              % output_path)
                        do_flip = False

                print("[ExportTexture] eid=%d, api=%s, is_rt=%s, flip_y=%s"
                      % (event_id, str(api), is_rt, do_flip))

                file_size = os.path.getsize(output_path)
                result["data"] = {
                    "url": self._build_url(filename),
                    "filename": filename,
                    "path": output_path,
                    "size_bytes": file_size,
                    "resource_id": resource_id,
                    "event_id": event_id,
                    "mip": mip,
                    "slice": slice_index,
                    "format": "png",
                    "api": str(api),
                    "is_render_target": is_rt,
                    "flip_y": do_flip,
                }
            except Exception as e:
                import traceback
                result["error"] = "Export failed: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    @staticmethod
    def _detect_need_flip_y(controller, api, is_render_target):
        """Auto-detect whether a texture needs vertical flipping.

        Only render targets may need flipping; regular textures are stored
        top-to-bottom in GPU memory for all modern APIs.

        Render targets are flipped for all APIs by default. This handles both
        native OpenGL (bottom-up framebuffer layout) and ANGLE-translated
        Vulkan captures (OpenGL ES -> Vulkan, which preserves GL's bottom-up
        layout). Users can override via the explicit flip_y parameter if the
        default is wrong for their specific capture.
        """
        if not is_render_target:
            return False

        return True

    # -------------------- PNG vertical flip --------------------

    @staticmethod
    def _flip_png_vertical(filepath):
        """Flip a PNG image vertically in-place.

        Pure-Python implementation using only stdlib (struct + zlib).
        Returns True on success, False if the file could not be flipped.
        """
        with open(filepath, "rb") as f:
            data = f.read()

        if data[:8] != b'\x89PNG\r\n\x1a\n':
            return False

        # Parse all chunks
        chunks = []
        pos = 8
        while pos + 8 <= len(data):
            length = struct.unpack('>I', data[pos:pos + 4])[0]
            ctype = data[pos + 4:pos + 8]
            if pos + 12 + length > len(data):
                break
            cdata = data[pos + 8:pos + 8 + length]
            chunks.append((ctype, cdata))
            pos += 12 + length

        # Extract IHDR
        ihdr = None
        for ctype, cdata in chunks:
            if ctype == b'IHDR':
                ihdr = cdata
                break
        if ihdr is None or len(ihdr) < 13:
            return False

        width, height = struct.unpack('>II', ihdr[:8])
        bit_depth = ihdr[8]
        color_type = ihdr[9]
        interlace = ihdr[12]

        if interlace != 0 or height == 0:
            return False

        ch_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
        channels = ch_map.get(color_type)
        if channels is None:
            return False

        bpp = max(1, channels * bit_depth // 8)
        if color_type == 3:
            row_bytes = (width * bit_depth + 7) // 8
        else:
            row_bytes = width * channels * bit_depth // 8
        scanline_len = 1 + row_bytes

        # Decompress IDAT
        idat_data = b''.join(cd for ct, cd in chunks if ct == b'IDAT')
        if not idat_data:
            return False

        try:
            raw = zlib.decompress(idat_data)
        except zlib.error:
            return False

        if len(raw) != height * scanline_len:
            return False

        # Split into scanlines and decode filters
        prev = bytearray(row_bytes)
        decoded_rows = []
        for y in range(height):
            off = y * scanline_len
            ft = raw[off]
            row_raw = bytearray(raw[off + 1:off + scanline_len])
            row = ExportService._png_unfilter(ft, row_raw, prev, bpp)
            decoded_rows.append(row)
            prev = row

        # Reverse row order
        decoded_rows.reverse()

        # Re-encode with filter type 0 (None)
        parts = []
        for row in decoded_rows:
            parts.append(b'\x00')
            parts.append(bytes(row))
        new_raw = b''.join(parts)

        new_idat = zlib.compress(new_raw)

        def _make_chunk(ct, cd):
            body = ct + cd
            crc = struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)
            return struct.pack('>I', len(cd)) + body + crc

        # Rebuild PNG
        out_parts = [b'\x89PNG\r\n\x1a\n']
        idat_written = False
        for ct, cd in chunks:
            if ct == b'IDAT':
                if not idat_written:
                    out_parts.append(_make_chunk(b'IDAT', new_idat))
                    idat_written = True
                continue
            if ct == b'IEND':
                continue
            out_parts.append(_make_chunk(ct, cd))
        out_parts.append(_make_chunk(b'IEND', b''))

        with open(filepath, "wb") as f:
            f.write(b''.join(out_parts))

        return True

    @staticmethod
    def _png_unfilter(filter_type, row, prev_row, bpp):
        """Decode one PNG filter row. Modifies *row* in-place and returns it."""
        if filter_type == 0:
            pass
        elif filter_type == 1:  # Sub
            for i in range(bpp, len(row)):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(len(row)):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(len(row)):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + (a + prev_row[i]) // 2) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(len(row)):
                a = row[i - bpp] if i >= bpp else 0
                b = prev_row[i]
                c = prev_row[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                row[i] = (row[i] + pr) & 0xFF
        return row

    # ======================== Shader Export ========================

    def export_shader(self, event_id, stage, disassembly_target=None):
        """Export bound shader disassembly to text file and return download URL."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._ensure_export_dir()

        tag = self._get_capture_tag()
        stage_name = str(stage).lower()
        filename = "%s_shader_%s_eid%d.txt" % (tag, stage_name, event_id)
        output_path = os.path.join(self.export_dir, filename)

        result = {"data": None, "error": None}
        _spirv = {}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                pipe = controller.GetPipelineState()
                stage_enum = Parsers.parse_stage(stage)
                shader = pipe.GetShader(stage_enum)

                if shader == rd.ResourceId.Null():
                    result["error"] = "No %s shader bound at event_id %d" % (stage, event_id)
                    return

                reflection = pipe.GetShaderReflection(stage_enum)
                if reflection is None:
                    result["error"] = "Shader reflection unavailable for %s at event_id %d" % (
                        stage,
                        event_id,
                    )
                    return

                targets = controller.GetDisassemblyTargets(True)
                if not targets:
                    result["error"] = "No disassembly target available"
                    return

                from .pipeline_service import PipelineService
                chosen, available = PipelineService._choose_disassembly_target(
                    targets, disassembly_target
                )

                # Capture raw SPIR-V for potential spirv-cross fallback
                try:
                    if (hasattr(reflection, "encoding")
                            and reflection.encoding == rd.ShaderEncoding.SPIRV):
                        raw = reflection.rawBytes
                        if raw and spirv_cross.is_spirv(raw):
                            _spirv["raw"] = bytes(raw)
                            _spirv["entry"] = reflection.entryPoint
                            _spirv["shader_id"] = str(shader)
                            _spirv["available"] = available
                except Exception:
                    pass

                if chosen is None and disassembly_target:
                    if _spirv.get("raw") and spirv_cross.parse_lang(disassembly_target):
                        _spirv["fallback_needed"] = True
                        return
                    result["error"] = (
                        "Requested target '%s' not available. Available: %s"
                        % (disassembly_target, ", ".join(available))
                    )
                    return

                if chosen is None:
                    chosen = available[0]

                # Check if default choice can be upgraded via spirv-cross
                if not disassembly_target and _spirv.get("raw"):
                    is_preferred = False
                    for pref in PipelineService.PREFERRED_TARGETS:
                        if pref.lower() in chosen.lower():
                            is_preferred = True
                            break
                    if not is_preferred:
                        _spirv["upgrade_from"] = chosen

                pipe_obj = pipe.GetGraphicsPipelineObject()
                if stage_enum == rd.ShaderStage.Compute:
                    try:
                        pipe_obj = pipe.GetComputePipelineObject()
                    except Exception:
                        pass

                disasm = controller.DisassembleShader(pipe_obj, reflection, chosen)
                if not disasm:
                    result["error"] = "Shader disassembly is empty"
                    return

                self._write_shader_file(
                    output_path, disasm, event_id, stage_name, chosen, str(shader)
                )

                if not os.path.isfile(output_path):
                    result["error"] = "Shader export did not produce output file"
                    return

                file_size = os.path.getsize(output_path)
                result["data"] = {
                    "url": self._build_url(filename),
                    "filename": filename,
                    "path": output_path,
                    "size_bytes": file_size,
                    "event_id": event_id,
                    "stage": stage_name,
                    "resource_id": str(shader),
                    "disassembly_target": chosen,
                    "available_disassembly_targets": available,
                    "format": "txt",
                }
            except Exception as e:
                import traceback
                result["error"] = "Shader export failed: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        # spirv-cross fallback: explicit target requested but not in API list
        if _spirv.get("fallback_needed") and not result["data"] and not result["error"]:
            self._export_via_spirv_cross(
                _spirv, disassembly_target, output_path, filename,
                event_id, stage_name, result,
            )

        # spirv-cross upgrade: default mode chose a non-preferred target
        if _spirv.get("upgrade_from") and result["data"]:
            self._upgrade_via_spirv_cross(
                _spirv, output_path, filename, result,
            )

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def _write_shader_file(self, path, disasm, event_id, stage_name,
                           target, shader_id):
        with open(path, "w") as f:
            f.write("// Exported from RenderDoc MCP\n")
            f.write("// event_id: %d\n" % event_id)
            f.write("// stage: %s\n" % stage_name)
            f.write("// disassembly_target: %s\n" % target)
            f.write("// resource_id: %s\n\n" % shader_id)
            f.write(disasm)

    def _export_via_spirv_cross(self, spirv_data, disassembly_target,
                                output_path, filename, event_id,
                                stage_name, result):
        """Decompile SPIR-V via spirv-cross and write the export file."""
        lang = spirv_cross.parse_lang(disassembly_target) or "glsl"
        code, error = spirv_cross.decompile(
            spirv_data["raw"], lang, spirv_data.get("entry")
        )
        if not code:
            available = spirv_data.get("available", [])
            result["error"] = (
                "Requested target '%s' not available via API (Available: %s) "
                "and spirv-cross fallback failed: %s"
                % (disassembly_target, ", ".join(available), error)
            )
            return

        target_label = "%s (spirv-cross)" % lang.upper()
        self._write_shader_file(
            output_path, code, event_id, stage_name,
            target_label, spirv_data.get("shader_id", ""),
        )
        available = list(spirv_data.get("available", []))
        for tag in ("GLSL (spirv-cross)", "HLSL (spirv-cross)"):
            if tag not in available:
                available.append(tag)

        file_size = os.path.getsize(output_path)
        result["data"] = {
            "url": self._build_url(filename),
            "filename": filename,
            "path": output_path,
            "size_bytes": file_size,
            "event_id": event_id,
            "stage": stage_name,
            "resource_id": spirv_data.get("shader_id", ""),
            "disassembly_target": target_label,
            "available_disassembly_targets": available,
            "format": "txt",
        }

    def _upgrade_via_spirv_cross(self, spirv_data, output_path, filename,
                                 result):
        """Attempt to replace a non-preferred disassembly with GLSL."""
        code, _error = spirv_cross.decompile(
            spirv_data["raw"], "glsl", spirv_data.get("entry")
        )
        if not code:
            return

        data = result["data"]
        target_label = "GLSL (spirv-cross)"
        self._write_shader_file(
            output_path, code, data["event_id"], data["stage"],
            target_label, data.get("resource_id", ""),
        )
        data["disassembly_target"] = target_label
        data["size_bytes"] = os.path.getsize(output_path)
        available = data.get("available_disassembly_targets", [])
        for tag in ("GLSL (spirv-cross)", "HLSL (spirv-cross)"):
            if tag not in available:
                available.append(tag)

    # ======================== Mesh Export ========================

    def export_mesh(self, event_id, flip_uv_v=None, flip_handedness=None,
                    source_engine=None):
        """Export mesh at a draw call to OBJ file and return download URL.

        Args:
            event_id: The event ID of the draw call.
            flip_uv_v: Force V-flip (1-v). None = derived from source_engine
                or auto-detection. True/False = override.
            flip_handedness: Force X-negation + winding reversal. None =
                derived from source_engine or auto-detection.
                True/False = override.
            source_engine: One of "unity", "unreal", "native_gl",
                "native_d3d", "native_vulkan", "auto", or None (same as
                "auto"). When "auto" or None, runs high-confidence
                engine detection. If detection fails, the call returns
                an error (not a silent API-based guess) asking the caller
                to retry with an explicit source_engine. Explicit
                flip_uv_v / flip_handedness always win over
                source_engine.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._ensure_export_dir()

        tag = self._get_capture_tag()
        filename = "%s_mesh_eid%d.obj" % (tag, event_id)
        output_path = os.path.join(self.export_dir, filename)

        # Validate source_engine up front so we don't pay the BlockInvoke
        # cost for a clearly bad request.
        if source_engine is not None and source_engine != "auto" \
                and source_engine not in _ENGINE_CONVENTIONS:
            return {
                "error": "invalid_source_engine",
                "message": ("source_engine must be one of %s, 'auto', or omitted"
                            % (list(_VALID_ENGINES),)),
                "valid_values": list(_VALID_ENGINES) + ["auto"],
            }

        result = {"data": None, "error": None}
        opts = {
            "flip_uv_v": flip_uv_v,
            "flip_hand": flip_handedness,
            "source_engine": source_engine,
        }

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                api = controller.GetAPIProperties().pipelineType

                do_flip_uv = opts["flip_uv_v"]
                do_flip_hand = opts["flip_hand"]
                engine_hint = opts["source_engine"]
                detected_engine = None
                detection_info = None
                engine_source = None  # "explicit" | "detected" | "explicit-flags"

                # 1) If user passed BOTH explicit flip params, honour them
                #    without running detection (preserves old behaviour).
                if do_flip_uv is not None and do_flip_hand is not None:
                    engine_source = "explicit-flags"
                else:
                    # 2) Resolve engine: explicit source_engine wins;
                    #    otherwise run high-confidence auto-detection.
                    if engine_hint and engine_hint != "auto":
                        detected_engine = engine_hint
                        engine_source = "explicit"
                    else:
                        detected_engine, detection_info = \
                            self._detect_engine(controller, event_id)
                        engine_source = "detected"

                        if detected_engine is None:
                            # Detection failed -- surface a rich error so
                            # the caller (or the human behind it) can pick
                            # a source_engine explicitly.
                            info = detection_info or {}
                            info["event_id"] = event_id
                            info["note"] = (
                                "Tip: Android emulator captures via ANGLE "
                                "(MuMu / LDPlayer / NoxPlayer) typically strip "
                                "all Unity identifiers. If the capture is of "
                                "a Unity game, retry with source_engine='unity'."
                            )
                            result["error"] = "engine_detection_failed"
                            result["error_data"] = {
                                "message": ("Could not detect source engine "
                                            "from capture metadata or shader "
                                            "reflection. Retry with explicit "
                                            "source_engine."),
                                "valid_values": list(_VALID_ENGINES),
                                "detection_info": info,
                            }
                            return

                    # Apply conventions, but only for fields the caller
                    # didn't pin. Explicit flip_uv_v / flip_handedness
                    # always win over the engine-derived defaults.
                    conv = _ENGINE_CONVENTIONS[detected_engine]
                    if do_flip_uv is None:
                        do_flip_uv = conv["flip_uv_v"]
                    if do_flip_hand is None:
                        do_flip_hand = conv["flip_hand"]

                draw = self.ctx.GetAction(event_id)
                if draw is None:
                    result["error"] = "No action found at event_id %d" % event_id
                    return

                state = controller.GetPipelineState()
                ib = state.GetIBuffer()
                vbs = state.GetVBuffers()
                attrs = state.GetVertexInputs()

                if not attrs:
                    result["error"] = "No vertex inputs at event_id %d" % event_id
                    return

                print(("[ExportMesh] eid=%d, api=%s, engine=%s(%s), "
                       "flip_uv=%s, flip_hand=%s, %d vertex attrs: %s")
                      % (event_id, str(api),
                         detected_engine, engine_source,
                         do_flip_uv, do_flip_hand, len(attrs),
                         [(a.name, "inst" if a.perInstance else "vert",
                           str(a.format.compType), a.format.compCount,
                           a.format.compByteWidth)
                          for a in attrs]))

                pos_attr, normal_attr, texcoord_attr = \
                    self._identify_vertex_attrs(attrs)

                if pos_attr is None:
                    attr_names = [a.name for a in attrs if not a.perInstance]
                    result["error"] = (
                        "No POSITION attribute found at event_id %d. "
                        "Available vertex attributes: %s"
                        % (event_id, attr_names)
                    )
                    return

                # Get indices
                indices = self._get_indices(controller, draw, ib)

                # Build vertex buffer cache: {vb_index: bytes}
                vb_cache = {}
                needed_vbs = set()
                for a in [pos_attr, normal_attr, texcoord_attr]:
                    if a is not None:
                        needed_vbs.add(a.vertexBuffer)

                for vb_idx in needed_vbs:
                    vb = vbs[vb_idx]
                    data = controller.GetBufferData(vb.resourceId, vb.byteOffset, 0)
                    vb_cache[vb_idx] = bytes(data)

                # Decode all unique vertices
                unique_indices = sorted(set(indices))
                index_remap = {}
                for new_idx, old_idx in enumerate(unique_indices):
                    index_remap[old_idx] = new_idx

                positions = []
                normals = []
                texcoords = []

                for idx in unique_indices:
                    # Position (required)
                    pos = self._read_vertex_attr(
                        pos_attr, vbs, vb_cache, idx, draw.vertexOffset
                    )
                    positions.append(pos)

                    if normal_attr is not None:
                        n = self._read_vertex_attr(
                            normal_attr, vbs, vb_cache, idx, draw.vertexOffset
                        )
                        normals.append(n)

                    if texcoord_attr is not None:
                        t = self._read_vertex_attr(
                            texcoord_attr, vbs, vb_cache, idx, draw.vertexOffset
                        )
                        texcoords.append(t)

                # Write OBJ
                has_normals = len(normals) == len(positions)
                has_texcoords = len(texcoords) == len(positions)
                face_count = len(indices) // 3

                with open(output_path, "w") as f:
                    f.write("# Exported from RenderDoc MCP - event_id %d\n" % event_id)
                    f.write("# Vertices: %d, Faces: %d\n" % (len(positions), face_count))
                    f.write("# API: %s, flip_uv_v: %s, flip_handedness: %s\n"
                            % (str(api), do_flip_uv, do_flip_hand))
                    f.write("\n")

                    for p in positions:
                        if do_flip_hand:
                            if len(p) >= 3:
                                f.write("v %s %s %s\n" % (-p[0], p[1], p[2]))
                            elif len(p) == 2:
                                f.write("v %s %s 0\n" % (-p[0], p[1]))
                        else:
                            if len(p) >= 3:
                                f.write("v %s %s %s\n" % (p[0], p[1], p[2]))
                            elif len(p) == 2:
                                f.write("v %s %s 0\n" % (p[0], p[1]))

                    if has_texcoords:
                        f.write("\n")
                        for t in texcoords:
                            if len(t) >= 2:
                                v_coord = 1.0 - t[1] if do_flip_uv else t[1]
                                f.write("vt %s %s\n" % (t[0], v_coord))
                            elif len(t) == 1:
                                f.write("vt %s 0\n" % t[0])

                    if has_normals:
                        f.write("\n")
                        for n in normals:
                            if len(n) >= 3:
                                if do_flip_hand:
                                    f.write("vn %s %s %s\n" % (-n[0], n[1], n[2]))
                                else:
                                    f.write("vn %s %s %s\n" % (n[0], n[1], n[2]))

                    f.write("\n")
                    for i in range(0, len(indices) - 2, 3):
                        # OBJ indices are 1-based
                        i0 = index_remap[indices[i]] + 1
                        if do_flip_hand:
                            i1 = index_remap[indices[i + 2]] + 1
                            i2 = index_remap[indices[i + 1]] + 1
                        else:
                            i1 = index_remap[indices[i + 1]] + 1
                            i2 = index_remap[indices[i + 2]] + 1

                        if has_texcoords and has_normals:
                            f.write("f %d/%d/%d %d/%d/%d %d/%d/%d\n"
                                    % (i0, i0, i0, i1, i1, i1, i2, i2, i2))
                        elif has_texcoords:
                            f.write("f %d/%d %d/%d %d/%d\n"
                                    % (i0, i0, i1, i1, i2, i2))
                        elif has_normals:
                            f.write("f %d//%d %d//%d %d//%d\n"
                                    % (i0, i0, i1, i1, i2, i2))
                        else:
                            f.write("f %d %d %d\n" % (i0, i1, i2))

                file_size = os.path.getsize(output_path)
                result["data"] = {
                    "url": self._build_url(filename),
                    "filename": filename,
                    "path": output_path,
                    "size_bytes": file_size,
                    "event_id": event_id,
                    "vertex_count": len(positions),
                    "face_count": face_count,
                    "has_normals": has_normals,
                    "has_texcoords": has_texcoords,
                    "api": str(api),
                    "flip_uv_v": do_flip_uv,
                    "flip_handedness": do_flip_hand,
                    "source_engine": detected_engine,
                    "source_engine_source": engine_source,
                    "format": "obj",
                }
                if detection_info is not None:
                    result["data"]["detection_info"] = detection_info
            except Exception as e:
                import traceback
                result["error"] = "Mesh export failed: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        # Structured error (e.g. engine_detection_failed) bubbles up as a
        # dict so the MCP layer can relay full detail to the caller. Free-
        # form strings are raised as ValueError like before.
        if result["error"]:
            if result.get("error_data") is not None:
                payload = {"error": result["error"]}
                payload.update(result["error_data"])
                return payload
            raise ValueError(result["error"])
        return result["data"]

    # ======================== Helpers ========================

    @staticmethod
    def _identify_vertex_attrs(attrs):
        """Identify position, normal, texcoord from vertex input attributes.

        Strategy:
        1. Match by semantic name (POSITION, NORMAL, TEXCOORD and common variants)
        2. Fallback: assign by slot order among per-vertex float3/float4 attrs
        """
        pos_attr = None
        normal_attr = None
        texcoord_attr = None

        _POS_NAMES = ("POSITION", "SV_POSITION", "POS", "IN_POSITION", "INPOSITION")
        _NORM_NAMES = ("NORMAL", "NORM", "IN_NORMAL", "INNORMAL")
        _UV_NAMES = ("TEXCOORD", "UV", "TEX", "IN_TEXCOORD", "INTEXCOORD")

        per_vertex = [a for a in attrs if not a.perInstance]

        for attr in per_vertex:
            name = attr.name.upper().lstrip("_")
            for prefix in _POS_NAMES:
                if name.startswith(prefix):
                    if pos_attr is None:
                        pos_attr = attr
                    break
            else:
                for prefix in _NORM_NAMES:
                    if name.startswith(prefix):
                        if normal_attr is None:
                            normal_attr = attr
                        break
                else:
                    for prefix in _UV_NAMES:
                        if name.startswith(prefix):
                            if texcoord_attr is None:
                                texcoord_attr = attr
                            break

        if pos_attr is not None:
            return pos_attr, normal_attr, texcoord_attr

        # Fallback: assign by slot order for float attrs with 2-4 components
        float_attrs = [
            a for a in per_vertex
            if a.format.compType == rd.CompType.Float and a.format.compCount >= 2
        ]
        if float_attrs:
            pos_attr = float_attrs[0]
            if len(float_attrs) > 1 and float_attrs[1].format.compCount >= 3:
                normal_attr = float_attrs[1]
            if len(float_attrs) > 2 and float_attrs[2].format.compCount >= 2:
                texcoord_attr = float_attrs[2]
            elif len(float_attrs) > 1 and float_attrs[1].format.compCount == 2:
                texcoord_attr = float_attrs[1]
                normal_attr = None

        return pos_attr, normal_attr, texcoord_attr

    @staticmethod
    def _get_indices(controller, draw, ib):
        """Decode index buffer for the draw call."""
        num_indices = draw.numIndices

        if not (draw.flags & rd.ActionFlags.Indexed):
            return list(range(num_indices))

        if ib.resourceId == rd.ResourceId.Null():
            return list(range(num_indices))

        index_format = "B"
        if ib.byteStride == 2:
            index_format = "H"
        elif ib.byteStride == 4:
            index_format = "I"

        ibdata = controller.GetBufferData(ib.resourceId, ib.byteOffset, 0)
        ibdata = bytes(ibdata)

        fmt = str(num_indices) + index_format
        offset = draw.indexOffset * ib.byteStride
        indices = struct.unpack_from(fmt, ibdata, offset)

        return [i + draw.baseVertex for i in indices]

    @staticmethod
    def _read_vertex_attr(attr, vbs, vb_cache, vertex_index, vertex_offset):
        """Read a single vertex attribute value."""
        vb = vbs[attr.vertexBuffer]
        vb_data = vb_cache.get(attr.vertexBuffer)
        if vb_data is None:
            return (0.0,)

        offset = (
            attr.byteOffset
            + vb.byteStride * (vertex_index + vertex_offset)
        )

        fmt = attr.format
        if fmt.Special():
            return (0.0,) * fmt.compCount

        format_chars = {}
        #                                 012345678
        format_chars[rd.CompType.UInt]  = "xBHxIxxxL"
        format_chars[rd.CompType.SInt]  = "xbhxixxxl"
        format_chars[rd.CompType.Float] = "xxexfxxxd"

        format_chars[rd.CompType.UNorm] = format_chars[rd.CompType.UInt]
        format_chars[rd.CompType.UScaled] = format_chars[rd.CompType.UInt]
        format_chars[rd.CompType.SNorm] = format_chars[rd.CompType.SInt]
        format_chars[rd.CompType.SScaled] = format_chars[rd.CompType.SInt]

        comp_type = fmt.compType
        if comp_type not in format_chars:
            return (0.0,) * fmt.compCount

        char = format_chars[comp_type]
        if fmt.compByteWidth >= len(char) or char[fmt.compByteWidth] == "x":
            return (0.0,) * fmt.compCount

        unpack_fmt = str(fmt.compCount) + char[fmt.compByteWidth]

        end = offset + fmt.compByteWidth * fmt.compCount
        if end > len(vb_data):
            return (0.0,) * fmt.compCount

        value = struct.unpack_from(unpack_fmt, vb_data, offset)

        # Post-process normalised formats
        if comp_type == rd.CompType.UNorm:
            divisor = float((2 ** (fmt.compByteWidth * 8)) - 1)
            value = tuple(float(i) / divisor for i in value)
        elif comp_type == rd.CompType.SNorm:
            max_neg = -float(2 ** (fmt.compByteWidth * 8)) / 2
            divisor = float(-(max_neg - 1))
            value = tuple(
                (float(i) if (i == max_neg) else (float(i) / divisor))
                for i in value
            )

        if fmt.BGRAOrder():
            value = tuple(value[i] for i in [2, 1, 0, 3])

        return value
