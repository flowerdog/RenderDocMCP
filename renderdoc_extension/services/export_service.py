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

    def export_mesh(self, event_id, flip_uv_v=None, flip_handedness=None):
        """Export mesh at a draw call to OBJ file and return download URL.

        Args:
            event_id: The event ID of the draw call.
            flip_uv_v: Flip V texcoord (1-v) for OBJ convention.
                None = auto-detect from graphics API (flip for Vulkan/D3D, keep for OpenGL).
                True = always flip. False = never flip.
            flip_handedness: Convert left-hand to right-hand for OBJ (negate X + reverse winding).
                None = auto-detect from graphics API (flip for Vulkan/D3D, keep for OpenGL).
                True = always flip. False = never flip.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._ensure_export_dir()

        tag = self._get_capture_tag()
        filename = "%s_mesh_eid%d.obj" % (tag, event_id)
        output_path = os.path.join(self.export_dir, filename)

        result = {"data": None, "error": None}
        opts = {"flip_uv_v": flip_uv_v, "flip_hand": flip_handedness}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                # Auto-detect coordinate conventions from graphics API
                api = controller.GetAPIProperties().pipelineType
                is_opengl = (api == rd.GraphicsAPI.OpenGL)

                do_flip_uv = opts["flip_uv_v"]
                if do_flip_uv is None:
                    do_flip_uv = not is_opengl

                do_flip_hand = opts["flip_hand"]
                if do_flip_hand is None:
                    do_flip_hand = not is_opengl

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

                print("[ExportMesh] eid=%d, api=%s, flip_uv=%s, flip_hand=%s, %d vertex attrs: %s"
                      % (event_id, str(api), do_flip_uv, do_flip_hand, len(attrs),
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
                    "format": "obj",
                }
            except Exception as e:
                import traceback
                result["error"] = "Mesh export failed: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        if result["error"]:
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
