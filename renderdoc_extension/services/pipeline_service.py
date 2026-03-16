"""
Pipeline state service for RenderDoc.
"""

import base64

import renderdoc as rd

from ..utils import Parsers, Serializers, Helpers
from .. import spirv_cross


class PipelineService:
    """Pipeline state service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    @staticmethod
    def _get_pipeline_object(pipe, stage):
        """Get pipeline object ID for a shader stage."""
        if stage == rd.ShaderStage.Compute:
            return pipe.GetComputePipelineObject()
        return pipe.GetGraphicsPipelineObject()

    # Preferred disassembly targets in priority order (case-insensitive substring match).
    PREFERRED_TARGETS = ["GLSL", "HLSL"]

    @staticmethod
    def _choose_disassembly_target(targets, requested=None):
        """Pick the best disassembly target string from *targets*.

        If *requested* is given, find the first target containing that
        substring (case-insensitive).  Otherwise walk PREFERRED_TARGETS and
        pick the first match; fall back to targets[0].

        Returns (chosen_target, available_list).
        """
        available = [str(t) for t in targets]
        if not available:
            return None, available

        if requested:
            req_lower = requested.lower()
            for t in available:
                if req_lower in t.lower():
                    return t, available
            return None, available

        for pref in PipelineService.PREFERRED_TARGETS:
            pref_lower = pref.lower()
            for t in available:
                if pref_lower in t.lower():
                    return t, available

        return available[0], available

    def get_shader_info(self, event_id, stage, disassembly_target=None):
        """Get shader information for a specific stage"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"shader": None, "error": None}
        _spirv = {}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            stage_enum = Parsers.parse_stage(stage)

            shader = pipe.GetShader(stage_enum)
            if shader == rd.ResourceId.Null():
                result["error"] = "No %s shader bound" % stage
                return

            entry = pipe.GetShaderEntryPoint(stage_enum)
            reflection = pipe.GetShaderReflection(stage_enum)

            shader_info = {
                "resource_id": str(shader),
                "entry_point": entry,
                "stage": stage,
            }

            # Get disassembly
            try:
                targets = controller.GetDisassemblyTargets(True)
                chosen, available = self._choose_disassembly_target(
                    targets, disassembly_target
                )
                shader_info["available_disassembly_targets"] = available

                if chosen is None and disassembly_target:
                    shader_info["disassembly_error"] = (
                        "Requested target '%s' not available. Available: %s"
                        % (disassembly_target, ", ".join(available))
                    )
                elif chosen is not None:
                    pipe_obj = self._get_pipeline_object(pipe, stage_enum)
                    disasm = controller.DisassembleShader(
                        pipe_obj, reflection, chosen
                    )
                    shader_info["disassembly"] = disasm
                    shader_info["disassembly_target"] = chosen
            except Exception as e:
                shader_info["disassembly_error"] = str(e)

            # Capture raw SPIR-V for potential spirv-cross fallback
            try:
                if (reflection
                        and hasattr(reflection, "encoding")
                        and reflection.encoding == rd.ShaderEncoding.SPIRV):
                    raw = reflection.rawBytes
                    if raw and spirv_cross.is_spirv(raw):
                        _spirv["raw"] = bytes(raw)
                        _spirv["entry"] = entry
            except Exception:
                pass

            # Get constant buffer info
            if reflection:
                shader_info["constant_buffers"] = self._get_cbuffer_info(
                    controller, pipe, reflection, stage_enum
                )
                shader_info["resources"] = self._get_resource_bindings(reflection)

            result["shader"] = shader_info

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])

        if _spirv.get("raw") and result["shader"]:
            self._apply_spirv_cross_fallback(
                result["shader"], _spirv, disassembly_target
            )

        return result["shader"]

    # ------------------------------------------------------------------ #
    #  spirv-cross fallback helpers                                       #
    # ------------------------------------------------------------------ #

    @classmethod
    def _spirv_cross_lang_needed(cls, shader_info, disassembly_target):
        """Determine if spirv-cross should be attempted and for which language.

        Returns the language key ("glsl"/"hlsl") or None.
        """
        if disassembly_target:
            if "disassembly_error" not in shader_info:
                return None
            return spirv_cross.parse_lang(disassembly_target)

        chosen = shader_info.get("disassembly_target", "")
        for pref in cls.PREFERRED_TARGETS:
            if pref.lower() in chosen.lower():
                return None
        return "glsl"

    @classmethod
    def _apply_spirv_cross_fallback(cls, shader_info, spirv_data, disassembly_target):
        """Try to improve disassembly output using spirv-cross."""
        available = shader_info.get("available_disassembly_targets", [])

        if spirv_cross.is_available():
            for tag in ("GLSL (spirv-cross)", "HLSL (spirv-cross)"):
                if tag not in available:
                    available.append(tag)
            shader_info["available_disassembly_targets"] = available

        lang = cls._spirv_cross_lang_needed(shader_info, disassembly_target)
        if not lang:
            return

        code, error = spirv_cross.decompile(
            spirv_data["raw"], lang, spirv_data.get("entry")
        )

        if code:
            shader_info["disassembly"] = code
            shader_info["disassembly_target"] = "%s (spirv-cross)" % lang.upper()
            shader_info.pop("disassembly_error", None)
        elif error and "disassembly_error" in shader_info:
            shader_info["disassembly_error"] += (
                "; spirv-cross fallback also failed: " + error
            )

    def get_pipeline_state(self, event_id):
        """Get full pipeline state at an event"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"pipeline": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            api = controller.GetAPIProperties().pipelineType

            pipeline_info = {
                "event_id": event_id,
                "api": str(api),
            }

            # Shader stages with detailed bindings
            stages = {}
            stage_list = Helpers.get_all_shader_stages()
            for stage in stage_list:
                shader = pipe.GetShader(stage)
                if shader != rd.ResourceId.Null():
                    stage_info = {
                        "resource_id": str(shader),
                        "entry_point": pipe.GetShaderEntryPoint(stage),
                    }

                    reflection = pipe.GetShaderReflection(stage)

                    stage_info["resources"] = self._get_stage_resources(
                        controller, pipe, stage, reflection
                    )
                    stage_info["uavs"] = self._get_stage_uavs(
                        controller, pipe, stage, reflection
                    )
                    stage_info["samplers"] = self._get_stage_samplers(
                        pipe, stage, reflection
                    )
                    stage_info["constant_buffers"] = self._get_stage_cbuffers(
                        controller, pipe, stage, reflection
                    )

                    stages[str(stage)] = stage_info

            pipeline_info["shaders"] = stages

            # Viewport and scissor
            try:
                vp_scissor = pipe.GetViewportScissor()
                if vp_scissor:
                    viewports = []
                    for v in vp_scissor.viewports:
                        viewports.append(
                            {
                                "x": v.x,
                                "y": v.y,
                                "width": v.width,
                                "height": v.height,
                                "min_depth": v.minDepth,
                                "max_depth": v.maxDepth,
                            }
                        )
                    pipeline_info["viewports"] = viewports
            except Exception:
                pass

            # Render targets
            try:
                om = pipe.GetOutputMerger()
                if om:
                    rts = []
                    for i, rt in enumerate(om.renderTargets):
                        if rt.resourceId != rd.ResourceId.Null():
                            rts.append({"index": i, "resource_id": str(rt.resourceId)})
                    pipeline_info["render_targets"] = rts

                    if om.depthTarget.resourceId != rd.ResourceId.Null():
                        pipeline_info["depth_target"] = str(om.depthTarget.resourceId)
            except Exception:
                pass

            # Input assembly
            try:
                ia = pipe.GetIAState()
                if ia:
                    pipeline_info["input_assembly"] = {"topology": str(ia.topology)}
            except Exception:
                pass

            result["pipeline"] = pipeline_info

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["pipeline"]

    def get_cbuffer_values(
        self,
        event_id,
        stage,
        cbuffer_name=None,
        cbuffer_index=None,
        include_raw_bytes=False,
    ):
        """Get actual values for one constant buffer at a specific event/stage."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            stage_enum = Parsers.parse_stage(stage)

            shader = pipe.GetShader(stage_enum)
            if shader == rd.ResourceId.Null():
                result["error"] = "No %s shader bound" % stage
                return

            reflection = pipe.GetShaderReflection(stage_enum)
            if not reflection:
                result["error"] = "Shader reflection unavailable for %s stage" % stage
                return

            if not reflection.constantBlocks:
                result["error"] = "No constant buffers found for %s stage" % stage
                return

            cb_idx = self._resolve_cbuffer_index(
                reflection, cbuffer_name=cbuffer_name, cbuffer_index=cbuffer_index
            )
            cb = reflection.constantBlocks[cb_idx]

            slot = cb.bindPoint if hasattr(cb, "bindPoint") else cb.fixedBindNumber

            bind = pipe.GetConstantBlock(stage_enum, cb_idx, 0)
            buffer_resource = bind.descriptor.resource
            buffer_offset = bind.descriptor.byteOffset
            buffer_size = bind.descriptor.byteSize

            if buffer_size is None or buffer_size <= 0:
                buffer_size = cb.byteSize
            if buffer_offset is None or buffer_offset < 0:
                buffer_offset = 0

            pipe_obj = self._get_pipeline_object(pipe, stage_enum)
            variables = controller.GetCBufferVariableContents(
                pipe_obj,
                reflection.resourceId,
                stage_enum,
                reflection.entryPoint,
                cb_idx,
                buffer_resource,
                int(buffer_offset),
                int(buffer_size),
            )

            data = {
                "event_id": event_id,
                "stage": stage,
                "cbuffer_name": cb.name,
                "cbuffer_index": cb_idx,
                "slot": slot,
                "byte_size": cb.byteSize,
                "buffer_backed": bool(getattr(cb, "bufferBacked", True)),
                "buffer_resource_id": (
                    str(buffer_resource)
                    if buffer_resource != rd.ResourceId.Null()
                    else None
                ),
                "byte_offset": int(buffer_offset),
                "bound_byte_size": int(buffer_size),
                "variables": Serializers.serialize_variables(variables),
            }

            if include_raw_bytes:
                if (
                    data["buffer_backed"]
                    and buffer_resource != rd.ResourceId.Null()
                    and int(buffer_size) > 0
                ):
                    raw_data = controller.GetBufferData(
                        buffer_resource, int(buffer_offset), int(buffer_size)
                    )
                    data["raw_bytes_base64"] = base64.b64encode(raw_data).decode("ascii")
                    data["raw_bytes_length"] = len(raw_data)
                else:
                    data["raw_bytes_base64"] = None
                    data["raw_bytes_length"] = 0

            result["data"] = data

        try:
            self._invoke(callback)
        except ValueError:
            raise
        except Exception as e:
            result["error"] = str(e)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def _get_stage_resources(self, controller, pipe, stage, reflection):
        """Get shader resource views (SRVs) for a stage"""
        resources = []
        try:
            srvs = pipe.GetReadOnlyResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readOnlyResources:
                    name_map[res.fixedBindNumber] = res.name

            for srv in srvs:
                if srv.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = srv.access.index
                res_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(srv.descriptor.resource),
                }

                res_info.update(
                    self._get_resource_details(controller, srv.descriptor.resource)
                )

                res_info["first_mip"] = srv.descriptor.firstMip
                res_info["num_mips"] = srv.descriptor.numMips
                res_info["first_slice"] = srv.descriptor.firstSlice
                res_info["num_slices"] = srv.descriptor.numSlices

                resources.append(res_info)
        except Exception as e:
            resources.append({"error": str(e)})

        return resources

    def _get_stage_uavs(self, controller, pipe, stage, reflection):
        """Get unordered access views (UAVs) for a stage"""
        uavs = []
        try:
            uav_list = pipe.GetReadWriteResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readWriteResources:
                    name_map[res.fixedBindNumber] = res.name

            for uav in uav_list:
                if uav.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = uav.access.index
                uav_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(uav.descriptor.resource),
                }

                uav_info.update(
                    self._get_resource_details(controller, uav.descriptor.resource)
                )

                uav_info["first_element"] = uav.descriptor.firstMip
                uav_info["num_elements"] = uav.descriptor.numMips

                uavs.append(uav_info)
        except Exception as e:
            uavs.append({"error": str(e)})

        return uavs

    def _get_stage_samplers(self, pipe, stage, reflection):
        """Get samplers for a stage"""
        samplers = []
        try:
            sampler_list = pipe.GetSamplers(stage, False)

            name_map = {}
            if reflection:
                for samp in reflection.samplers:
                    name_map[samp.fixedBindNumber] = samp.name

            for samp in sampler_list:
                slot = samp.access.index
                samp_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                }

                desc = samp.descriptor
                try:
                    samp_info["address_u"] = str(desc.addressU)
                    samp_info["address_v"] = str(desc.addressV)
                    samp_info["address_w"] = str(desc.addressW)
                except AttributeError:
                    pass

                try:
                    samp_info["filter"] = str(desc.filter)
                except AttributeError:
                    pass

                try:
                    samp_info["max_anisotropy"] = desc.maxAnisotropy
                except AttributeError:
                    pass

                try:
                    samp_info["min_lod"] = desc.minLOD
                    samp_info["max_lod"] = desc.maxLOD
                    samp_info["mip_lod_bias"] = desc.mipLODBias
                except AttributeError:
                    pass

                try:
                    samp_info["border_color"] = [
                        desc.borderColor[0],
                        desc.borderColor[1],
                        desc.borderColor[2],
                        desc.borderColor[3],
                    ]
                except (AttributeError, TypeError):
                    pass

                try:
                    samp_info["compare_function"] = str(desc.compareFunction)
                except AttributeError:
                    pass

                samplers.append(samp_info)
        except Exception as e:
            samplers.append({"error": str(e)})

        return samplers

    def _get_stage_cbuffers(self, controller, pipe, stage, reflection):
        """Get constant buffers for a stage from shader reflection"""
        cbuffers = []
        try:
            if not reflection:
                return cbuffers

            for cb in reflection.constantBlocks:
                slot = cb.bindPoint if hasattr(cb, 'bindPoint') else cb.fixedBindNumber
                cb_info = {
                    "slot": slot,
                    "name": cb.name,
                    "byte_size": cb.byteSize,
                    "variable_count": len(cb.variables) if cb.variables else 0,
                    "variables": [],
                }
                if cb.variables:
                    for var in cb.variables:
                        cb_info["variables"].append({
                            "name": var.name,
                            "byte_offset": var.byteOffset,
                            "type": str(var.type.name) if var.type else "",
                        })
                cbuffers.append(cb_info)

        except Exception as e:
            cbuffers.append({"error": str(e)})

        return cbuffers

    def _get_resource_details(self, controller, resource_id):
        """Get details about a resource (texture or buffer)"""
        details = {}

        try:
            resource_name = self.ctx.GetResourceName(resource_id)
            if resource_name:
                details["resource_name"] = resource_name
        except Exception:
            pass

        for tex in controller.GetTextures():
            if tex.resourceId == resource_id:
                details["type"] = "texture"
                details["width"] = tex.width
                details["height"] = tex.height
                details["depth"] = tex.depth
                details["array_size"] = tex.arraysize
                details["mip_levels"] = tex.mips
                details["format"] = str(tex.format.Name())
                details["dimension"] = str(tex.type)
                details["msaa_samples"] = tex.msSamp
                return details

        for buf in controller.GetBuffers():
            if buf.resourceId == resource_id:
                details["type"] = "buffer"
                details["length"] = buf.length
                return details

        return details

    def _get_cbuffer_info(self, controller, pipe, reflection, stage):
        """Get constant buffer information and values"""
        cbuffers = []

        for i, cb in enumerate(reflection.constantBlocks):
            cb_info = {
                "name": cb.name,
                "slot": i,
                "size": cb.byteSize,
                "variables": [],
            }

            try:
                bind = pipe.GetConstantBlock(stage, i, 0)
                buffer_resource = bind.descriptor.resource
                buffer_offset = bind.descriptor.byteOffset
                buffer_size = bind.descriptor.byteSize
                if buffer_resource != rd.ResourceId.Null():
                    pipe_obj = self._get_pipeline_object(pipe, stage)
                    # RenderDoc expects the bound byte range for sub-allocated buffers.
                    # Passing 0,0 can read the wrong region when CBV/Ubo binds a slice.
                    if buffer_size is None or buffer_size <= 0:
                        buffer_size = cb.byteSize
                    if buffer_offset is None or buffer_offset < 0:
                        buffer_offset = 0
                    variables = controller.GetCBufferVariableContents(
                        pipe_obj,
                        reflection.resourceId,
                        stage,
                        reflection.entryPoint,
                        i,
                        buffer_resource,
                        int(buffer_offset),
                        int(buffer_size),
                    )
                    cb_info["variables"] = Serializers.serialize_variables(variables)
            except Exception as e:
                cb_info["error"] = str(e)

            cbuffers.append(cb_info)

        return cbuffers

    def _resolve_cbuffer_index(self, reflection, cbuffer_name=None, cbuffer_index=None):
        """Resolve constant buffer index from name/index inputs."""
        if cbuffer_name is not None:
            for i, cb in enumerate(reflection.constantBlocks):
                if cb.name == cbuffer_name:
                    return i

            target_name = cbuffer_name.lower()
            for i, cb in enumerate(reflection.constantBlocks):
                if cb.name and cb.name.lower() == target_name:
                    return i

            raise ValueError("Constant buffer not found by name: %s" % cbuffer_name)

        if cbuffer_index is None:
            raise ValueError("Either cbuffer_name or cbuffer_index is required")

        idx = int(cbuffer_index)
        if idx < 0 or idx >= len(reflection.constantBlocks):
            raise ValueError(
                "cbuffer_index out of range: %d (available: 0-%d)"
                % (idx, len(reflection.constantBlocks) - 1)
            )
        return idx

    def _get_resource_bindings(self, reflection):
        """Get shader resource bindings"""
        resources = []

        try:
            for res in reflection.readOnlyResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadOnly",
                    }
                )
        except Exception:
            pass

        try:
            for res in reflection.readWriteResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadWrite",
                    }
                )
        except Exception:
            pass

        return resources
