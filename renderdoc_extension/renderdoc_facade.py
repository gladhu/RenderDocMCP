"""
RenderDoc API Facade
Provides thread-safe access to RenderDoc's ReplayController and CaptureContext.
Uses BlockInvoke to marshal calls to the replay thread.
"""

from .services import (
    CaptureManager,
    ActionService,
    SearchService,
    ResourceService,
    PipelineService,
    AnalysisService,
    DiagnosisService,
    PassService,
)


class RenderDocFacade:
    """
    Facade for RenderDoc API access.

    This class delegates all operations to specialized service classes:
    - CaptureManager: Capture management (status, list, open)
    - ActionService: Draw call / action operations
    - SearchService: Reverse lookup searches
    - ResourceService: Texture and buffer data
    - PipelineService: Pipeline state and shader info
    - AnalysisService: Coverage, shader sources, texture export
    - DiagnosisService: Mobile / TBR heuristic risks
    - PassService: Render-pass enumeration and detail
    """

    def __init__(self, ctx):
        """
        Initialize facade with CaptureContext.

        Args:
            ctx: The pyrenderdoc CaptureContext from register()
        """
        self.ctx = ctx

        # Initialize service classes
        self._capture = CaptureManager(ctx, self._invoke)
        self._action = ActionService(ctx, self._invoke)
        self._search = SearchService(ctx, self._invoke)
        self._resource = ResourceService(ctx, self._invoke)
        self._pipeline = PipelineService(ctx, self._invoke)
        self._analysis = AnalysisService(ctx, self._invoke)
        self._diagnosis = DiagnosisService(ctx, self._invoke)
        self._pass = PassService(ctx, self._invoke)

    def _invoke(self, callback):
        """Invoke callback on replay thread via BlockInvoke"""
        self.ctx.Replay().BlockInvoke(callback)

    # ==================== Capture Management ====================

    def get_capture_status(self):
        """Check if a capture is loaded and get API info"""
        return self._capture.get_capture_status()

    def get_replay_context(self):
        """Get the current replay context including remote device status"""
        return self._capture.get_replay_context()

    def list_captures(self, directory):
        """List all .rdc files in the specified directory"""
        return self._capture.list_captures(directory)

    def open_capture(self, capture_path, use_remote=None):
        """Open a capture file in RenderDoc, with optional remote replay support"""
        return self._capture.open_capture(capture_path, use_remote=use_remote)

    # ==================== Draw Call / Action Operations ====================

    def get_draw_calls(
        self,
        include_children=True,
        marker_filter=None,
        exclude_markers=None,
        event_id_min=None,
        event_id_max=None,
        only_actions=False,
        flags_filter=None,
    ):
        """Get all draw calls/actions in the capture with optional filtering"""
        return self._action.get_draw_calls(
            include_children=include_children,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            only_actions=only_actions,
            flags_filter=flags_filter,
        )

    def get_frame_summary(self):
        """Get a summary of the current capture frame"""
        return self._action.get_frame_summary()

    def get_draw_call_details(self, event_id):
        """Get detailed information about a specific draw call"""
        return self._action.get_draw_call_details(event_id)

    def get_action_timings(self, event_ids=None, marker_filter=None, exclude_markers=None):
        """Get GPU timing information for actions"""
        return self._action.get_action_timings(
            event_ids=event_ids,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
        )

    # ==================== Search Operations ====================

    def find_draws_by_shader(self, shader_name, stage=None):
        """Find all draw calls using a shader with the given name (partial match)"""
        return self._search.find_draws_by_shader(shader_name, stage)

    def find_draws_by_texture(self, texture_name):
        """Find all draw calls using a texture with the given name (partial match)"""
        return self._search.find_draws_by_texture(texture_name)

    def find_draws_by_resource(self, resource_id):
        """Find all draw calls using a specific resource ID (exact match)"""
        return self._search.find_draws_by_resource(resource_id)

    # ==================== Resource Operations ====================

    def get_buffer_contents(self, resource_id, offset=0, length=0):
        """Get buffer data"""
        return self._resource.get_buffer_contents(resource_id, offset, length)

    def get_texture_info(self, resource_id):
        """Get texture metadata"""
        return self._resource.get_texture_info(resource_id)

    def get_texture_data(self, resource_id, mip=0, slice=0, sample=0, depth_slice=None):
        """Get texture pixel data"""
        return self._resource.get_texture_data(resource_id, mip, slice, sample, depth_slice)

    def list_frame_textures(self, sort_by="size", top_n=50, filter_fmt=None, min_width=None, rt_only=False):
        """Frame-level texture inventory"""
        return self._resource.list_frame_textures(sort_by, top_n, filter_fmt, min_width, rt_only)

    def get_texture_detail(self, resource_id):
        """Texture metadata plus usage events"""
        return self._resource.get_texture_detail(resource_id)

    def get_resource_usage(self, resource_id):
        """Usage history of a texture or buffer"""
        return self._resource.get_resource_usage(resource_id)

    # ==================== Pipeline Operations ====================

    def get_shader_info(self, event_id, stage):
        """Get shader information for a specific stage"""
        return self._pipeline.get_shader_info(event_id, stage)

    def get_pipeline_state(self, event_id):
        """Get full pipeline state at an event"""
        return self._pipeline.get_pipeline_state(event_id)

    # ==================== Analysis Operations ====================

    def get_draw_coverage(self, event_ids=None, marker_filter=None, event_id_min=None, event_id_max=None):
        """Per-draw depth-test pixel coverage"""
        return self._analysis.get_draw_coverage(event_ids, marker_filter, event_id_min, event_id_max)

    def get_shader_usage(self, marker_filter=None, event_id_min=None, event_id_max=None):
        """Group draw calls by pixel shader"""
        return self._analysis.get_shader_usage(marker_filter, event_id_min, event_id_max)

    def list_shaders(self, stage=None, top_n=50):
        """Unique shaders with draw counts"""
        return self._analysis.list_shaders(stage, top_n)

    def get_shader_sources(self, event_ids=None, marker_filter=None, event_id_min=None, event_id_max=None,
                           stage="pixel", fold_uniforms=True, fold_blocks=None, overrides=None):
        """Shader sources for the selected draws, optionally with material uniforms folded"""
        return self._analysis.get_shader_sources(event_ids, marker_filter, event_id_min, event_id_max,
                                                 stage, fold_uniforms, fold_blocks, overrides)

    def save_texture(self, output_path, event_id=None, resource_id=None, target="color0", file_type="png"):
        """Save a texture or an event's render target to a local file"""
        return self._analysis.save_texture(output_path, event_id, resource_id, target, file_type)

    def pick_pixel(self, x, y, event_id=None, resource_id=None, target="color0"):
        """Read a single pixel"""
        return self._analysis.pick_pixel(x, y, event_id, resource_id, target)

    def pixel_history(self, x, y, event_id=None, resource_id=None, target="color0", max_entries=200):
        """Pixel modification history"""
        return self._analysis.pixel_history(x, y, event_id, resource_id, target, max_entries)

    def read_texture_stats(self, event_id=None, resource_id=None, target="color0"):
        """GPU min/max with NaN/Inf/negative flags"""
        return self._analysis.read_texture_stats(event_id, resource_id, target)

    def sample_pixel_region(self, event_id=None, resource_id=None, target="color0",
                            x=0, y=0, width=None, height=None, sample_count=256, anomaly_threshold=10.0):
        """Grid-sample a region for anomalies"""
        return self._analysis.sample_pixel_region(
            event_id, resource_id, target, x, y, width, height, sample_count, anomaly_threshold
        )

    def get_post_vs_data(self, event_id, stage="vsout", max_vertices=100):
        """Post-transform vertex data"""
        return self._analysis.get_post_vs_data(event_id, stage, max_vertices)

    def export_mesh(self, event_id, output_path):
        """Export post-VS mesh as OBJ"""
        return self._analysis.export_mesh(event_id, output_path)

    def diagnose_mobile_risks(self, check_categories=None, severity_filter="all"):
        """Heuristic mobile / TBR risk scan"""
        return self._diagnosis.diagnose_mobile_risks(check_categories, severity_filter)

    def analyze_state_changes(self, marker_filter=None, change_types=None, max_draws=200):
        """PSO / RT / shader change scan"""
        return self._diagnosis.analyze_state_changes(marker_filter, change_types, max_draws)

    def get_renderpass_list(self, sort_by="index"):
        """Enumerate render passes"""
        return self._pass.get_renderpass_list(sort_by)

    def get_pass_detail(self, pass_index):
        """Draws and attachments of one pass"""
        return self._pass.get_pass_detail(pass_index)
