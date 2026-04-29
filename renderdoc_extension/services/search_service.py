"""
Reverse lookup search service for RenderDoc.
"""

import renderdoc as rd

from ..utils import Parsers, Helpers


class SearchService:
    """Reverse lookup search service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def _search_draws(self, matcher_fn):
        """
        Common template for searching draw calls.

        Args:
            matcher_fn: Function(pipe, controller, action, ctx) -> match_reason or None
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"matches": [], "scanned_draws": 0}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            all_actions = Helpers.flatten_actions(root_actions)

            # Filter to only draw calls and dispatches
            draw_actions = [
                a for a in all_actions
                if a.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch)
            ]
            result["scanned_draws"] = len(draw_actions)

            for action in draw_actions:
                controller.SetFrameEvent(action.eventId, False)
                pipe = controller.GetPipelineState()

                match_reason = matcher_fn(pipe, controller, action, self.ctx)
                if match_reason:
                    result["matches"].append({
                        "event_id": action.eventId,
                        "name": action.GetName(structured_file),
                        "match_reason": match_reason,
                    })

        self._invoke(callback)
        result["total_matches"] = len(result["matches"])
        return result

    def find_draws_by_shader(self, shader_name, stage=None):
        """Find all draw calls using a shader with the given name (partial match)."""
        # Determine which stages to check
        if stage:
            stages_to_check = [Parsers.parse_stage(stage)]
        else:
            stages_to_check = Helpers.get_all_shader_stages()

        def matcher(pipe, controller, action, ctx):
            for s in stages_to_check:
                shader = pipe.GetShader(s)
                if shader == rd.ResourceId.Null():
                    continue

                reflection = pipe.GetShaderReflection(s)
                if reflection:
                    entry_point = pipe.GetShaderEntryPoint(s)
                    shader_debug_name = ""
                    try:
                        shader_debug_name = ctx.GetResourceName(shader)
                    except Exception:
                        pass

                    if shader_name.lower() in entry_point.lower():
                        return "%s entry_point: '%s'" % (str(s), entry_point)
                    elif shader_debug_name and shader_name.lower() in shader_debug_name.lower():
                        return "%s name: '%s'" % (str(s), shader_debug_name)
            return None

        return self._search_draws(matcher)

    def find_draws_by_texture(self, texture_name):
        """Find all draw calls using a texture with the given name (partial match)."""
        stages_to_check = Helpers.get_all_shader_stages()

        def matcher(pipe, controller, action, ctx):
            # Check SRVs (read-only resources)
            for stage in stages_to_check:
                try:
                    srvs = pipe.GetReadOnlyResources(stage, False)
                    for srv in srvs:
                        if srv.descriptor.resource == rd.ResourceId.Null():
                            continue
                        res_name = ""
                        try:
                            res_name = ctx.GetResourceName(srv.descriptor.resource)
                        except Exception:
                            pass
                        if res_name and texture_name.lower() in res_name.lower():
                            return "%s SRV: '%s'" % (str(stage), res_name)
                except Exception:
                    pass

                # Check UAVs (read-write resources)
                try:
                    uavs = pipe.GetReadWriteResources(stage, False)
                    for uav in uavs:
                        if uav.descriptor.resource == rd.ResourceId.Null():
                            continue
                        res_name = ""
                        try:
                            res_name = ctx.GetResourceName(uav.descriptor.resource)
                        except Exception:
                            pass
                        if res_name and texture_name.lower() in res_name.lower():
                            return "%s UAV: '%s'" % (str(stage), res_name)
                except Exception:
                    pass

            # Check render targets
            try:
                om = pipe.GetOutputMerger()
                if om:
                    for i, rt in enumerate(om.renderTargets):
                        if rt.resourceId != rd.ResourceId.Null():
                            res_name = ""
                            try:
                                res_name = ctx.GetResourceName(rt.resourceId)
                            except Exception:
                                pass
                            if res_name and texture_name.lower() in res_name.lower():
                                return "RenderTarget[%d]: '%s'" % (i, res_name)
            except Exception:
                pass

            return None

        return self._search_draws(matcher)

    # ResourceUsage enum classification for usage_filter (computed lazily; the
    # enum class is in renderdoc and we keep these as strings to be robust to
    # additions).
    _WRITE_USAGE_NAMES = frozenset([
        "VS_RWResource", "HS_RWResource", "DS_RWResource", "GS_RWResource",
        "PS_RWResource", "CS_RWResource", "TS_RWResource", "MS_RWResource",
        "All_RWResource",
        "ColorTarget", "DepthStencilTarget", "StreamOut",
        "Clear", "Discard", "GenMips",
        "Resolve", "ResolveDst",
        "Copy", "CopyDst",
        "CPUWrite",
    ])
    _READ_USAGE_NAMES = frozenset([
        "VertexBuffer", "IndexBuffer",
        "VS_Constants", "HS_Constants", "DS_Constants", "GS_Constants",
        "PS_Constants", "CS_Constants", "TS_Constants", "MS_Constants",
        "All_Constants",
        "VS_Resource", "HS_Resource", "DS_Resource", "GS_Resource",
        "PS_Resource", "CS_Resource", "TS_Resource", "MS_Resource",
        "All_Resource",
        "InputTarget", "Indirect",
        "ResolveSrc", "CopySrc",
    ])

    @classmethod
    def _classify_usage(cls, usage_enum):
        # ResourceUsage prints as e.g. "ResourceUsage.ColorTarget" or just the
        # member name depending on bindings; normalise via str().split('.')[-1].
        # Anything not explicitly read/write (Barrier, Unused, future enum
        # values) returns "other" and is dropped under usage_filter="read"/
        # "write". `Resolve` is treated as write since it produces a
        # resolved attachment image; `ResolveSrc`/`ResolveDst` are explicit.
        name = str(usage_enum).split(".")[-1]
        if name in cls._WRITE_USAGE_NAMES:
            return "write"
        if name in cls._READ_USAGE_NAMES:
            return "read"
        return "other"

    def find_draws_by_resource(self, resource_id, usage_filter=None):
        """Find all events using a specific resource ID (exact match).

        Uses controller.GetUsage() — a capture-wide query that does not require
        SetFrameEvent per event. Much faster than scanning every draw call.

        Args:
            resource_id: Target resource ID (string or int).
            usage_filter: Optional. "read", "write", or "any"/None. Filters
                events by whether the resource is read or written.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        target_rid = Parsers.parse_resource_id(resource_id)
        if usage_filter is not None:
            usage_filter = str(usage_filter).lower()
            if usage_filter == "any":
                usage_filter = None
            elif usage_filter not in ("read", "write"):
                raise ValueError(
                    "usage_filter must be 'read', 'write', 'any', or omitted"
                )

        result = {
            "matches": [],
            "resource_id": str(target_rid),
            "scanned_events": 0,
        }

        def callback(controller):
            usages = controller.GetUsage(target_rid)
            result["scanned_events"] = len(usages)
            structured_file = controller.GetStructuredFile()

            # Build eventId -> action name map from the action tree (single pass).
            root_actions = controller.GetRootActions()
            all_actions = Helpers.flatten_actions(root_actions)
            name_by_eid = {}
            for a in all_actions:
                name_by_eid[a.eventId] = a.GetName(structured_file)

            for u in usages:
                cls = self._classify_usage(u.usage)
                if usage_filter and cls != usage_filter:
                    continue

                usage_name = str(u.usage).split(".")[-1]
                match = {
                    "event_id": int(u.eventId),
                    "name": name_by_eid.get(int(u.eventId), ""),
                    "usage": usage_name,
                    "access": cls,
                }
                if u.view != rd.ResourceId.Null():
                    match["view"] = str(u.view)
                result["matches"].append(match)

        self._invoke(callback)
        result["total_matches"] = len(result["matches"])
        return result
