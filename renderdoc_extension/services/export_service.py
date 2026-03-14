"""
Export service for RenderDoc MCP Bridge.
Exports textures/shaders/meshes to files, serving them via HTTP.

Compatible with Python 3.6 (no f-strings).
"""

import os
import struct
import time

import renderdoc as rd

from ..utils import Parsers


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

    def export_texture(self, resource_id, event_id, mip=0, slice_index=0):
        """Export a texture to PNG file and return download URL."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._ensure_export_dir()

        numeric_id = Parsers.extract_numeric_id(resource_id)
        tag = self._get_capture_tag()
        filename = "%s_tex_%d_eid%d_mip%d.png" % (tag, numeric_id, event_id, mip)
        output_path = os.path.join(self.export_dir, filename)

        result = {"data": None, "error": None}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                # Find the texture resource
                target_id = numeric_id
                tex_rid = None
                for tex in controller.GetTextures():
                    tex_id = Parsers.extract_numeric_id(str(tex.resourceId))
                    if tex_id == target_id:
                        tex_rid = tex.resourceId
                        break

                if tex_rid is None:
                    result["error"] = "Texture not found: %s" % resource_id
                    return

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
                }
            except Exception as e:
                import traceback
                result["error"] = "Export failed: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    # ======================== Shader Export ========================

    def export_shader(self, event_id, stage):
        """Export bound shader disassembly to text file and return download URL."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._ensure_export_dir()

        tag = self._get_capture_tag()
        stage_name = str(stage).lower()
        filename = "%s_shader_%s_eid%d.txt" % (tag, stage_name, event_id)
        output_path = os.path.join(self.export_dir, filename)

        result = {"data": None, "error": None}

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

                pipe_obj = pipe.GetGraphicsPipelineObject()
                if stage_enum == rd.ShaderStage.Compute:
                    try:
                        pipe_obj = pipe.GetComputePipelineObject()
                    except Exception:
                        pass

                disasm = controller.DisassembleShader(pipe_obj, reflection, targets[0])
                if not disasm:
                    result["error"] = "Shader disassembly is empty"
                    return

                with open(output_path, "w") as f:
                    f.write("// Exported from RenderDoc MCP\n")
                    f.write("// event_id: %d\n" % event_id)
                    f.write("// stage: %s\n" % stage_name)
                    f.write("// resource_id: %s\n\n" % str(shader))
                    f.write(disasm)

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
                    "disassembly_target": str(targets[0]),
                    "format": "txt",
                }
            except Exception as e:
                import traceback
                result["error"] = "Shader export failed: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    # ======================== Mesh Export ========================

    def export_mesh(self, event_id):
        """Export mesh at a draw call to OBJ file and return download URL."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        self._ensure_export_dir()

        tag = self._get_capture_tag()
        filename = "%s_mesh_eid%d.obj" % (tag, event_id)
        output_path = os.path.join(self.export_dir, filename)

        result = {"data": None, "error": None}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

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

                print("[ExportMesh] eid=%d, %d vertex attrs: %s"
                      % (event_id, len(attrs),
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
                    f.write("\n")

                    for p in positions:
                        if len(p) >= 3:
                            f.write("v %s %s %s\n" % (p[0], p[1], p[2]))
                        elif len(p) == 2:
                            f.write("v %s %s 0\n" % (p[0], p[1]))

                    if has_texcoords:
                        f.write("\n")
                        for t in texcoords:
                            if len(t) >= 2:
                                f.write("vt %s %s\n" % (t[0], t[1]))
                            elif len(t) == 1:
                                f.write("vt %s 0\n" % t[0])

                    if has_normals:
                        f.write("\n")
                        for n in normals:
                            if len(n) >= 3:
                                f.write("vn %s %s %s\n" % (n[0], n[1], n[2]))

                    f.write("\n")
                    for i in range(0, len(indices) - 2, 3):
                        # OBJ indices are 1-based
                        i0 = index_remap[indices[i]] + 1
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
