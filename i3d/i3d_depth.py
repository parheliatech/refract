#!/usr/bin/env python3
"""
i3d_depth -- Depth Anything inference and normalization, shared by all phases.

Phase 1 built the interpreter inside every call, which is fine for one still
image and ruinous for video (model load dominates). This keeps one interpreter
alive and adds the temporal smoothing video needs.

    eng = DepthEngine()
    d01, ms = eng.infer_norm(rgb_uint8)
"""

import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "model", "depth_anything_fp16_640_360.tflite")


def normalize_depth(d, lo_pct=2.0, hi_pct=98.0, soften=True, bounds=None):
    """Depth Anything emits UNNORMALIZED inverse depth; the shader wants [0,1].

    This is the piece the shader comments attribute to depth_normalization.rs,
    which is not in the APK -- reimplemented here.

    Percentile clipping (not min/max) so a few outlier pixels cannot crush the
    usable range. Higher value = nearer, which the shader's
    dFT = depth*2-1 mapping expects (convergence plane at 0.5).

    `bounds` overrides the per-frame percentiles -- video uses it to hold the
    range steady across frames (see DepthEngine.smooth).
    """
    lo, hi = bounds if bounds is not None else np.percentile(d, [lo_pct, hi_pct])
    if hi - lo < 1e-6:
        return np.full_like(d, 0.5)
    n = np.clip((d - lo) / (hi - lo), 0.0, 1.0)
    if soften:
        # horizontal 3-tap blur -- port of soften_edges_horizontal; stabilizes
        # the march against single-texel depth noise.
        p = np.pad(n, ((0, 0), (1, 1)), mode="edge")
        n = (p[:, :-2] + p[:, 1:-1] + p[:, 2:]) / 3.0
    return n.astype(np.float32)


class DepthEngine:
    """One long-lived TFLite interpreter.

    smooth: EMA factor for the normalization percentiles, 0 = off. Per-frame
    percentiles make video pump -- the whole scene breathes as a bright object
    enters or leaves -- because the mapping from raw depth to [0,1] moves under
    the shader. Holding the range steady costs nothing and fixes it.
    """

    def __init__(self, model=MODEL, threads=None, smooth=0.0):
        from ai_edge_litert.interpreter import Interpreter
        if not os.path.exists(model):
            raise FileNotFoundError("depth model missing: %s" % model)
        self.it = Interpreter(model_path=model,
                              num_threads=threads or os.cpu_count() or 4)
        self.it.allocate_tensors()
        self.inp = self.it.get_input_details()[0]
        self.out = self.it.get_output_details()[0]
        _, self.mh, self.mw, _ = self.inp["shape"]
        self.smooth = smooth
        self._bounds = None

    def infer(self, rgb):
        """Raw float32 depth at model resolution, plus seconds elapsed."""
        from PIL import Image
        small = np.asarray(
            Image.fromarray(rgb).resize((self.mw, self.mh), Image.BILINEAR),
            dtype=np.float32) / 255.0
        t0 = time.time()
        self.it.set_tensor(self.inp["index"],
                           small[None, ...].astype(self.inp["dtype"]))
        self.it.invoke()
        depth = self.it.get_tensor(self.out["index"])[0, ..., 0].astype(np.float32)
        return depth, time.time() - t0

    def infer_norm(self, rgb, lo_pct=2.0, hi_pct=98.0):
        """Normalized [0,1] depth ready for the shader, plus seconds elapsed."""
        raw, dt = self.infer(rgb)
        lo, hi = np.percentile(raw, [lo_pct, hi_pct])
        if self.smooth > 0.0:
            if self._bounds is None:
                self._bounds = (lo, hi)
            else:
                k = self.smooth
                self._bounds = (self._bounds[0] * k + lo * (1 - k),
                                self._bounds[1] * k + hi * (1 - k))
            lo, hi = self._bounds
        return normalize_depth(raw, bounds=(lo, hi)), dt
