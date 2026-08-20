# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import time
import threading
import traceback

# maya.cmds is intentionally NOT used inside this worker thread.
import bg_core
import bg_license

try:
    import bg_math_core
    HAS_MATH_CORE = True
except ImportError:
    bg_math_core = None
    HAS_MATH_CORE = False
    print("WARNING: bg_math_core (.pyd) not found! LP matching will use bbox-center fallback logic.")

try:
    from PySide6 import QtCore
except ImportError:
    from PySide2 import QtCore


class LPMatchingWorker(QtCore.QThread):
    progress_value = QtCore.Signal(int)
    progress_text = QtCore.Signal(str)
    result_ready = QtCore.Signal(dict)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()

    def __init__(self, hp_groups, hp_data_cache, lp_data_cache,
                 hp_verts_cache, lp_verts_cache_fast, lp_verts_cache_full,
                 lp_threshold_coef):
        """
        Fast LP -> HP group matcher.

        Important performance rule:
        This worker must not call Maya API/cmds. All geometry is pre-cached in
        bg_mixins on the main thread.
        """
        super(LPMatchingWorker, self).__init__()
        self.hp_groups = hp_groups
        self.hp_data = hp_data_cache
        self.lp_data = lp_data_cache

        self.hp_verts_cache = hp_verts_cache
        self.lp_verts_cache_fast = lp_verts_cache_fast
        self.lp_verts_cache_full = lp_verts_cache_full

        self.lp_threshold_coef = lp_threshold_coef
        self.is_cancelled = False
        self._terminal_lock = threading.Lock()
        self._terminal_emitted = False
        self._prepared_groups = None
        self._prepared_lp_fast = None
        self._prepared_lp_full = None

    @staticmethod
    def _center_from_min_max(data):
        return [
            (data["max"][0] + data["min"][0]) * 0.5,
            (data["max"][1] + data["min"][1]) * 0.5,
            (data["max"][2] + data["min"][2]) * 0.5,
        ]

    @staticmethod
    def _center_distance(center_a, center_b):
        dx = center_a[0] - center_b[0]
        dy = center_a[1] - center_b[1]
        dz = center_a[2] - center_b[2]
        return (dx * dx + dy * dy + dz * dz) ** 0.5

    @staticmethod
    def _flat_vertex_buffer(vertices):
        """Return flat XYZ data, preserving already-flat buffers by reference."""
        if not vertices:
            return vertices
        first = vertices[0]
        if not isinstance(first, (list, tuple)):
            return vertices
        flattened = []
        for point in vertices:
            if len(point) >= 3:
                flattened.extend((point[0], point[1], point[2]))
        return flattened

    def _prepare_match_data(self):
        """Precompute stable HP records and normalize vertex buffers once per run."""
        if self._prepared_groups is not None:
            return

        prepared_hp_resources = {}
        prepared_groups = []
        for group_name in sorted(self.hp_groups.keys(), key=str):
            records = []
            seen_paths = set()
            for hp_path in sorted(self.hp_groups.get(group_name) or (), key=str):
                if hp_path in seen_paths:
                    continue
                seen_paths.add(hp_path)
                hp_data = self.hp_data.get(hp_path)
                if not hp_data:
                    continue
                try:
                    center = self._center_from_min_max(hp_data)
                    size = [
                        hp_data["max"][axis] - hp_data["min"][axis]
                        for axis in range(3)
                    ]
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                hp_resource = prepared_hp_resources.get(hp_path)
                if hp_resource is None:
                    hp_resource = {
                        "vertices": self._flat_vertex_buffer(
                            self.hp_verts_cache.get(hp_path)
                        ),
                        "native_index": None,
                        "native_index_initialized": False,
                    }
                    prepared_hp_resources[hp_path] = hp_resource
                records.append({
                    "path": hp_path,
                    "data": hp_data,
                    "center": center,
                    "size": size,
                    "resource": hp_resource,
                })
            prepared_groups.append((
                group_name,
                tuple(records),
                "zbrush" in str(group_name).lower()
            ))

        self._prepared_groups = tuple(prepared_groups)
        self._prepared_lp_fast = dict(
            (lp_path, self._flat_vertex_buffer(self.lp_verts_cache_fast.get(lp_path)))
            for lp_path in sorted(self.lp_data.keys(), key=str)
        )
        self._prepared_lp_full = dict(
            (lp_path, self._flat_vertex_buffer(self.lp_verts_cache_full.get(lp_path)))
            for lp_path in sorted(self.lp_data.keys(), key=str)
        )

    def get_best_match(self, lp_data, lp_verts_flat):
        """
        Restored fast logic from the older worker:
        - bbox overlap prefilter
        - topology/position fast-confirm
        - one-way C++ distance: LP -> HP

        The previous v4 used bidirectional distance and weighted group scoring;
        that is more expensive and is not needed for the fast LP assignment pass.
        """
        self._prepare_match_data()
        best_grp = None
        best_group_is_zbrush = False
        min_avg_distance = float('inf')

        center_lp = self._center_from_min_max(lp_data)
        size_lp = [
            lp_data["max"][0] - lp_data["min"][0],
            lp_data["max"][1] - lp_data["min"][1],
            lp_data["max"][2] - lp_data["min"][2],
        ]
        diag_sq_lp = size_lp[0] * size_lp[0] + size_lp[1] * size_lp[1] + size_lp[2] * size_lp[2]

        for grp, hp_records, group_is_zbrush in self._prepared_groups:
            if self.is_cancelled or self.isInterruptionRequested():
                return None

            fast_confirm = False

            for hp_record in hp_records:
                hp_data = hp_record["data"]

                # Cheap broad-phase filter: only expensive C++ distance for plausible candidates.
                if not bg_core.MathUtils.is_overlapping(lp_data, hp_data, padding=1.05):
                    continue

                # Fast path for duplicated LP/HP topology in the same place.
                is_topo_match = (
                    lp_data.get("vtx", -1) == hp_data.get("vtx", -2) and
                    lp_data.get("edges", -1) == hp_data.get("edges", -2)
                )

                if is_topo_match:
                    size_hp = hp_record["size"]
                    dims_match = all(
                        abs(size_lp[i] - size_hp[i]) / (size_lp[i] if size_lp[i] > 1e-6 else 1e-6) < 0.001
                        for i in range(3)
                    )

                    if dims_match:
                        center_hp = hp_record["center"]
                        dx = center_lp[0] - center_hp[0]
                        dy = center_lp[1] - center_hp[1]
                        dz = center_lp[2] - center_hp[2]
                        center_dist_sq = dx * dx + dy * dy + dz * dz

                        if center_dist_sq <= diag_sq_lp * 0.001:
                            fast_confirm = True
                            break

                hp_resource = hp_record["resource"]
                hp_verts_flat = hp_resource["vertices"]

                if HAS_MATH_CORE and hp_verts_flat and lp_verts_flat:
                    if not hp_resource["native_index_initialized"]:
                        hp_resource["native_index_initialized"] = True
                        index_class = getattr(bg_math_core, "PointCloudIndex", None)
                        if index_class is not None:
                            try:
                                hp_resource["native_index"] = index_class(hp_verts_flat)
                            except (TypeError, ValueError):
                                # Older/incompatible native modules retain the legacy path.
                                hp_resource["native_index"] = None
                    native_index = hp_resource["native_index"]
                    if native_index is not None:
                        avg_dist = native_index.average_distance(lp_verts_flat)
                    else:
                        avg_dist = bg_math_core.calculate_avg_distance(
                            lp_verts_flat, hp_verts_flat
                        )
                else:
                    # Safe fallback when .pyd is missing or some cache is empty.
                    center_hp = hp_record["center"]
                    avg_dist = self._center_distance(center_lp, center_hp)

                if avg_dist < min_avg_distance:
                    min_avg_distance = avg_dist
                    best_grp = grp
                    best_group_is_zbrush = group_is_zbrush

            if fast_confirm:
                return grp

        if best_grp:
            threshold = lp_data.get("diag", 1.0) * self.lp_threshold_coef
            if best_group_is_zbrush:
                threshold *= 3.0
            if min_avg_distance < threshold:
                return best_grp

        return None

    def run(self):
        try:
            result = self._run_impl()
        except BaseException:
            error_text = traceback.format_exc().strip()
            with self._terminal_lock:
                if self._terminal_emitted:
                    return
                self._terminal_emitted = True
                if self.is_cancelled or self.isInterruptionRequested():
                    terminal = "cancelled"
                else:
                    terminal = "failed"
            if terminal == "cancelled":
                self.cancelled.emit()
            else:
                self.failed.emit(error_text)
            return

        with self._terminal_lock:
            if self._terminal_emitted:
                return
            self._terminal_emitted = True
            if self.is_cancelled or self.isInterruptionRequested():
                terminal = "cancelled"
            elif result is None:
                terminal = "missing_result"
            else:
                terminal = "result"
        if terminal == "cancelled":
            self.cancelled.emit()
        elif terminal == "missing_result":
            self.failed.emit("LP matching worker exited without a result.")
        else:
            self.result_ready.emit(result)

    def _run_impl(self):
        bg_license.require_authorized_action()
        self._prepare_match_data()
        matches = {grp: set() for grp in sorted(self.hp_groups.keys(), key=str)}
        unassigned_lp = sorted(self.lp_data.keys(), key=str)

        self.progress_text.emit("Geometric surface analysis via C++ Kernel (Fast Pass)...")
        first_pass_misses = []

        total = max(len(unassigned_lp), 1)
        for i, lp_path in enumerate(unassigned_lp):
            if self.is_cancelled:
                return
            self.progress_value.emit(int((i / total) * 50))

            lp_verts_flat = self._prepared_lp_fast.get(lp_path)
            if not lp_verts_flat:
                # Do not silently drop it; try full cache in second pass.
                first_pass_misses.append(lp_path)
                continue

            best_grp = self.get_best_match(self.lp_data[lp_path], lp_verts_flat)
            if best_grp:
                matches[best_grp].add(lp_path)
            else:
                first_pass_misses.append(lp_path)

        if first_pass_misses:
            self.progress_text.emit("Precise tracking for unmatched LP...")
            total_misses = max(len(first_pass_misses), 1)
            for i, lp_path in enumerate(first_pass_misses):
                if self.is_cancelled:
                    return
                self.progress_value.emit(50 + int((i / total_misses) * 50))

                lp_verts_flat = self._prepared_lp_full.get(lp_path)
                if not lp_verts_flat:
                    continue

                best_grp = self.get_best_match(self.lp_data[lp_path], lp_verts_flat)
                if best_grp:
                    matches[best_grp].add(lp_path)

        self.progress_value.emit(100)
        self.progress_text.emit("Matching complete!")
        return matches

    def stop(self):
        with self._terminal_lock:
            self.is_cancelled = True
            self.requestInterruption()
