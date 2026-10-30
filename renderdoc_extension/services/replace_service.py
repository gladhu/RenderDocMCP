"""
Shader replacement: edit a shader, rebuild it on the replay device and replay the capture with it.

BuildTargetShader compiles on the replay side (the phone's own driver for remote replay, so compile errors are the
device's) and ReplaceResource makes every later replay and analysis use the new shader in place of the original,
for every draw that uses it. That includes get_gpu_counters, render_overlay, diff_textures, pixel reads...: change
the shader, then measure. The replacement stays until revert_shader (or until another capture is opened).

Sources: GL / GLES shaders carry their GLSL text; Vulkan shaders only SPIR-V, which can be replaced by another
SPIR-V binary (base64) or by Vulkan GLSL that matches the shader's interface.
"""

import base64
import difflib
import hashlib

import renderdoc as rd

from ..utils import Helpers, Parsers

STAGES = ("vertex", "hull", "domain", "geometry", "pixel", "compute")
ENC_GLSL, ENC_SPIRV = "glsl", "spirv"


# ---------------------------------------------------------------- pure helpers

def apply_edits(source, edits):
    """
    Apply [{"find": text, "replace": text}] in order and return (new source, [occurrences replaced per edit]).
    Every occurrence is replaced; an edit whose text is not in the source is an error, naming the edit.
    """
    counts = []
    for i, edit in enumerate(edits or []):
        if not isinstance(edit, dict) or "find" not in edit or "replace" not in edit:
            raise ValueError("edit %d must be {\"find\": ..., \"replace\": ...}" % i)
        find, repl = str(edit["find"]), str(edit["replace"])
        if not find:
            raise ValueError("edit %d has an empty find text" % i)
        n = source.count(find)
        if n == 0:
            raise ValueError("edit %d: text not found in the shader source: %r" % (i, find[:80]))
        source = source.replace(find, repl)
        counts.append(n)
    return source, counts


def change_summary(old, new, max_lines=60):
    """Unified diff of two sources, cut at max_lines"""
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "original", "replacement", lineterm="", n=2))
    cut = len(lines) > max_lines
    return {"changed": old != new, "diff": lines[:max_lines], "diff_truncated": cut, "diff_lines": len(lines)}


def pick_encoding(requested, original_kind):
    """"glsl" / "spirv" for the replacement, from the request or else from what the original shader is"""
    req = (requested or "auto").lower()
    if req in (ENC_GLSL, ENC_SPIRV):
        return req
    if req != "auto":
        raise ValueError("encoding must be auto, glsl or spirv")
    return ENC_SPIRV if original_kind == "spirv" else ENC_GLSL


SPIRV_MAGIC = 0x07230203


def validate_spirv(data):
    """
    Structural check of a SPIR-V binary: magic, whole 32-bit words, and an instruction chain (word count in the top
    16 bits of each first word, never 0) that ends exactly at the end of the data. Raises ValueError otherwise.

    Needed because the Vulkan replay accepts ANY bytes in BuildTargetShader (compiled=True, no message): a
    truncated binary was "replaced" and the replay then failed every later readback until the capture was
    reopened (measured on a phone). A well-formed but semantically wrong module is still the caller's risk.
    """
    import struct
    if len(data) < 20 or len(data) % 4:
        raise ValueError("not a SPIR-V binary: %d bytes (need a multiple of 4, at least 20)" % len(data))
    words = struct.unpack("<%dI" % (len(data) // 4), data)
    if words[0] != SPIRV_MAGIC:
        raise ValueError("not a SPIR-V binary: magic 0x%08x (expected 0x%08x, little-endian)" % (words[0], SPIRV_MAGIC))
    i, bound = 5, words[3]
    while i < len(words):
        count = words[i] >> 16
        if count == 0 or i + count > len(words):
            raise ValueError("malformed SPIR-V: instruction at word %d has length %d, %d words left (truncated?)"
                             % (i, count, len(words) - i))
        i += count
    if bound == 0 or bound > (1 << 22):
        raise ValueError("malformed SPIR-V: implausible id bound %d" % bound)
    return len(words)


def decode_spirv_source(text):
    """base64 text -> bytes, with an error that says what was expected (a plain b64decode says "Incorrect padding")"""
    try:
        return base64.b64decode(text, validate=True)
    except Exception:
        raise ValueError("source is not base64: a SPIR-V replacement must be the base64 of the binary (or give "
                         "source_path to a .spv file, or encoding=\"glsl\" for GLSL text)")


def short_hash(data):
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.md5(data).hexdigest()[:12]


# ---------------------------------------------------------------- service

class ReplaceService:
    """Replace shaders of the loaded capture"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn
        self._active = {}                 # original shader id (str) -> record
        self._capture = None

    def _sync_capture(self):
        """Replacements belong to one loaded capture; forget them when it changed (their ids are gone)"""
        name = self.ctx.GetCaptureFilename() if self.ctx.IsCaptureLoaded() else None
        if name != self._capture:
            self._active = {}
            self._capture = name

    def _original(self, refl):
        """(kind, text or bytes): "glsl" with the source text, "spirv" with the binary"""
        try:
            if int(refl.encoding) in (3, 7):
                return ENC_SPIRV, bytes(refl.rawBytes)
        except Exception:
            pass
        try:
            files = refl.debugInfo.files
            if files and files[0].contents:
                return ENC_GLSL, str(files[0].contents)
        except Exception:
            pass
        return None, None

    def replace_shader(self, event_id, stage="pixel", source=None, edits=None, encoding="auto", entry=None,
                       dry_run=False):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        self._sync_capture()
        if (stage or "").lower() not in STAGES:
            raise ValueError("stage must be one of %s" % ", ".join(STAGES))
        if source is not None and edits:
            raise ValueError("give either source (the whole new shader) or edits, not both")
        stage_enum = Parsers.parse_stage(stage)
        result = {"data": None}

        def callback(controller):
            controller.SetFrameEvent(int(event_id), True)
            pipe = controller.GetPipelineState()
            orig_id = pipe.GetShader(stage_enum)
            if orig_id == rd.ResourceId.Null():
                raise ValueError("no %s shader bound at event %d" % (stage, int(event_id)))
            refl = pipe.GetShaderReflection(stage_enum)
            kind, original = self._original(refl)
            if kind is None:
                raise ValueError("the %s shader at event %d has neither GLSL source nor SPIR-V to start from"
                                 % (stage, int(event_id)))
            key = str(orig_id)
            # edits apply to what the shader is now: the original text, never an earlier replacement of it
            enc = pick_encoding(encoding, kind)
            counts = []
            if source is not None:
                new_src = decode_spirv_source(source) if enc == ENC_SPIRV else str(source)
            elif edits:
                if kind != ENC_GLSL:
                    raise ValueError("edits work on GLSL text; this shader is SPIR-V (no source): give source "
                                     "(SPIR-V base64, or Vulkan GLSL with encoding=\"glsl\")")
                new_src, counts = apply_edits(original, edits)
            else:
                new_src = original               # rebuild unchanged: the control for any experiment
            if enc == ENC_GLSL and isinstance(new_src, str):
                data = new_src.encode("utf-8")
            else:
                data = bytes(new_src)
                validate_spirv(data)
            info = {
                "event_id": int(event_id), "stage": stage.lower(), "shader_id": key,
                "original_kind": kind, "encoding": enc, "entry_point": entry or str(refl.entryPoint),
                "original_hash": short_hash(original), "new_hash": short_hash(new_src),
                "edits_applied": counts, "bytes": len(data),
            }
            if kind == ENC_GLSL and enc == ENC_GLSL:
                info["changes"] = change_summary(original, new_src)
            if dry_run:
                info["dry_run"] = True
                result["data"] = info
                return
            encoding_enum = rd.ShaderEncoding.GLSL if enc == ENC_GLSL else (
                rd.ShaderEncoding.OpenGLSPIRV if int(refl.encoding) == 7 else rd.ShaderEncoding.SPIRV)
            new_id, errors = controller.BuildTargetShader(info["entry_point"], encoding_enum, data,
                                                          rd.ShaderCompileFlags(), stage_enum)
            info["compiler_output"] = str(errors or "")
            if new_id == rd.ResourceId.Null():
                info["compiled"] = False
                info["replaced"] = False
                result["data"] = info
                return
            previous = self._active.pop(key, None)
            if previous is not None:
                controller.RemoveReplacement(orig_id)
                controller.FreeTargetResource(previous["new"])
            controller.ReplaceResource(orig_id, new_id)
            self._active[key] = {"orig": orig_id, "new": new_id, "stage": stage.lower(), "event_id": int(event_id),
                                 "new_hash": info["new_hash"], "edits": len(counts)}
            controller.SetFrameEvent(int(event_id), True)           # replay again with the new shader
            info["compiled"] = True
            info["replaced"] = True
            info["replacement_id"] = str(new_id)
            info["active_replacements"] = self._listing()
            result["data"] = info

        self._invoke(callback)
        return result["data"]

    def _listing(self):
        return [{"shader_id": k, "replacement_id": str(v["new"]), "stage": v["stage"],
                 "set_at_event": v["event_id"], "hash": v["new_hash"]} for k, v in sorted(self._active.items())]

    def revert_shader(self, shader_id=None):
        """Remove one replacement (original or replacement id) or, without an id, all of them"""
        if not self.ctx.IsCaptureLoaded():
            self._active = {}
            self._capture = None
            return {"reverted": [], "active_replacements": []}
        self._sync_capture()
        want = None
        if shader_id:
            want = str(Parsers.extract_numeric_id(str(shader_id)))
        result = {"data": None}

        def callback(controller):
            reverted = []
            for key, rec in list(self._active.items()):
                if want is not None and want not in (str(Parsers.extract_numeric_id(key)),
                                                     str(Parsers.extract_numeric_id(str(rec["new"])))):
                    continue
                controller.RemoveReplacement(rec["orig"])
                controller.FreeTargetResource(rec["new"])
                del self._active[key]
                reverted.append({"shader_id": key, "stage": rec["stage"]})
            if want is not None and not reverted:
                raise ValueError("no active replacement for %s" % shader_id)
            if reverted:
                controller.SetFrameEvent(int(self.ctx.CurEvent()), True)
            result["data"] = {"reverted": reverted, "active_replacements": self._listing()}

        self._invoke(callback)
        return result["data"]
