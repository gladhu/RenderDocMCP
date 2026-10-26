"""
Request Handler for RenderDoc MCP Bridge
Routes incoming requests to appropriate facade methods.
"""

import traceback


class RequestHandler:
    """Handles incoming MCP bridge requests"""

    def __init__(self, facade):
        self.facade = facade
        self._methods = {
            "ping": self._handle_ping,
            "get_capture_status": self._handle_get_capture_status,
            "get_replay_context": self._handle_get_replay_context,
            "get_draw_calls": self._handle_get_draw_calls,
            "get_frame_summary": self._handle_get_frame_summary,
            "find_draws_by_shader": self._handle_find_draws_by_shader,
            "find_draws_by_texture": self._handle_find_draws_by_texture,
            "find_draws_by_resource": self._handle_find_draws_by_resource,
            "get_draw_call_details": self._handle_get_draw_call_details,
            "get_action_timings": self._handle_get_action_timings,
            "get_shader_info": self._handle_get_shader_info,
            "get_buffer_contents": self._handle_get_buffer_contents,
            "get_texture_info": self._handle_get_texture_info,
            "get_texture_data": self._handle_get_texture_data,
            "get_pipeline_state": self._handle_get_pipeline_state,
            "list_captures": self._handle_list_captures,
            "open_capture": self._handle_open_capture,
            "get_draw_coverage": self._handle_get_draw_coverage,
            "get_shader_usage": self._handle_get_shader_usage,
            "get_shader_sources": self._handle_get_shader_sources,
            "save_texture": self._handle_save_texture,
            "pick_pixel": self._handle_pick_pixel,
            "diagnose_mobile_risks": self._handle_diagnose_mobile_risks,
            "get_renderpass_list": self._handle_get_renderpass_list,
            "get_pass_detail": self._handle_get_pass_detail,
            "list_frame_textures": self._handle_list_frame_textures,
            "get_texture_detail": self._handle_get_texture_detail,
            "pixel_history": self._handle_pixel_history,
            "read_texture_stats": self._handle_read_texture_stats,
            "sample_pixel_region": self._handle_sample_pixel_region,
            "get_resource_usage": self._handle_get_resource_usage,
            "list_shaders": self._handle_list_shaders,
            "analyze_state_changes": self._handle_analyze_state_changes,
            "get_post_vs_data": self._handle_get_post_vs_data,
            "export_mesh": self._handle_export_mesh,
        }

    def handle(self, request):
        """Handle a request and return response"""
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        try:
            if method not in self._methods:
                return self._error_response(
                    request_id, -32601, "Method not found: %s" % method
                )

            result = self._methods[method](params)
            return {"id": request_id, "result": result}

        except ValueError as e:
            return self._error_response(request_id, -32602, str(e))
        except Exception as e:
            traceback.print_exc()
            return self._error_response(request_id, -32000, str(e))

    def _error_response(self, request_id, code, message):
        """Create an error response"""
        return {"id": request_id, "error": {"code": code, "message": message}}

    def _handle_ping(self, params):
        """Handle ping request"""
        return {"status": "ok", "message": "pong"}

    def _handle_get_capture_status(self, params):
        """Handle get_capture_status request"""
        return self.facade.get_capture_status()

    def _handle_get_replay_context(self, params):
        """Handle get_replay_context request"""
        return self.facade.get_replay_context()

    def _handle_get_draw_calls(self, params):
        """Handle get_draw_calls request"""
        include_children = params.get("include_children", True)
        marker_filter = params.get("marker_filter")
        exclude_markers = params.get("exclude_markers")
        event_id_min = params.get("event_id_min")
        event_id_max = params.get("event_id_max")
        only_actions = params.get("only_actions", False)
        flags_filter = params.get("flags_filter")
        return self.facade.get_draw_calls(
            include_children=include_children,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            only_actions=only_actions,
            flags_filter=flags_filter,
        )

    def _handle_get_frame_summary(self, params):
        """Handle get_frame_summary request"""
        return self.facade.get_frame_summary()

    def _handle_find_draws_by_shader(self, params):
        """Handle find_draws_by_shader request"""
        shader_name = params.get("shader_name")
        if shader_name is None:
            raise ValueError("shader_name is required")
        stage = params.get("stage")
        return self.facade.find_draws_by_shader(shader_name, stage)

    def _handle_find_draws_by_texture(self, params):
        """Handle find_draws_by_texture request"""
        texture_name = params.get("texture_name")
        if texture_name is None:
            raise ValueError("texture_name is required")
        return self.facade.find_draws_by_texture(texture_name)

    def _handle_find_draws_by_resource(self, params):
        """Handle find_draws_by_resource request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        return self.facade.find_draws_by_resource(resource_id)

    def _handle_get_draw_call_details(self, params):
        """Handle get_draw_call_details request"""
        event_id = params.get("event_id")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_draw_call_details(int(event_id))

    def _handle_get_action_timings(self, params):
        """Handle get_action_timings request"""
        event_ids = params.get("event_ids")
        marker_filter = params.get("marker_filter")
        exclude_markers = params.get("exclude_markers")
        return self.facade.get_action_timings(
            event_ids=event_ids,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
        )

    def _handle_get_shader_info(self, params):
        """Handle get_shader_info request"""
        event_id = params.get("event_id")
        stage = params.get("stage")
        if event_id is None:
            raise ValueError("event_id is required")
        if stage is None:
            raise ValueError("stage is required")
        return self.facade.get_shader_info(int(event_id), stage)

    def _handle_get_buffer_contents(self, params):
        """Handle get_buffer_contents request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        offset = params.get("offset", 0)
        length = params.get("length", 0)
        return self.facade.get_buffer_contents(resource_id, offset, length)

    def _handle_get_texture_info(self, params):
        """Handle get_texture_info request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        return self.facade.get_texture_info(resource_id)

    def _handle_get_texture_data(self, params):
        """Handle get_texture_data request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        mip = params.get("mip", 0)
        slice_idx = params.get("slice", 0)
        sample = params.get("sample", 0)
        depth_slice = params.get("depth_slice")  # None = full volume
        return self.facade.get_texture_data(resource_id, mip, slice_idx, sample, depth_slice)

    def _handle_get_pipeline_state(self, params):
        """Handle get_pipeline_state request"""
        event_id = params.get("event_id")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_pipeline_state(int(event_id))

    def _handle_list_captures(self, params):
        """Handle list_captures request"""
        directory = params.get("directory")
        if directory is None:
            raise ValueError("directory is required")
        return self.facade.list_captures(directory)

    def _handle_open_capture(self, params):
        """Handle open_capture request"""
        capture_path = params.get("capture_path")
        if capture_path is None:
            raise ValueError("capture_path is required")
        use_remote = params.get("use_remote")  # True/False/None
        wait = params.get("wait", True)
        return self.facade.open_capture(capture_path, use_remote=use_remote, wait=wait)

    def _handle_get_draw_coverage(self, params):
        """Handle get_draw_coverage request"""
        return self.facade.get_draw_coverage(
            event_ids=params.get("event_ids"),
            marker_filter=params.get("marker_filter"),
            event_id_min=params.get("event_id_min"),
            event_id_max=params.get("event_id_max"),
        )

    def _handle_get_shader_usage(self, params):
        """Handle get_shader_usage request"""
        return self.facade.get_shader_usage(
            marker_filter=params.get("marker_filter"),
            event_id_min=params.get("event_id_min"),
            event_id_max=params.get("event_id_max"),
        )

    def _handle_get_shader_sources(self, params):
        """Handle get_shader_sources request"""
        return self.facade.get_shader_sources(
            event_ids=params.get("event_ids"),
            marker_filter=params.get("marker_filter"),
            event_id_min=params.get("event_id_min"),
            event_id_max=params.get("event_id_max"),
            stage=params.get("stage", "pixel"),
            fold_uniforms=params.get("fold_uniforms", True),
            fold_blocks=params.get("fold_blocks"),
            overrides=params.get("overrides"),
        )

    def _handle_save_texture(self, params):
        """Handle save_texture request"""
        output_path = params.get("output_path")
        if output_path is None:
            raise ValueError("output_path is required")
        return self.facade.save_texture(
            output_path,
            event_id=params.get("event_id"),
            resource_id=params.get("resource_id"),
            target=params.get("target", "color0"),
            file_type=params.get("file_type", "png"),
        )

    def _handle_pick_pixel(self, params):
        """Handle pick_pixel request"""
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            raise ValueError("x and y are required")
        return self.facade.pick_pixel(
            x, y,
            event_id=params.get("event_id"),
            resource_id=params.get("resource_id"),
            target=params.get("target", "color0"),
        )

    def _handle_diagnose_mobile_risks(self, params):
        """Handle diagnose_mobile_risks request"""
        return self.facade.diagnose_mobile_risks(
            check_categories=params.get("check_categories"),
            severity_filter=params.get("severity_filter", "all"),
        )

    def _handle_get_renderpass_list(self, params):
        """Handle get_renderpass_list request"""
        return self.facade.get_renderpass_list(sort_by=params.get("sort_by", "index"))

    def _handle_get_pass_detail(self, params):
        """Handle get_pass_detail request"""
        pass_index = params.get("pass_index")
        if pass_index is None:
            raise ValueError("pass_index is required")
        return self.facade.get_pass_detail(int(pass_index))

    def _handle_list_frame_textures(self, params):
        """Handle list_frame_textures request"""
        return self.facade.list_frame_textures(
            sort_by=params.get("sort_by", "size"),
            top_n=params.get("top_n", 50),
            filter_fmt=params.get("filter_fmt"),
            min_width=params.get("min_width"),
            rt_only=params.get("rt_only", False),
        )

    def _handle_get_texture_detail(self, params):
        """Handle get_texture_detail request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        return self.facade.get_texture_detail(resource_id)

    def _handle_pixel_history(self, params):
        """Handle pixel_history request"""
        x, y = params.get("x"), params.get("y")
        if x is None or y is None:
            raise ValueError("x and y are required")
        return self.facade.pixel_history(
            x, y,
            event_id=params.get("event_id"),
            resource_id=params.get("resource_id"),
            target=params.get("target", "color0"),
            max_entries=params.get("max_entries", 200),
        )

    def _handle_read_texture_stats(self, params):
        """Handle read_texture_stats request"""
        return self.facade.read_texture_stats(
            event_id=params.get("event_id"),
            resource_id=params.get("resource_id"),
            target=params.get("target", "color0"),
        )

    def _handle_sample_pixel_region(self, params):
        """Handle sample_pixel_region request"""
        return self.facade.sample_pixel_region(
            event_id=params.get("event_id"),
            resource_id=params.get("resource_id"),
            target=params.get("target", "color0"),
            x=params.get("x", 0),
            y=params.get("y", 0),
            width=params.get("width"),
            height=params.get("height"),
            sample_count=params.get("sample_count", 256),
            anomaly_threshold=params.get("anomaly_threshold", 10.0),
        )

    def _handle_get_resource_usage(self, params):
        """Handle get_resource_usage request"""
        resource_id = params.get("resource_id")
        if resource_id is None:
            raise ValueError("resource_id is required")
        return self.facade.get_resource_usage(resource_id)

    def _handle_list_shaders(self, params):
        """Handle list_shaders request"""
        return self.facade.list_shaders(
            stage=params.get("stage"),
            top_n=params.get("top_n", 50),
        )

    def _handle_analyze_state_changes(self, params):
        """Handle analyze_state_changes request"""
        return self.facade.analyze_state_changes(
            marker_filter=params.get("marker_filter"),
            change_types=params.get("change_types"),
            max_draws=params.get("max_draws", 200),
        )

    def _handle_get_post_vs_data(self, params):
        """Handle get_post_vs_data request"""
        event_id = params.get("event_id")
        if event_id is None:
            raise ValueError("event_id is required")
        return self.facade.get_post_vs_data(
            int(event_id),
            stage=params.get("stage", "vsout"),
            max_vertices=params.get("max_vertices", 100),
        )

    def _handle_export_mesh(self, params):
        """Handle export_mesh request"""
        event_id = params.get("event_id")
        output_path = params.get("output_path")
        if event_id is None or output_path is None:
            raise ValueError("event_id and output_path are required")
        return self.facade.export_mesh(int(event_id), output_path)
