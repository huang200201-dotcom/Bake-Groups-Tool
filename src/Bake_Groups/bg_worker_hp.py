# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import time
import math
import re
import heapq
import threading
import traceback

import bg_core
try:
    import bg_math_core
    HAS_MATH_CORE = True
except ImportError:
    print("WARNING: bg_math_core not found! HP worker will use fallback logic.")
    HAS_MATH_CORE = False

try:
    from PySide6 import QtCore
except ImportError:
    from PySide2 import QtCore


def bounded_grid_cells(bbox, cell_size, max_cells=4096):
    """Return representative occupied cells with a strict memory bound."""
    cell_size = max(float(cell_size), 0.000001)
    max_cells = max(1, int(max_cells))
    min_x, max_x = int(math.floor(bbox[0] / cell_size)), int(math.floor(bbox[3] / cell_size))
    min_y, max_y = int(math.floor(bbox[1] / cell_size)), int(math.floor(bbox[4] / cell_size))
    min_z, max_z = int(math.floor(bbox[2] / cell_size)), int(math.floor(bbox[5] / cell_size))
    count_x = max_x - min_x + 1
    count_y = max_y - min_y + 1
    count_z = max_z - min_z + 1
    if count_x * count_y * count_z <= max_cells:
        return [(x, y, z) for x in range(min_x, max_x + 1)
                         for y in range(min_y, max_y + 1)
                         for z in range(min_z, max_z + 1)]

    axis_counts = [count_x, count_y, count_z]
    sample_counts = [1, 1, 1]
    while True:
        current_product = sample_counts[0] * sample_counts[1] * sample_counts[2]
        candidates = []
        for axis_index in range(3):
            if sample_counts[axis_index] >= axis_counts[axis_index]:
                continue
            next_product = (
                current_product // sample_counts[axis_index] *
                (sample_counts[axis_index] + 1)
            )
            if next_product <= max_cells:
                candidates.append((
                    axis_counts[axis_index] / float(sample_counts[axis_index]),
                    -axis_index,
                    axis_index
                ))
        if not candidates:
            break
        sample_counts[max(candidates)[2]] += 1

    def sampled_axis(low, high, sample_count):
        count = high - low + 1
        if count <= sample_count:
            return list(range(low, high + 1))
        if sample_count <= 1:
            return [low + ((count - 1) // 2)]
        return sorted(set(
            low + int(round(index * (count - 1) / float(sample_count - 1)))
            for index in range(sample_count)
        ))

    return [
        (x, y, z)
        for x in sampled_axis(min_x, max_x, sample_counts[0])
        for y in sampled_axis(min_y, max_y, sample_counts[1])
        for z in sampled_axis(min_z, max_z, sample_counts[2])
    ]


def bbox_sweep_candidate_pairs(names, bbox_by_name, padding_by_name):
    """Yield deterministic bbox candidates without materializing all name pairs."""
    records = []
    for name in sorted(set(names)):
        bbox = bbox_by_name.get(name)
        if not bbox or len(bbox) != 6:
            continue
        try:
            values = tuple(float(value) for value in bbox)
            padding = max(float(padding_by_name.get(name, 0.0) or 0.0), 0.0)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in values) or not math.isfinite(padding):
            continue
        if values[0] > values[3] or values[1] > values[4] or values[2] > values[5]:
            continue
        expanded = (
            values[0] - padding, values[1] - padding, values[2] - padding,
            values[3] + padding, values[4] + padding, values[5] + padding
        )
        records.append((name, values, padding, expanded))

    if not records:
        return

    axis_scores = []
    for axis_index in range(3):
        low = min(record[3][axis_index] for record in records)
        high = max(record[3][axis_index + 3] for record in records)
        average_width = sum(
            record[3][axis_index + 3] - record[3][axis_index]
            for record in records
        ) / float(len(records))
        axis_scores.append(((high - low) / max(average_width, 0.000001), -axis_index))
    sweep_axis = max(axis_scores)[1] * -1
    other_axes = [axis for axis in range(3) if axis != sweep_axis]
    records.sort(key=lambda record: (
        record[3][sweep_axis], record[3][sweep_axis + 3], record[0]
    ))
    active = []
    for name, bbox, padding, expanded in records:
        expanded_min = expanded[sweep_axis]
        active = [
            record for record in active
            if record[3][sweep_axis + 3] >= expanded_min
        ]
        for other in active:
            other_expanded = other[3]
            if any(
                other_expanded[axis + 3] < expanded[axis] or
                other_expanded[axis] > expanded[axis + 3]
                for axis in other_axes
            ):
                continue
            yield tuple(sorted((other[0], name)))
        active.append((name, bbox, padding, expanded))


def bboxes_within_tolerance(bbox_a, bbox_b, tolerance):
    """Conservative exact-distance broad check; true point hits cannot be rejected."""
    tolerance = max(float(tolerance), 0.0)
    return not (
        bbox_a[3] + tolerance < bbox_b[0] or bbox_b[3] + tolerance < bbox_a[0] or
        bbox_a[4] + tolerance < bbox_b[1] or bbox_b[4] + tolerance < bbox_a[1] or
        bbox_a[5] + tolerance < bbox_b[2] or bbox_b[5] + tolerance < bbox_a[2]
    )


def sample_flat_vertices(flat_vertices, max_points):
    """Deterministically sample an already-flat XYZ buffer without copying it needlessly."""
    if not flat_vertices:
        return flat_vertices
    max_points = max(1, int(max_points))
    point_count = len(flat_vertices) // 3
    if point_count <= max_points:
        return flat_vertices
    step = max(1, int(math.ceil(point_count / float(max_points))))
    sampled = []
    for point_index in range(0, point_count, step):
        base = point_index * 3
        sampled.extend(flat_vertices[base:base + 3])
    return sampled


def calculate_surface_near_coverage(source_vertices, target_vertices, threshold,
                                    max_source=180, max_target=900,
                                    cancelled_getter=None):
    """Calculate sampled surface coverage, preferring the native KD-tree kernel."""
    cancelled_getter = cancelled_getter or (lambda: False)
    if (not source_vertices or not target_vertices or threshold <= 0.0 or
            cancelled_getter()):
        return 0.0, float('inf')

    source_sample = sample_flat_vertices(source_vertices, max_source)
    target_sample = sample_flat_vertices(target_vertices, max_target)
    source_count = len(source_sample) // 3
    target_count = len(target_sample) // 3
    if source_count <= 0 or target_count <= 0 or cancelled_getter():
        return 0.0, float('inf')

    if HAS_MATH_CORE and hasattr(bg_math_core, 'calculate_coverage_stats'):
        try:
            coverage, average_distance = bg_math_core.calculate_coverage_stats(
                source_sample, target_sample, float(threshold)
            )
            if cancelled_getter():
                return 0.0, float('inf')
            coverage = float(coverage)
            average_distance = float(average_distance)
            if math.isfinite(coverage) and math.isfinite(average_distance):
                return max(0.0, min(1.0, coverage)), average_distance
        except Exception:
            pass

    # Compatibility fallback for an older/missing native module.  Keep the
    # quadratic work strictly small so one candidate cannot stall the worker.
    source_sample = sample_flat_vertices(source_sample, 48)
    target_sample = sample_flat_vertices(target_sample, 192)
    source_count = len(source_sample) // 3
    target_count = len(target_sample) // 3
    threshold_sq = float(threshold) * float(threshold)
    near_count = 0
    minimum_distance_sum = 0.0
    for source_index in range(source_count):
        if source_index % 4 == 0 and cancelled_getter():
            return 0.0, float('inf')
        source_base = source_index * 3
        source_x = source_sample[source_base]
        source_y = source_sample[source_base + 1]
        source_z = source_sample[source_base + 2]
        best_sq = float('inf')
        for target_index in range(target_count):
            target_base = target_index * 3
            dx = source_x - target_sample[target_base]
            dy = source_y - target_sample[target_base + 1]
            dz = source_z - target_sample[target_base + 2]
            distance_sq = dx * dx + dy * dy + dz * dz
            if distance_sq < best_sq:
                best_sq = distance_sq
        if best_sq <= threshold_sq:
            near_count += 1
        minimum_distance_sum += math.sqrt(max(best_sq, 0.0))
    if source_count <= 0:
        return 0.0, float('inf')
    return (
        near_count / float(source_count),
        minimum_distance_sum / float(source_count)
    )


def find_confident_direct_matches(hp_candidates, hp_data, lp_data,
                                  hp_vertices, lp_vertices,
                                  coverage_getter=None,
                                  cancelled_getter=None):
    """Return only conservative conflict-resolving HP/LP protections.

    This is deliberately *not* a general matching pass. The normal LP-guided
    resolver already handles straightforward one-candidate relationships more
    reliably. This helper only keeps an excellent, unambiguous match out of the
    neighbouring-HP compound-link pass when several HP meshes compete around
    the same LP. Limiting its scope avoids a sampled-vertex heuristic freezing
    ordinary multi-part assets into separate ownership decisions.
    """
    coverage_getter = coverage_getter or calculate_surface_near_coverage
    cancelled_getter = cancelled_getter or (lambda: False)

    # Direct protection matters only for a real LP-side conflict. Earlier
    # versions evaluated every one-to-one candidate, even when the normal
    # resolver would make the same decision without this extra heuristic.
    lp_candidate_counts = {}
    for hp_name, candidates in hp_candidates.items():
        hp_info = hp_data.get(hp_name, {})
        if hp_info.get('is_zbrush', False) or 'zbrush' in str(hp_name).lower():
            continue
        for lp_name in set(candidates or []):
            lp_candidate_counts[lp_name] = lp_candidate_counts.get(lp_name, 0) + 1
    competing_lps = set(
        lp_name for lp_name, count in lp_candidate_counts.items() if count >= 2
    )
    if not competing_lps:
        return {}

    def _diag(info):
        try:
            value = float(info.get('diag', info.get('radius', 0.5) * 2.0) or 0.0)
        except Exception:
            value = 0.0
        return max(value, 0.0001) if math.isfinite(value) else 0.0001

    def _center(info):
        bbox = info.get('bbox') or []
        if len(bbox) == 6:
            try:
                return tuple((float(bbox[index]) + float(bbox[index + 3])) * 0.5
                             for index in range(3))
            except Exception:
                pass
        center = info.get('center') or (0.0, 0.0, 0.0)
        try:
            return tuple(float(center[index]) for index in range(3))
        except Exception:
            return (0.0, 0.0, 0.0)

    contenders_by_hp = {}
    for hp_name in sorted(hp_candidates.keys(), key=str):
        if cancelled_getter():
            return {}
        hp_info = hp_data.get(hp_name, {})
        hp_flat = hp_vertices.get(hp_name, []) or []
        if not hp_flat or hp_info.get('is_zbrush', False) or 'zbrush' in hp_name.lower():
            continue
        hp_diag = _diag(hp_info)
        hp_center = _center(hp_info)
        scored = []
        for lp_name in sorted(set(hp_candidates.get(hp_name, []) or []), key=str):
            if lp_name not in competing_lps:
                continue
            if cancelled_getter():
                return {}
            lp_info = lp_data.get(lp_name, {})
            lp_flat = lp_vertices.get(lp_name, []) or []
            if not lp_flat:
                continue
            lp_diag = _diag(lp_info)
            scale_ratio = max(hp_diag, lp_diag) / max(min(hp_diag, lp_diag), 0.0001)
            if scale_ratio > 2.75:
                continue
            lp_center = _center(lp_info)
            center_distance = math.sqrt(sum(
                (hp_center[index] - lp_center[index]) ** 2 for index in range(3)
            ))
            pair_scale = max(min(hp_diag, lp_diag), 0.0001)
            if center_distance > max(pair_scale * 0.20, 0.0001):
                continue
            tolerance = max(pair_scale * 0.025, 0.0001)
            hp_coverage, hp_average = coverage_getter(
                hp_flat, lp_flat, tolerance,
                max_source=240, max_target=900,
                cancelled_getter=cancelled_getter,
            )
            lp_coverage, lp_average = coverage_getter(
                lp_flat, hp_flat, tolerance,
                max_source=180, max_target=900,
                cancelled_getter=cancelled_getter,
            )
            if cancelled_getter():
                return {}
            if (hp_coverage < 0.90 or lp_coverage < 0.82 or
                    not math.isfinite(hp_average) or not math.isfinite(lp_average) or
                    hp_average > tolerance * 0.65 or lp_average > tolerance * 0.75):
                continue
            distance_quality = max(
                0.0,
                1.0 - max(hp_average / tolerance, lp_average / tolerance),
            )
            confidence = (
                min(float(hp_coverage), float(lp_coverage)) * 0.72 +
                distance_quality * 0.20 +
                max(0.0, 1.0 - center_distance / pair_scale) * 0.08
            )
            scored.append({
                'hp': hp_name,
                'lp': lp_name,
                'confidence': confidence,
                'hp_coverage': float(hp_coverage),
                'lp_coverage': float(lp_coverage),
                'average_distance': max(float(hp_average), float(lp_average)),
                'tolerance': tolerance,
            })
        scored.sort(key=lambda item: (-item['confidence'], str(item['lp'])))
        if not scored or scored[0]['confidence'] < 0.86:
            continue
        if len(scored) > 1 and scored[0]['confidence'] - scored[1]['confidence'] < 0.08:
            continue
        contenders_by_hp[hp_name] = scored[0]

    contenders_by_lp = {}
    for contender in contenders_by_hp.values():
        contenders_by_lp.setdefault(contender['lp'], []).append(contender)

    protected = {}
    for lp_name, contenders in contenders_by_lp.items():
        contenders.sort(key=lambda item: (-item['confidence'], str(item['hp'])))
        if len(contenders) > 1 and contenders[0]['confidence'] - contenders[1]['confidence'] < 0.08:
            continue
        winner = contenders[0]
        protected[winner['hp']] = winner
    return protected


class RevisionedCandidateQueue(object):
    """Max-score directed-pair queue with lazy revision invalidation."""

    def __init__(self, candidate_getter, revision_getter, source_key_getter,
                 order_by_name, cancelled_getter=None):
        self._candidate_getter = candidate_getter
        self._revision_getter = revision_getter
        self._source_key_getter = source_key_getter
        self._order_by_name = order_by_name
        self._cancelled_getter = cancelled_getter or (lambda: False)
        self._heap = []
        self.evaluation_count = 0

    def _push(self, source_name, target_name):
        if source_name == target_name:
            return
        source_revision = self._revision_getter(source_name)
        target_revision = self._revision_getter(target_name)
        if source_revision is None or target_revision is None:
            return
        candidate = self._candidate_getter(source_name, target_name)
        self.evaluation_count += 1
        if not candidate:
            return
        score = candidate[0]
        heapq.heappush(self._heap, (
            -score,
            self._source_key_getter(source_name),
            self._order_by_name.get(source_name, 999999999),
            self._order_by_name.get(target_name, 999999999),
            source_name,
            target_name,
            source_revision,
            target_revision,
            candidate
        ))

    def seed(self, active_names):
        ordered_targets = sorted(
            active_names,
            key=lambda name: (self._order_by_name.get(name, 999999999), name)
        )
        ordered_sources = sorted(
            active_names,
            key=lambda name: (
                self._source_key_getter(name),
                self._order_by_name.get(name, 999999999)
            )
        )
        for source_index, source_name in enumerate(ordered_sources):
            if self._cancelled_getter():
                return False
            for pair_index, target_name in enumerate(ordered_targets):
                if pair_index % 64 == 0 and self._cancelled_getter():
                    return False
                self._push(source_name, target_name)
        return True

    def pop_best(self):
        while self._heap:
            entry = heapq.heappop(self._heap)
            source_name = entry[4]
            target_name = entry[5]
            if self._revision_getter(source_name) != entry[6]:
                continue
            if self._revision_getter(target_name) != entry[7]:
                continue
            candidate = entry[8]
            return (candidate[0], source_name, target_name) + tuple(candidate[1:])
        return None

    def refresh(self, changed_name, active_names):
        ordered_names = sorted(
            active_names,
            key=lambda name: (self._order_by_name.get(name, 999999999), name)
        )
        for pair_index, other_name in enumerate(ordered_names):
            if pair_index % 32 == 0 and self._cancelled_getter():
                return False
            if other_name == changed_name:
                continue
            self._push(changed_name, other_name)
            self._push(other_name, changed_name)
        return True


class HPGroupingWorker(QtCore.QThread):
    progress_value = QtCore.Signal(int)
    progress_text = QtCore.Signal(str)
    result_ready = QtCore.Signal(dict, list)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()

    def __init__(self, hp_data, lp_data, hp_verts_cache, lp_verts_cache, hp_holes_cache,
                 threshold_pct, group_limit, custom_clusters_dict=None,
                 locked_names=None, strategy=1, use_symmetry=True,
                 bolt_elongation=2.5, bolt_symmetry=0.8, wire_elongation=6.0, # wire_elongation СѓРІРµР»РёС‡РµРЅ РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ
                 compound_link_verts=8, compound_link_dist_pct=0.1,
                 detect_floaters=True, floater_radius=0.3,
                 grouping_mode="legacy_compact",
                 hp_surface_cache=None, lp_surface_cache=None):
        super(HPGroupingWorker, self).__init__()
        self.hp_data = hp_data
        self.lp_data = lp_data

        self.hp_verts_cache = hp_verts_cache
        self.lp_verts_cache = lp_verts_cache
        self.hp_holes_cache = hp_holes_cache
        self.hp_surface_cache = hp_surface_cache or {}
        self.lp_surface_cache = lp_surface_cache or {}

        self.threshold_pct = threshold_pct / 100.0
        self.threshold_delta = self.threshold_pct - 0.15
        self.match_padding = max(1.0, 1.2 + self.threshold_delta)
        self.match_tolerance_scale = max(0.1, 1.0 + self.threshold_delta)
        self.collision_tolerance = max(0.0001, 0.005 * self.match_tolerance_scale)
        self.group_limit = group_limit if group_limit > 0 else 999
        # GT Matcher/manual links are HARD constraints. Keep them separate from
        # auto semantic links so automatic ZBrush grouping cannot overwrite user links.
        self.custom_clusters_dict = {}
        for _name, _uuids in (custom_clusters_dict or {}).items():
            if not _name:
                continue
            _clean = []
            for _uid in (_uuids or []):
                if _uid and _uid not in _clean:
                    _clean.append(_uid)
            if _clean:
                self.custom_clusters_dict[_name] = _clean
        self.manual_cluster_names = set(self.custom_clusters_dict.keys())
        self.manual_uuid_set = set([u for vals in self.custom_clusters_dict.values() for u in vals])
        self.locked_names = locked_names or set()

        self.strategy = strategy
        self.use_symmetry = use_symmetry

        self.bolt_elongation = bolt_elongation
        self.bolt_symmetry = bolt_symmetry
        self.wire_elongation = wire_elongation
        self.compound_link_verts = max(1, int(compound_link_verts or 8))
        self.compound_link_dist_pct = max(0.01, float(compound_link_dist_pct or 0.1))

        self.detect_floaters = detect_floaters
        self.floater_radius = floater_radius

        valid_grouping_modes = ("conflict_safe", "one_lp_per_group", "legacy_compact")
        self.grouping_mode = grouping_mode if grouping_mode in valid_grouping_modes else "conflict_safe"

        self.is_cancelled = False
        self._terminal_lock = threading.Lock()
        self._terminal_emitted = False
        self.debug_lines = []
        self.summary_lines = []

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
            self.failed.emit("HP grouping worker exited without a result.")
        else:
            groups, logs = result
            self.result_ready.emit(groups, logs)

    def _run_impl(self):

        logs = []
        self.debug_lines = []
        self.summary_lines = []
        groups = {}
        linked_mesh_names = set()

        strategy_name = ["Spatial Volume Match", "PCA Shape Alignment", "Topology Fingerprint"][self.strategy] if self.strategy in (0, 1, 2) else str(self.strategy)
        safe_grouping = self.grouping_mode in ("conflict_safe", "one_lp_per_group")

        def _short_name(name):
            try:
                return str(name).split('|')[-1]
            except Exception:
                return str(name)

        def _lp_base_name(lp_name):
            try:
                value = str(lp_name)
                value = re.sub(r"::M\d{2,}$", "", value)
                base = value.split("::shell_")[0]
                slot = _lp_material_slot(lp_name)
                return "{}::{}".format(base, slot) if slot else base
            except Exception:
                return str(lp_name)

        def _lp_material_slot(lp_name):
            try:
                info = self.lp_data.get(lp_name, {})
                slot = info.get("material_slot")
                if slot:
                    return str(slot)
                match = re.search(r"::(M\d{2,})$", str(lp_name))
                if match:
                    return match.group(1)
            except Exception:
                pass
            return None

        lp_name_lookup = {}
        for _lp_name in self.lp_data.keys():
            _raw_lp = str(_lp_name)
            _base_lp = re.sub(r"::M\d{2,}$", "", _raw_lp).split("::shell_")[0]
            for _key in (_raw_lp, _short_name(_raw_lp), _base_lp, _short_name(_base_lp), _lp_base_name(_lp_name), _short_name(_lp_base_name(_lp_name))):
                if _key:
                    lp_name_lookup.setdefault(str(_key).lower(), _lp_name)

        def _cluster_owner_lp(cluster_name):
            raw = str(cluster_name or "").split("::")[0]
            candidates = [
                raw,
                _short_name(raw),
                re.sub(r"_(HP|LP)$", "", raw, flags=re.IGNORECASE),
                re.sub(r"_(HP|LP)$", "", _short_name(raw), flags=re.IGNORECASE),
            ]
            for candidate in candidates:
                found = lp_name_lookup.get(str(candidate).lower())
                if found:
                    return found
            return None

        def _lp_material_compatible(lp_a, lp_b):
            slot_a = _lp_material_slot(lp_a)
            slot_b = _lp_material_slot(lp_b)
            return not (slot_a and slot_b and slot_a != slot_b)

        def _claim_material_slot(hp_name):
            claim = hp_claims.get(hp_name, {}) if hp_name else {}
            slot = claim.get("material_slot")
            if slot:
                return str(slot)
            owner_lp = claim.get("owner_lp")
            return _lp_material_slot(owner_lp) if owner_lp else None

        def _debug(line):
            self.debug_lines.append(str(line))

        def _debug_list(prefix, values, limit=24):
            values = [_short_name(v) for v in (values or [])]
            if len(values) > limit:
                values = values[:limit] + ["... +{} more".format(len(values) - limit)]
            _debug("{}{}".format(prefix, ", ".join(values) if values else "none"))

        _debug("Bake Master Analyze HP Debug")
        _debug("Settings: collision={}%, strategy={}, grouping_mode={}, symmetry={}, hp_link_vtx={}, hp_link_dist={}%, max_groups={}".format(
            int(round(self.threshold_pct * 100.0)),
            strategy_name,
            self.grouping_mode,
            "on" if self.use_symmetry else "off",
            self.compound_link_verts,
            self.compound_link_dist_pct,
            self.group_limit
        ))
        _debug("Input: {} HP mesh(es), {} LP mesh(es).".format(len(self.hp_data), len(self.lp_data)))
        lp_material_slots = sorted(set([
            info.get("material_slot") for info in self.lp_data.values()
            if info.get("material_slot")
        ]))
        if lp_material_slots:
            logs.append("LP material-aware grouping: {} slot(s) detected ({}).".format(
                len(lp_material_slots),
                ", ".join(lp_material_slots)
            ))
            _debug("LP material slots: {}.".format(", ".join(lp_material_slots)))
        _debug("")

        self.progress_value.emit(2)
        self.progress_text.emit("Step 0: Semantic ZBrush Grouping...")
        logs.append(
                "HP settings: collision={}%, strategy={}, grouping_mode={}, symmetry={}.".format(
                    int(round(self.threshold_pct * 100.0)),
                    strategy_name,
                    self.grouping_mode,
                    "on" if self.use_symmetry else "off"
                )
            )

        # --- STEP 0: ZBRUSH SEMANTIC BINDING ---
        # Build auto semantic clusters separately. They must NEVER overwrite GT/manual links.
        semantic_clusters = {}
        for hp_name, hp_info in self.hp_data.items():
            uid = hp_info.get('uuid')
            if uid and uid in self.manual_uuid_set:
                continue
            if hp_info.get("is_zbrush", False) or "ZBrush" in hp_name or "zbrush" in hp_name.lower():
                match = re.search(r'(.+?)_high', hp_name, re.IGNORECASE)
                if match:
                    root_name = match.group(1)
                    cluster_key = "ZBrush_Semantic_{}_HP".format(root_name)
                    if uid:
                        semantic_clusters.setdefault(cluster_key, [])
                        if uid not in semantic_clusters[cluster_key]:
                            semantic_clusters[cluster_key].append(uid)

        effective_custom_clusters = dict((k, list(v)) for k, v in self.custom_clusters_dict.items())
        for cluster_key, uid_list in semantic_clusters.items():
            clean = [u for u in uid_list if u not in self.manual_uuid_set]
            if clean:
                effective_custom_clusters[cluster_key] = clean
        if semantic_clusters:
            _debug("Step 0: {} semantic ZBrush cluster candidate(s) prepared.".format(len(semantic_clusters)))
            for cluster_key, uid_list in sorted(semantic_clusters.items()):
                _debug("  ZBRUSH_SEMANTIC: {} | uuid_count={}".format(cluster_key, len(uid_list or [])))
        else:
            _debug("Step 0: no semantic ZBrush clusters detected.")
        _debug("")

        protected_group_names = set()

        self.progress_value.emit(5)
        self.progress_text.emit("Step 1: Preparing LP boundaries...")

        sorted_lp_names = sorted(
            self.lp_data.keys(),
            key=lambda k: (self.lp_data[k].get("bbox_vol", 0), str(k))
        )
        ordered_hp_names = sorted(self.hp_data.keys(), key=str)
        unassigned_hps = set(ordered_hp_names)
        lp_to_owned_hps = {lp: [] for lp in sorted_lp_names}
        hp_claims = {}

        self.progress_value.emit(10)
        self.progress_text.emit("Step 2: Smart Tender Matching (HP to LP)...")

        # GT Matcher/manual links are NOT created as final subgroups here.
        # They are injected later as cluster-items into the normal packing queue,
        # replacing auto-created cluster items before subgroup packing.
        # This keeps manual GT links authoritative without multiplying subgroups.

        # --- STEP 2: TENDER SYSTEM ---
        bbox_fallback_count = 0
        normal_center_fallback_count = 0
        def _center_from_info(info):
            bb = info.get('bbox')
            if bb and len(bb) == 6:
                return [(bb[0] + bb[3]) * 0.5, (bb[1] + bb[4]) * 0.5, (bb[2] + bb[5]) * 0.5]
            c = info.get('center')
            return c if c else [0.0, 0.0, 0.0]

        def _center_dist(a, b):
            ca = _center_from_info(a)
            cb = _center_from_info(b)
            return math.sqrt(sum((ca[i] - cb[i]) ** 2 for i in range(3)))

        hp_candidates = {hp: [] for hp in ordered_hp_names}
        unassigned_hp_names = list(unassigned_hps)

        for hp_idx, hp_name in enumerate(unassigned_hp_names):
            if self.is_cancelled: return
            if hp_idx % max(1, len(unassigned_hp_names) // 25) == 0:
                self.progress_value.emit(10 + int((hp_idx / float(max(len(unassigned_hp_names), 1))) * 8))
            hp_info = self.hp_data[hp_name]
            for lp_name in sorted_lp_names:
                lp_info = self.lp_data[lp_name]
                if bg_core.MathUtils.is_overlapping(lp_info, hp_info, padding=self.match_padding):
                    hp_candidates[hp_name].append(lp_name)

            # Center fallback: if bbox overlap fails, try one nearest LP candidate.
            # The later narrow validation still decides whether this HP is really assigned.
            # This keeps GT/manual links as hard constraints because they were removed from unassigned_hps above.
            if not hp_candidates[hp_name] and sorted_lp_names:
                hp_diag = max(hp_info.get('diag', hp_info.get('radius', 1.0) * 2.0), 0.001)
                nearest_lp = min(sorted_lp_names, key=lambda lp: _center_dist(hp_info, self.lp_data[lp]))
                nearest_info = self.lp_data[nearest_lp]
                lp_diag = max(nearest_info.get('diag', nearest_info.get('radius', 1.0) * 2.0), 0.001)
                is_zb = hp_info.get('is_zbrush', False) or 'zbrush' in hp_name.lower()
                if is_zb:
                    max_dist = max(min(hp_diag, lp_diag) * 2.0, hp_diag * 1.25, 0.001) * self.match_tolerance_scale
                else:
                    max_dist = max(min(hp_diag, lp_diag) * 3.0, hp_diag * 0.60, lp_diag * 1.5, 0.001) * self.match_tolerance_scale
                if _center_dist(hp_info, nearest_info) <= max_dist:
                    hp_candidates[hp_name].append(nearest_lp)
                    if is_zb:
                        bbox_fallback_count += 1
                    else:
                        normal_center_fallback_count += 1

        candidate_counts = [len(v) for v in hp_candidates.values()]
        no_candidate_count = sum(1 for c in candidate_counts if c == 0)
        single_candidate_count = sum(1 for c in candidate_counts if c == 1)
        multi_candidate_count = sum(1 for c in candidate_counts if c > 1)
        _debug("Step 2A: HP -> LP BBox candidates.")
        _debug("  Summary: no_candidate={}, single_candidate={}, multi_candidate={}, zbrush_center_fallback={}, normal_center_fallback={}.".format(
            no_candidate_count, single_candidate_count, multi_candidate_count, bbox_fallback_count, normal_center_fallback_count
        ))
        for hp_name in sorted(unassigned_hp_names, key=_short_name):
            _debug("  CANDIDATES: HP='{}' | count={} | LP={}".format(
                _short_name(hp_name),
                len(hp_candidates.get(hp_name, [])),
                ", ".join(_short_name(lp) for lp in hp_candidates.get(hp_name, [])) or "none"
            ))
        _debug("")

        def _is_finite_value(value):
            try:
                value = float(value)
                return not math.isnan(value) and not math.isinf(value)
            except Exception:
                return False

        def _is_zbrush_hp(hp_name):
            hp_info = self.hp_data.get(hp_name, {})
            return hp_info.get('is_zbrush', False) or 'zbrush' in hp_name.lower()

        def _analyze_shape_metrics(flat_verts):
            if not HAS_MATH_CORE or not flat_verts:
                return None
            analyzer = getattr(bg_math_core, 'analyze_mesh_shape_v2', None)
            if analyzer is None:
                analyzer = getattr(bg_math_core, 'analyze_mesh_shape', None)
            if analyzer is None:
                return None
            try:
                return analyzer(flat_verts)
            except Exception:
                return None

        def _flat_to_points(flat_verts):
            points = []
            if not flat_verts:
                return points
            for i in range(0, len(flat_verts) - 2, 3):
                x, y, z = flat_verts[i], flat_verts[i + 1], flat_verts[i + 2]
                if _is_finite_value(x) and _is_finite_value(y) and _is_finite_value(z):
                    points.append((float(x), float(y), float(z)))
            return points

        def _spatial_hash(points, cell_size):
            cell_size = max(float(cell_size), 0.000001)
            grid = {}
            for x, y, z in points:
                key = (
                    int(math.floor(x / cell_size)),
                    int(math.floor(y / cell_size)),
                    int(math.floor(z / cell_size))
                )
                grid.setdefault(key, []).append((x, y, z))
            return grid

        def _near_vertex_hits(source_points, target_name, tolerance, stop_after=None):
            target_points = hp_point_cache.get(target_name, [])
            if not source_points or not target_points:
                return 0
            tolerance = max(float(tolerance), 0.000001)
            tol_sq = tolerance * tolerance
            grid = hp_spatial_hash_cache.get(target_name)
            cell_size = hp_grid_cell_size_cache.get(target_name, tolerance)
            if grid is None:
                grid = _spatial_hash(target_points, cell_size)
                hp_spatial_hash_cache[target_name] = grid
            hits = 0

            for x, y, z in source_points:
                base = (
                    int(math.floor(x / cell_size)),
                    int(math.floor(y / cell_size)),
                    int(math.floor(z / cell_size))
                )
                found = False
                for dx in (-1, 0, 1):
                    if found:
                        break
                    for dy in (-1, 0, 1):
                        if found:
                            break
                        for dz in (-1, 0, 1):
                            bucket = grid.get((base[0] + dx, base[1] + dy, base[2] + dz), [])
                            for tx, ty, tz in bucket:
                                vx = x - tx
                                vy = y - ty
                                vz = z - tz
                                if (vx * vx) + (vy * vy) + (vz * vz) <= tol_sq:
                                    hits += 1
                                    found = True
                                    break
                            if found:
                                break
                if stop_after is not None and hits >= stop_after:
                    break
            return hits

        def _base_bbox(info):
            bbox = info.get('bbox')
            if not bbox or len(bbox) != 6:
                mn = info.get('min')
                mx = info.get('max')
                if mn and mx and len(mn) == 3 and len(mx) == 3:
                    bbox = [mn[0], mn[1], mn[2], mx[0], mx[1], mx[2]]
                else:
                    return None

            try:
                values = [float(v) for v in bbox]
            except Exception:
                return None
            if not all(_is_finite_value(v) for v in values):
                return None
            if values[0] > values[3] or values[1] > values[4] or values[2] > values[5]:
                return None
            return values

        def _points_inside_bbox(points, bbox):
            if not points or not bbox:
                return []
            min_x, min_y, min_z, max_x, max_y, max_z = bbox
            return [
                (x, y, z)
                for x, y, z in points
                if min_x <= x <= max_x and min_y <= y <= max_y and min_z <= z <= max_z
            ]

        def _shared_vertex_hit_count(hp_a, hp_b, tolerance, required_hits):
            points_a = hp_point_cache.get(hp_a)
            points_b = hp_point_cache.get(hp_b)
            if points_a is None:
                _cache_hp_points(hp_a)
                points_a = hp_point_cache.get(hp_a, [])
            if points_b is None:
                _cache_hp_points(hp_b)
                points_b = hp_point_cache.get(hp_b, [])
            if not points_a or not points_b or min(len(points_a), len(points_b)) < required_hits:
                return 0

            tolerance = max(float(tolerance), 0.000001)
            if (HAS_MATH_CORE and
                    hasattr(bg_math_core, 'calculate_coverage_stats') and
                    len(points_a) + len(points_b) >= 1024):
                coverage_a, _avg_a = bg_math_core.calculate_coverage_stats(
                    hp_flat_point_cache[hp_a], hp_flat_point_cache[hp_b], tolerance
                )
                hits_a = int(round(float(coverage_a) * len(points_a)))
                if hits_a < required_hits:
                    return hits_a
                coverage_b, _avg_b = bg_math_core.calculate_coverage_stats(
                    hp_flat_point_cache[hp_b], hp_flat_point_cache[hp_a], tolerance
                )
                hits_b = int(round(float(coverage_b) * len(points_b)))
                return min(hits_a, hits_b)

            bbox_a = hp_bbox_cache.get(hp_a)
            bbox_b = hp_bbox_cache.get(hp_b)
            points_a_in_b = _points_inside_bbox(points_a, [
                bbox_b[0] - tolerance, bbox_b[1] - tolerance, bbox_b[2] - tolerance,
                bbox_b[3] + tolerance, bbox_b[4] + tolerance, bbox_b[5] + tolerance
            ])
            points_b_in_a = _points_inside_bbox(points_b, [
                bbox_a[0] - tolerance, bbox_a[1] - tolerance, bbox_a[2] - tolerance,
                bbox_a[3] + tolerance, bbox_a[4] + tolerance, bbox_a[5] + tolerance
            ])
            if min(len(points_a_in_b), len(points_b_in_a)) < required_hits:
                return 0

            hits_a = _near_vertex_hits(points_a_in_b, hp_b, tolerance, stop_after=required_hits)
            if hits_a < required_hits:
                return hits_a
            hits_b = _near_vertex_hits(points_b_in_a, hp_a, tolerance, stop_after=required_hits)
            return min(hits_a, hits_b)

        def _compound_tolerance(hp_a, hp_b, scene_diag):
            info_a = self.hp_data.get(hp_a, {})
            info_b = self.hp_data.get(hp_b, {})
            diag_a = float(info_a.get('diag', info_a.get('radius', 1.0) * 2.0) or 0.0)
            diag_b = float(info_b.get('diag', info_b.get('radius', 1.0) * 2.0) or 0.0)
            if not _is_finite_value(diag_a):
                diag_a = 0.0001
            if not _is_finite_value(diag_b):
                diag_b = 0.0001
            diag_a = max(diag_a, 0.0001)
            diag_b = max(diag_b, 0.0001)
            pair_diag = max(min(diag_a, diag_b), 0.0001)

            scene_diag = float(scene_diag or 1.0)
            if not _is_finite_value(scene_diag):
                scene_diag = 1.0
            scene_tol = max(scene_diag * 0.001 * self.match_tolerance_scale, 0.000001)
            pair_floor = pair_diag * 0.0025
            pair_cap = pair_diag * (self.compound_link_dist_pct / 100.0)
            return min(max(scene_tol, pair_floor), pair_cap)

        compound_min_hits = self.compound_link_verts
        compound_edges = []
        compound_broad_phase_pairs = 0
        compound_bbox_rejects = 0
        compound_pair_tests = 0
        compound_hit_pairs = 0
        hp_point_cache = {}
        hp_flat_point_cache = {}
        hp_bbox_cache = {}
        hp_grid_cell_size_cache = {}
        hp_spatial_hash_cache = {}

        def _cache_hp_points(hp_name):
            if hp_name in hp_point_cache:
                return hp_point_cache[hp_name]
            flat_points = self.hp_verts_cache.get(hp_name, []) or []
            points = _flat_to_points(flat_points)
            hp_point_cache[hp_name] = points
            if len(points) * 3 == len(flat_points):
                hp_flat_point_cache[hp_name] = flat_points
            else:
                hp_flat_point_cache[hp_name] = [
                    coordinate for point in points for coordinate in point
                ]
            return points

        def _compound_padding_upper_bound(hp_name):
            info = self.hp_data.get(hp_name, {})
            diag = float(info.get('diag', info.get('radius', 1.0) * 2.0) or 0.0)
            if not _is_finite_value(diag):
                diag = 0.0001
            # _compound_tolerance is capped by min(mesh diagonals) * distance_pct.
            return max(max(diag, 0.0001) * (self.compound_link_dist_pct / 100.0), 0.000001)

        hp_scene_diags = []
        for info in self.hp_data.values():
            diag = float(info.get('diag', info.get('radius', 1.0) * 2.0) or 0.0)
            if _is_finite_value(diag) and diag > 0.001:
                hp_scene_diags.append(diag)
        hp_scene_diags.sort()
        if hp_scene_diags:
            mid = len(hp_scene_diags) // 2
            if len(hp_scene_diags) % 2:
                compound_scene_diag = hp_scene_diags[mid]
            else:
                compound_scene_diag = (hp_scene_diags[mid - 1] + hp_scene_diags[mid]) * 0.5
        else:
            compound_scene_diag = 1.0

        # Surface evidence is shared by the direct-match protection and the
        # general LP resolver.  One HP/LP pair is therefore evaluated at most
        # once even when it participates in a compound component.
        surface_match_cache = {}
        surface_match_stats = {'tests': 0}
        hp_resolution_evidence = {}
        ambiguous_hp_matches = set()

        def _surface_match_evidence(hp_name, lp_name):
            if self.is_cancelled or self.isInterruptionRequested():
                return None
            cache_key = (hp_name, lp_name)
            if cache_key in surface_match_cache:
                return surface_match_cache[cache_key]
            if not (HAS_MATH_CORE and hasattr(bg_math_core, 'calculate_surface_match')):
                surface_match_cache[cache_key] = None
                return None

            hp_proxy = self.hp_surface_cache.get(hp_name) or {}
            lp_proxy = self.lp_surface_cache.get(lp_name) or {}
            hp_samples = hp_proxy.get('samples') or []
            hp_triangles = hp_proxy.get('triangles') or []
            lp_samples = lp_proxy.get('samples') or []
            lp_triangles = lp_proxy.get('triangles') or []
            if not hp_samples or not hp_triangles or not lp_samples or not lp_triangles:
                surface_match_cache[cache_key] = None
                return None

            hp_info = self.hp_data.get(hp_name, {})
            lp_info = self.lp_data.get(lp_name, {})
            hp_diag = max(float(hp_info.get('diag', hp_info.get('radius', 1.0) * 2.0) or 0.0), 0.000001)
            lp_diag = max(float(lp_info.get('diag', lp_info.get('radius', 1.0) * 2.0) or 0.0), 0.000001)
            pair_diag = max(min(hp_diag, lp_diag), 0.000001)
            tolerance = max(pair_diag * 0.025 * self.match_tolerance_scale,
                            compound_scene_diag * 0.00005, 0.000001)
            try:
                metrics = bg_math_core.calculate_surface_match(
                    hp_samples, hp_triangles,
                    lp_samples, lp_triangles,
                    tolerance
                )
                surface_match_stats['tests'] += 1
                hp_distance = float(metrics.get('hp_to_lp_distance', float('inf')))
                lp_distance = float(metrics.get('lp_to_hp_distance', float('inf')))
                hp_coverage = float(metrics.get('hp_coverage', 0.0))
                lp_coverage = float(metrics.get('lp_coverage', 0.0))
                average_distance = float(metrics.get('average_distance', float('inf')))
                coverage = float(metrics.get('coverage', min(hp_coverage, lp_coverage)))
            except Exception:
                surface_match_cache[cache_key] = None
                return None

            if (not all(math.isfinite(value) for value in (hp_distance, lp_distance, average_distance, hp_coverage, lp_coverage, coverage))
                    or hp_distance < 0.0 or lp_distance < 0.0
                    or not 0.0 <= hp_coverage <= 1.0 or not 0.0 <= lp_coverage <= 1.0):
                surface_match_cache[cache_key] = None
                return None
            size_ratio = min(hp_diag, lp_diag) / max(hp_diag, lp_diag)
            distance_quality = max(0.0, 1.0 - min(hp_distance / max(tolerance * 1.5, 0.000001), 1.0))
            confidence = max(0.0, min(100.0, 100.0 * (
                hp_coverage * 0.68 + distance_quality * 0.22 + size_ratio * 0.10
            )))
            evidence = {
                'lp_name': lp_name,
                'hp_to_lp_distance': hp_distance,
                'lp_to_hp_distance': lp_distance,
                'average_distance': average_distance,
                'hp_coverage': hp_coverage,
                'lp_coverage': lp_coverage,
                'coverage': coverage,
                'tolerance': tolerance,
                'size_ratio': size_ratio,
                'confidence': confidence,
                # HP coverage is directional on purpose: a valid LP may contain
                # several HP pieces, so low LP->HP coverage is not a rejection.
                'accepted': hp_coverage >= 0.70 and hp_distance <= tolerance * 1.35,
                'excellent': hp_coverage >= 0.84 and hp_distance <= tolerance * 1.10,
                'ambiguous': False,
                'confidence_margin': None,
            }
            surface_match_cache[cache_key] = evidence
            return evidence

        self.progress_value.emit(18)
        self.progress_text.emit("Step 2.2: Compound HP vertex linking...")

        direct_surface_matches = find_confident_direct_matches(
            hp_candidates,
            self.hp_data,
            self.lp_data,
            self.hp_verts_cache,
            self.lp_verts_cache,
            cancelled_getter=lambda: (
                self.is_cancelled or self.isInterruptionRequested()
            ),
        )
        for hp_name in list(direct_surface_matches.keys()):
            hp_uid = self.hp_data.get(hp_name, {}).get('uuid')
            if hp_uid and hp_uid in self.manual_uuid_set:
                direct_surface_matches.pop(hp_name, None)
        direct_protected_hps = set(direct_surface_matches.keys())
        compound_direct_owners = dict((name, match["lp"]) for name, match in direct_surface_matches.items())
        if direct_surface_matches:
            logs.append(
                "Direct surface protection: kept {} high-confidence conflicting HP/LP pair(s) outside adjacent linking.".format(
                    len(direct_surface_matches)
                )
            )
            for hp_name, match in sorted(direct_surface_matches.items()):
                _debug(
                    "  DIRECT_SURFACE: HP='{}' -> LP='{}' | confidence={:.3f} | coverage={:.3f}/{:.3f} | avg={:.6f} | tolerance={:.6f}".format(
                        _short_name(hp_name),
                        _short_name(match['lp']),
                        match['confidence'],
                        match['hp_coverage'],
                        match['lp_coverage'],
                        match['average_distance'],
                        match['tolerance'],
                    )
                )
        else:
            _debug("Step 2.2: direct surface protection found no conflict-level match; normal LP matching remains authoritative.")

        hp_to_lp_candidate_sets = {}
        for hp_name, candidates in hp_candidates.items():
            if not candidates:
                continue
            if hp_name in direct_protected_hps:
                continue
            if _is_zbrush_hp(hp_name):
                continue
            hp_uid = self.hp_data.get(hp_name, {}).get('uuid')
            if hp_uid and hp_uid in self.manual_uuid_set:
                continue
            if not _cache_hp_points(hp_name):
                continue
            hp_bbox = _base_bbox(self.hp_data.get(hp_name, {}))
            if hp_bbox is None:
                continue
            hp_bbox_cache[hp_name] = hp_bbox
            hp_grid_cell_size_cache[hp_name] = _compound_padding_upper_bound(hp_name)
            stable_candidates = sorted(set(candidates), key=str)
            hp_to_lp_candidate_sets[hp_name] = frozenset(stable_candidates)

        compound_parent = {}

        def _compound_find(name):
            compound_parent.setdefault(name, name)
            if compound_parent[name] != name:
                compound_parent[name] = _compound_find(compound_parent[name])
            return compound_parent[name]

        def _compound_union(a, b):
            root_a = _compound_find(a)
            root_b = _compound_find(b)
            if root_a != root_b:
                compound_parent[root_b] = root_a

        # Sweep all eligible HP bboxes once.  A pair is relevant only when its
        # two meshes share at least one LP candidate; checking that intersection
        # is much cheaper than rerunning the same sweep for every LP.
        compound_hp_names = sorted(hp_bbox_cache.keys(), key=str)
        padding_by_name = dict(
            (name, hp_grid_cell_size_cache[name]) for name in compound_hp_names
        )
        for pair_index, (hp_a, hp_b) in enumerate(bbox_sweep_candidate_pairs(
                compound_hp_names, hp_bbox_cache, padding_by_name)):
            if pair_index % 64 == 0:
                if self.is_cancelled or self.isInterruptionRequested():
                    return
                self.progress_value.emit(18 + min(5, pair_index // 256))
            if hp_to_lp_candidate_sets[hp_a].isdisjoint(hp_to_lp_candidate_sets[hp_b]):
                continue
            pair_key = (hp_a, hp_b)
            compound_broad_phase_pairs += 1
            tolerance = _compound_tolerance(hp_a, hp_b, compound_scene_diag)
            if not bboxes_within_tolerance(
                    hp_bbox_cache[hp_a], hp_bbox_cache[hp_b], max(tolerance, 0.000001)):
                compound_bbox_rejects += 1
                continue
            compound_pair_tests += 1
            shared_hits = _shared_vertex_hit_count(
                hp_a, hp_b, tolerance, compound_min_hits
            )
            if shared_hits >= compound_min_hits:
                _compound_union(hp_a, hp_b)
                compound_edges.append(pair_key)
                compound_hit_pairs += 1
                _debug("  COMPOUND_EDGE: '{}' <-> '{}' | shared_vertices={} | tolerance={:.6f}".format(
                    _short_name(hp_a), _short_name(hp_b), shared_hits, tolerance
                ))
        self.progress_value.emit(24)

        compound_components_by_root = {}
        for hp_a, hp_b in compound_edges:
            compound_components_by_root.setdefault(_compound_find(hp_a), set()).add(hp_a)
            compound_components_by_root.setdefault(_compound_find(hp_b), set()).add(hp_b)

        compound_components = []
        for component in compound_components_by_root.values():
            component = sorted(component)
            if len(component) < 2:
                continue
            compound_components.append(component)
        compound_components.sort(key=lambda component: tuple(component))

        if compound_components:
            logs.append(
                "Compound HP vertex linking: created {} pre-resolve compound component(s) from {} matching pair(s), min hits={}.".format(
                    len(compound_components), compound_hit_pairs, compound_min_hits
                )
            )
            for idx, component in enumerate(compound_components, 1):
                _debug_list("  COMPOUND_COMPONENT_{}: ".format(idx), component)
        elif compound_pair_tests:
            logs.append(
                "Compound HP vertex linking: tested {} HP pair(s), no pairs reached {} shared vertices.".format(
                    compound_pair_tests, compound_min_hits
                )
            )
        _debug("Step 2.2: broad candidates={} | bbox rejects={} | precise tests={} | matched={} | components={}.".format(
            compound_broad_phase_pairs,
            compound_bbox_rejects,
            compound_pair_tests,
            compound_hit_pairs,
            len(compound_components)
        ))
        _debug("")

        def _resolve_hp_to_lp(hp_name, candidates):
            if not candidates:
                return None, False, "no LP candidate"

            candidates = sorted(set(candidates), key=lambda name: (_short_name(name), str(name)))
            best_lp = None
            hp_verts = self.hp_verts_cache.get(hp_name, [])
            method = "single candidate"
            validation = "not tested"
            # Мемоизация avg_distance(hp, lp) по кандидату: min() перебирает каждого
            # один раз, а победитель переиспользуется в валидации ниже (без 2-3 лишних
            # C++-вызовов на плотных мешах).
            mat_score_cache = {}

            material_candidates = [c for c in candidates if _lp_material_slot(c)]
            surface_candidates = material_candidates or candidates
            surface_records = []
            for lp_name in surface_candidates:
                evidence = _surface_match_evidence(hp_name, lp_name)
                if evidence:
                    surface_records.append(evidence)
            accepted_surface_records = [record for record in surface_records if record.get('accepted')]
            accepted_surface_records.sort(key=lambda record: (
                -float(record.get('confidence', 0.0)),
                -float(record.get('hp_coverage', 0.0)),
                float(record.get('hp_to_lp_distance', float('inf'))),
                _short_name(record.get('lp_name')),
                str(record.get('lp_name')),
            ))

            if accepted_surface_records:
                best_record = dict(accepted_surface_records[0])
                runner_up = accepted_surface_records[1] if len(accepted_surface_records) > 1 else None
                if runner_up and _lp_base_name(best_record['lp_name']) != _lp_base_name(runner_up['lp_name']):
                    confidence_margin = float(best_record['confidence']) - float(runner_up['confidence'])
                    coverage_margin = float(best_record['hp_coverage']) - float(runner_up['hp_coverage'])
                    distance_margin = float(runner_up['hp_to_lp_distance']) - float(best_record['hp_to_lp_distance'])
                    ambiguous = (
                        confidence_margin < 8.0
                        and coverage_margin < 0.07
                        and distance_margin < float(best_record['tolerance']) * 0.35
                    )
                    best_record['confidence_margin'] = confidence_margin
                    best_record['ambiguous'] = ambiguous
                    if ambiguous:
                        ambiguous_hp_matches.add(hp_name)
                hp_resolution_evidence[hp_name] = best_record
                best_lp = best_record['lp_name']
                method = "directional surface evidence"
            elif surface_records:
                surface_records.sort(key=lambda record: (
                    -float(record.get('confidence', 0.0)),
                    _short_name(record.get('lp_name')),
                    str(record.get('lp_name')),
                ))
                hp_resolution_evidence[hp_name] = dict(surface_records[0])

            protected_match = direct_surface_matches.get(hp_name)
            if best_lp is None and protected_match and protected_match.get('lp') in candidates:
                best_lp = protected_match['lp']
                method = "confident direct surface match"

            if best_lp is None and len(candidates) == 1:
                best_lp = candidates[0]
            elif best_lp is None:
                if material_candidates and HAS_MATH_CORE and hp_verts:
                    def material_distance_score(lp_name):
                        if lp_name in mat_score_cache:
                            return mat_score_cache[lp_name]
                        lp_verts = self.lp_verts_cache.get(lp_name, [])
                        if not lp_verts:
                            mat_score_cache[lp_name] = float('inf')
                            return float('inf')
                        try:
                            score = bg_math_core.calculate_avg_distance(hp_verts, lp_verts)
                        except Exception:
                            score = float('inf')
                        mat_score_cache[lp_name] = score
                        return score

                    best_lp = min(material_candidates, key=material_distance_score)
                    if material_distance_score(best_lp) == float('inf'):
                        best_lp = None
                    method = "material distance"
                if best_lp is None and self.strategy == 0:
                    hp_info = self.hp_data[hp_name]
                    hp_diag = max(hp_info.get("diag", hp_info.get("radius", 1.0) * 2.0), 0.001)
                    hp_vol = max(hp_info.get("bbox_vol", hp_info.get("volume", 0.0)), 0.0001)

                    def spatial_score(lp_name):
                        lp_info = self.lp_data[lp_name]
                        lp_vol = max(lp_info.get("bbox_vol", lp_info.get("volume", 0.0)), 0.0001)
                        volume_score = abs(hp_vol - lp_vol) / max(hp_vol, lp_vol, 0.0001)
                        center_score = _center_dist(hp_info, lp_info) / hp_diag
                        return center_score + volume_score

                    best_lp = min(candidates, key=spatial_score)
                    method = "spatial score"
                elif best_lp is None and self.strategy == 2:
                    hp_info = self.hp_data[hp_name]
                    hp_vtx = max(hp_info.get("vtx", 0), 1)
                    hp_edges = max(hp_info.get("edges", 0), 1)

                    def topology_score(lp_name):
                        lp_info = self.lp_data[lp_name]
                        vtx_score = abs(hp_info.get("vtx", 0) - lp_info.get("vtx", 0)) / float(max(hp_vtx, lp_info.get("vtx", 1), 1))
                        edge_score = abs(hp_info.get("edges", 0) - lp_info.get("edges", 0)) / float(max(hp_edges, lp_info.get("edges", 1), 1))
                        return vtx_score + edge_score

                    best_lp = min(candidates, key=topology_score)
                    method = "topology score"
                elif best_lp is None and HAS_MATH_CORE and hp_verts:
                    lp_candidates_verts = [self.lp_verts_cache.get(c, []) for c in candidates]
                    best_idx = bg_math_core.resolve_hp_collision(hp_verts, lp_candidates_verts)
                    if 0 <= best_idx < len(candidates):
                        best_lp = candidates[best_idx]
                        method = "C++ vertex collision resolve"
                    else:
                        best_lp = candidates[0]
                        method = "fallback first candidate"
                elif best_lp is None:
                    best_lp = candidates[0]
                    method = "fallback first candidate"

            assigned_to_lp = False
            selected_evidence = hp_resolution_evidence.get(hp_name) or {}
            if selected_evidence.get('lp_name') == best_lp and selected_evidence.get('accepted'):
                assigned_to_lp = True
                validation = (
                    "surface_hp_coverage={:.3f}, surface_distance={:.6f}, threshold={:.6f}, confidence={:.1f}{}"
                    .format(
                        float(selected_evidence.get('hp_coverage', 0.0)),
                        float(selected_evidence.get('hp_to_lp_distance', float('inf'))),
                        float(selected_evidence.get('tolerance', 0.0)) * 1.35,
                        float(selected_evidence.get('confidence', 0.0)),
                        " | ambiguous" if selected_evidence.get('ambiguous') else ""
                    )
                )
            if HAS_MATH_CORE and hp_verts:
                best_lp_verts = self.lp_verts_cache.get(best_lp, [])
                if best_lp_verts:
                    cached_score = mat_score_cache.get(best_lp)
                    if cached_score is not None and cached_score != float('inf'):
                        avg_dist = cached_score
                    else:
                        avg_dist = bg_math_core.calculate_avg_distance(hp_verts, best_lp_verts)
                    hp_info = self.hp_data[hp_name]
                    best_lp_info = self.lp_data[best_lp]

                    hp_diag = hp_info.get("diag", 1.0)
                    lp_diag = best_lp_info.get("diag", 1.0)
                    is_zb = hp_info.get('is_zbrush', False) or 'zbrush' in hp_name.lower()
                    threshold = ((hp_diag * (1.25 if is_zb else 0.5)) + (lp_diag * (0.75 if is_zb else 0.25))) * self.match_tolerance_scale

                    if avg_dist <= threshold:
                        assigned_to_lp = True
                    if not selected_evidence.get('accepted'):
                        validation = "avg_distance={:.6f}, threshold={:.6f}".format(avg_dist, threshold)
                else:
                    if not assigned_to_lp:
                        validation = "best LP has no vertex cache"
            else:
                if not assigned_to_lp:
                    validation = "C++ core or HP vertex cache unavailable"

            # Fallback for ZBrush or missing/too sparse vertex cache: accept the chosen candidate.
            if not assigned_to_lp:
                hp_info = self.hp_data[hp_name]
                is_zb = hp_info.get('is_zbrush', False) or 'zbrush' in hp_name.lower()
                if (not HAS_MATH_CORE) or (not hp_verts) or is_zb:
                    assigned_to_lp = True
                    validation += " | accepted by fallback"

            return best_lp, assigned_to_lp, "{} | {}".format(method, validation)

        def _lp_claim_score(hp_name, lp_name, reason, candidate_count, evidence=None):
            evidence = evidence or {}
            if evidence.get('lp_name') == lp_name and evidence.get('accepted'):
                score = float(evidence.get('confidence', 0.0) or 0.0)
                if candidate_count == 1:
                    score += 5.0
                if evidence.get('ambiguous'):
                    score = min(score, 55.0)
                return max(0.0, min(100.0, score))

            score = 0.0
            reason = reason or ""
            match = re.search(r"avg_distance=([0-9eE+\-.]+), threshold=([0-9eE+\-.]+)", reason)
            if match:
                try:
                    avg_distance = float(match.group(1))
                    threshold = max(float(match.group(2)), 0.000001)
                    score += max(0.0, 100.0 * (1.0 - min(avg_distance / threshold, 1.0)))
                except Exception:
                    pass
            else:
                hp_info = self.hp_data.get(hp_name, {})
                lp_info = self.lp_data.get(lp_name, {})
                scale = max(
                    hp_info.get("diag", hp_info.get("radius", 1.0) * 2.0),
                    lp_info.get("diag", lp_info.get("radius", 1.0) * 2.0),
                    0.001
                )
                score += max(0.0, 50.0 * (1.0 - min(_center_dist(hp_info, lp_info) / scale, 1.0)))

            if candidate_count == 1:
                score += 25.0
            elif candidate_count > 1:
                score += max(0.0, 15.0 / float(candidate_count))

            if "accepted by fallback" in reason:
                score *= 0.65
            return score

        processing_units = []
        used_compound_members = set()
        material_first_pass = bool(lp_material_slots)
        if material_first_pass:
            for hp_name in hp_candidates.keys():
                processing_units.append([hp_name])
            logs.append("LP material-aware pass: resolving HP material ownership before compound grouping.")
            _debug("Step 2.3: material-aware mode keeps HP resolve units separate before packing.")
        else:
            for component in compound_components:
                processing_units.append(list(component))
                used_compound_members.update(component)
            for hp_name in hp_candidates.keys():
                if hp_name not in used_compound_members:
                    processing_units.append([hp_name])

        total_units = len(processing_units)
        for idx, hp_unit in enumerate(processing_units):
            if self.is_cancelled:
                return
            if idx % max(1, total_units // 20) == 0:
                self.progress_value.emit(24 + int((idx / float(max(total_units, 1))) * 11))

            hp_unit = [hp for hp in hp_unit if hp in hp_candidates]
            if not hp_unit:
                continue

            ordered_unit = sorted(
                hp_unit,
                key=lambda name: self.hp_data.get(name, {}).get("bbox_vol", self.hp_data.get(name, {}).get("volume", 0.0)),
                reverse=True
            )

            best_lp = None
            driver_hp = None
            driver_reason = ""
            for hp_name in ordered_unit:
                candidate_lp, assigned, reason = _resolve_hp_to_lp(hp_name, hp_candidates.get(hp_name, []))
                _debug("  RESOLVE: HP='{}' | unit_size={} | candidates={} | best_lp='{}' | assigned={} | reason={}".format(
                    _short_name(hp_name),
                    len(ordered_unit),
                    len(hp_candidates.get(hp_name, [])),
                    _short_name(candidate_lp) if candidate_lp else "none",
                    "yes" if assigned else "no",
                    reason
                ))
                if assigned and candidate_lp:
                    best_lp = candidate_lp
                    driver_hp = hp_name
                    driver_reason = reason
                    break

            if not best_lp:
                _debug_list("  UNRESOLVED_UNIT: ", ordered_unit)
                continue

            assigned_in_unit = 0
            pulled_in_unit = 0
            split_in_unit = 0
            skipped_in_unit = 0
            for hp_name in ordered_unit:
                if hp_name not in unassigned_hps:
                    continue

                member_lp = best_lp
                member_driver = driver_hp
                member_reason = driver_reason
                member_source = "compound" if len(ordered_unit) > 1 else "lp"

                if hp_name != driver_hp:
                    member_direct_owner = compound_direct_owners.get(hp_name)
                    if member_direct_owner:
                        member_lp = member_direct_owner
                        member_driver = hp_name
                        member_reason = "surface direct match"
                        member_source = "lp"
                        member_evidence = _surface_match_evidence(hp_name, member_direct_owner)
                        if member_evidence:
                            hp_resolution_evidence[hp_name] = dict(member_evidence)
                    else:
                        resolved_lp, resolved_member, resolved_reason = _resolve_hp_to_lp(
                            hp_name, hp_candidates.get(hp_name, [])
                        )
                        if resolved_member and resolved_lp:
                            member_lp = resolved_lp
                            member_driver = hp_name
                            member_reason = resolved_reason
                            member_source = "lp" if resolved_lp != best_lp else "compound"
                        elif best_lp in hp_candidates.get(hp_name, []) and hp_name not in ambiguous_hp_matches:
                            # A compound edge may support a weak fragment, but
                            # only when that fragment independently overlaps the
                            # same LP candidate.  Transitive components can no
                            # longer pull every member to the driver's LP.
                            member_reason = "compound geometric support via '{}' | {}".format(
                                _short_name(driver_hp), resolved_reason
                            )
                            member_source = "compound"
                        else:
                            skipped_in_unit += 1
                            _debug("  COMPOUND_MEMBER_DEFER: HP='{}' | driver='{}' LP='{}' | reason={}".format(
                                _short_name(hp_name),
                                _short_name(driver_hp),
                                _short_name(best_lp),
                                resolved_reason
                            ))
                            continue

                if member_lp not in lp_to_owned_hps:
                    skipped_in_unit += 1
                    continue
                lp_to_owned_hps[member_lp].append(self.hp_data[hp_name])
                unassigned_hps.remove(hp_name)
                evidence = hp_resolution_evidence.get(hp_name) or {}
                if evidence.get('lp_name') != member_lp:
                    evidence = {}
                hp_claims[hp_name] = {
                    "owner_lp": member_lp,
                    "material_slot": _lp_material_slot(member_lp),
                    "material_name": self.lp_data.get(member_lp, {}).get("material_name"),
                    "candidate_lps": list(hp_candidates.get(hp_name, [])),
                    "candidate_count": len(hp_candidates.get(hp_name, [])),
                    "driver_hp": member_driver,
                    "source": member_source,
                    "reason": member_reason,
                    "score": _lp_claim_score(
                        hp_name,
                        member_lp,
                        member_reason,
                        len(hp_candidates.get(hp_name, [])),
                        evidence
                    ),
                    "confidence": float(evidence.get('confidence', 0.0) or 0.0),
                    "ambiguous": bool(evidence.get('ambiguous', False)),
                    "evidence": dict(evidence),
                }
                assigned_in_unit += 1
                if hp_name != driver_hp and member_lp == best_lp:
                    pulled_in_unit += 1
                elif member_lp != best_lp:
                    split_in_unit += 1

            if len(ordered_unit) > 1:
                _debug("  COMPOUND_ASSIGN: driver='{}' -> LP='{}' | assigned={} pulled={} split={} deferred={} | reason={}".format(
                    _short_name(driver_hp),
                    _short_name(best_lp),
                    assigned_in_unit,
                    pulled_in_unit,
                    split_in_unit,
                    skipped_in_unit,
                    driver_reason
                ))
            else:
                _debug("  SINGLE_ASSIGN: HP='{}' -> LP='{}' | reason={}".format(
                    _short_name(ordered_unit[0]),
                    _short_name(best_lp),
                    driver_reason
                ))

        accepted_surface_count = sum(
            1 for evidence in hp_resolution_evidence.values()
            if evidence.get('accepted')
        )
        logs.append(
            "LP ownership evidence: {} surface pair test(s), {} accepted HP claim(s), {} ambiguous claim(s).".format(
                surface_match_stats.get('tests', 0),
                accepted_surface_count,
                len(ambiguous_hp_matches)
            )
        )
        if ambiguous_hp_matches:
            _debug_list("  LP_AMBIGUOUS_PROTECTED: ", sorted(ambiguous_hp_matches, key=_short_name))


        def _step2_5_floater_decal_pass():
            self.progress_value.emit(35)
            self.progress_text.emit("Step 2.5: Analyzing HP holes for floater/decal matching...")

            # --- STEP 2.5 ---
            if self.detect_floaters and HAS_MATH_CORE:
                hp_owner_lp = {}
                for _lp_name, _owned_hps in lp_to_owned_hps.items():
                    for _hp_info in _owned_hps:
                        _hp_name = _hp_info.get("name")
                        if _hp_name:
                            hp_owner_lp[_hp_name] = _lp_name

                def _sample_flat_verts(flat_verts, max_points=700):
                    return sample_flat_vertices(flat_verts, max_points)

                def _hole_entry(hp_name):
                    return self.hp_holes_cache.get(hp_name)

                def _hole_points_from_entry(entry):
                    if isinstance(entry, dict):
                        return entry.get("points") or entry.get("boundary_verts") or []
                    return entry or []

                def _hole_points(hp_name):
                    return _hole_points_from_entry(_hole_entry(hp_name))

                def _has_hole(hp_name):
                    return bool(_hole_points(hp_name))

                def _hole_orientation(hp_name):
                    entry = _hole_entry(hp_name)
                    if isinstance(entry, dict):
                        return entry.get("orientation") or "unknown"
                    return "unknown"

                def _hole_orientation_score(hp_name):
                    entry = _hole_entry(hp_name)
                    if isinstance(entry, dict):
                        try:
                            return float(entry.get("orientation_score", 0.0) or 0.0)
                        except Exception:
                            return 0.0
                    return 0.0

                def _bbox_overlaps_expanded(info_a, info_b, padding):
                    bb_a = info_a.get("bbox", [0, 0, 0, 0, 0, 0])
                    bb_b = info_b.get("bbox", [0, 0, 0, 0, 0, 0])
                    if not bb_a or not bb_b or len(bb_a) != 6 or len(bb_b) != 6:
                        return False
                    return not (
                        bb_a[3] + padding < bb_b[0] or bb_a[0] - padding > bb_b[3] or
                        bb_a[4] + padding < bb_b[1] or bb_a[1] - padding > bb_b[4] or
                        bb_a[5] + padding < bb_b[2] or bb_a[2] - padding > bb_b[5]
                    )

                def _bbox_intersection_fraction(child_info, parent_info, padding):
                    child_bb = child_info.get("bbox", [0, 0, 0, 0, 0, 0])
                    parent_bb = parent_info.get("bbox", [0, 0, 0, 0, 0, 0])
                    if not child_bb or not parent_bb or len(child_bb) != 6 or len(parent_bb) != 6:
                        return 0.0
                    expanded_parent = [
                        parent_bb[0] - padding, parent_bb[1] - padding, parent_bb[2] - padding,
                        parent_bb[3] + padding, parent_bb[4] + padding, parent_bb[5] + padding
                    ]
                    ix = max(0.0, min(child_bb[3], expanded_parent[3]) - max(child_bb[0], expanded_parent[0]))
                    iy = max(0.0, min(child_bb[4], expanded_parent[4]) - max(child_bb[1], expanded_parent[1]))
                    iz = max(0.0, min(child_bb[5], expanded_parent[5]) - max(child_bb[2], expanded_parent[2]))
                    child_vol = max(
                        (child_bb[3] - child_bb[0]) *
                        (child_bb[4] - child_bb[1]) *
                        (child_bb[5] - child_bb[2]),
                        0.000001
                    )
                    return max(0.0, min(1.0, (ix * iy * iz) / child_vol))

                def _surface_near_coverage(source_verts, target_verts, threshold, max_source=180, max_target=900):
                    return calculate_surface_near_coverage(
                        source_verts,
                        target_verts,
                        threshold,
                        max_source=max_source,
                        max_target=max_target,
                        cancelled_getter=lambda: (
                            self.is_cancelled or self.isInterruptionRequested()
                        )
                    )

                hp_vtx_frequency = {}
                for _hp_info in self.hp_data.values():
                    _vtx = _hp_info.get("vtx")
                    if _vtx is not None:
                        hp_vtx_frequency[_vtx] = hp_vtx_frequency.get(_vtx, 0) + 1

                repeated_fastener_cache = {}

                def _is_repeated_fastener_like(hp_name, hp_info, hp_verts):
                    if hp_name in repeated_fastener_cache:
                        return repeated_fastener_cache[hp_name]
                    repeated = hp_vtx_frequency.get(hp_info.get("vtx"), 0) >= 4
                    result = False
                    if repeated and hp_verts:
                        try:
                            metrics = _analyze_shape_metrics(_sample_flat_verts(hp_verts, 600))
                            result = (
                                float(metrics.elongation) < self.bolt_elongation
                                and (not self.use_symmetry or float(metrics.symmetry_score) < self.bolt_symmetry)
                            )
                        except Exception:
                            result = False
                    repeated_fastener_cache[hp_name] = result
                    return result

                all_hp_names = sorted(
                    (hp_name for hp_name in self.hp_data.keys() if hp_name in hp_owner_lp),
                    key=str
                )
                weak_floater_candidates = set(unassigned_hps)
                unstable_floater_parents = set(unassigned_hps)
                floater_candidate_reason = {hp_name: "no_lp" for hp_name in unassigned_hps}
                hole_classify_debug = []
                hole_keep_own_lp_count = 0
                hole_inward_no_lp_count = 0
                hole_outward_count = 0
                hole_legacy_count = 0

                def _has_own_lp_claim(hp_name, claim):
                    if not claim or not claim.get("owner_lp"):
                        return False
                    try:
                        score = float(claim.get("score", 0.0) or 0.0)
                    except Exception:
                        score = 0.0
                    source = claim.get("source")
                    if claim.get("ambiguous") and source in (
                        "lp", "compound", "manual", "custom", "gt"
                    ):
                        # Ambiguous means "two plausible own LPs", not "this is
                        # a floater".  Keep it in the deterministic LP result and
                        # expose the uncertainty in diagnostics instead of
                        # silently attaching it to an unrelated HP parent.
                        return True
                    # Candidate count only describes overlapping LP bounding
                    # boxes. Once the geometry resolver produced a confident
                    # direct claim, nearby LP boxes must not turn that HP into
                    # somebody else's floater.
                    return score >= 45.0 and source in (
                        "lp", "compound", "manual", "custom", "gt"
                    )

                def _has_scale_matched_lp_claim(hp_name, claim):
                    """Return True when HP and its resolved LP have comparable bounds."""
                    if not claim or not claim.get("owner_lp"):
                        return False
                    try:
                        score = float(claim.get("score", 0.0) or 0.0)
                    except Exception:
                        score = 0.0
                    if score < 45.0:
                        return False
                    hp_info = self.hp_data.get(hp_name, {})
                    lp_info = self.lp_data.get(claim.get("owner_lp"), {})
                    hp_diag = float(hp_info.get("diag", hp_info.get("radius", 0.0) * 2.0) or 0.0)
                    lp_diag = float(lp_info.get("diag", lp_info.get("radius", 0.0) * 2.0) or 0.0)
                    if hp_diag <= 0.0 or lp_diag <= 0.0:
                        return False
                    return min(hp_diag, lp_diag) / max(hp_diag, lp_diag) >= 0.60

                def _mark_floater_candidate(hp_name, reason, unstable_parent=True):
                    weak_floater_candidates.add(hp_name)
                    floater_candidate_reason[hp_name] = reason
                    if unstable_parent:
                        unstable_floater_parents.add(hp_name)

                for _hp_name in sorted(unassigned_hps, key=str):
                    if _hp_name not in self.hp_data or not _has_hole(_hp_name):
                        continue
                    _hole_mode = _hole_orientation(_hp_name)
                    if _hole_mode == "inward":
                        _mark_floater_candidate(_hp_name, "inward_hole_no_lp")
                        hole_inward_no_lp_count += 1
                        _hole_action = "attach_no_lp"
                    elif _hole_mode == "outward":
                        _mark_floater_candidate(_hp_name, "outward_hole")
                        hole_outward_count += 1
                        _hole_action = "attach_outward"
                    else:
                        continue
                    if len(hole_classify_debug) < 80:
                        hole_classify_debug.append(
                            "  HOLE_CLASSIFY: HP='{}' | orientation={} score={:.3f} | owner_lp='none' score=0.0 | action={}".format(
                                _short_name(_hp_name),
                                _hole_mode,
                                _hole_orientation_score(_hp_name),
                                _hole_action
                            )
                        )

                for _hp_name, _claim in hp_claims.items():
                    if _hp_name not in self.hp_data:
                        continue
                    if _hp_name in unassigned_hps:
                        continue
                    if _claim.get("source") == "floater":
                        continue
                    _claim_score_value = float(_claim.get("score", 0.0) or 0.0)
                    _has_open_hole = _has_hole(_hp_name)
                    _hole_mode = _hole_orientation(_hp_name) if _has_open_hole else "none"
                    _has_own_lp = _has_own_lp_claim(_hp_name, _claim)
                    if _has_open_hole:
                        _hole_action = "legacy"
                        if _hole_mode == "inward" and _has_own_lp:
                            hole_keep_own_lp_count += 1
                            _hole_action = "keep_own_lp"
                        elif _hole_mode == "inward":
                            _mark_floater_candidate(_hp_name, "inward_hole_no_lp")
                            hole_inward_no_lp_count += 1
                            _hole_action = "attach_no_lp"
                        elif _hole_mode == "outward":
                            _mark_floater_candidate(_hp_name, "outward_hole")
                            hole_outward_count += 1
                            _hole_action = "attach_outward"

                        if _hole_action != "legacy":
                            if len(hole_classify_debug) < 80:
                                hole_classify_debug.append(
                                    "  HOLE_CLASSIFY: HP='{}' | orientation={} score={:.3f} | owner_lp='{}' score={:.1f} | action={}".format(
                                        _short_name(_hp_name),
                                        _hole_mode,
                                        _hole_orientation_score(_hp_name),
                                        _short_name(_claim.get("owner_lp")) if _claim.get("owner_lp") else "none",
                                        _claim_score_value,
                                        _hole_action
                                    )
                                )
                            if _hole_action == "keep_own_lp":
                                continue
                            if _hole_action in ("attach_no_lp", "attach_outward"):
                                continue

                    if _claim_score_value <= 30.0:
                        _mark_floater_candidate(_hp_name, "weak_lp_claim")
                        continue
                    # A mesh with an open border can still be a dependent detail, but
                    # only weak or non-unique claims should be reconsidered here.
                    if _has_open_hole and (
                        _claim_score_value <= 70.0 or int(_claim.get("candidate_count", 0) or 0) != 1
                    ):
                        _mark_floater_candidate(_hp_name, "legacy_hole_weak")
                        hole_legacy_count += 1
                for _line in hole_classify_debug:
                    _debug(_line)
                if hole_keep_own_lp_count or hole_inward_no_lp_count or hole_outward_count or hole_legacy_count:
                    _debug("Step 2.5: hole classification | keep_own_lp={} | inward_no_lp={} | outward={} | legacy_weak={}.".format(
                        hole_keep_own_lp_count,
                        hole_inward_no_lp_count,
                        hole_outward_count,
                        hole_legacy_count
                    ))
                floaters_assigned = set()
                floater_parent_tests = 0
                floater_distance_tests = 0
                floater_surface_tests = 0
                floater_surface_matches = 0
                floater_bbox_rejects = 0
                floater_assigned_count = 0
                floater_reassigned_count = 0
                floater_size_rejects = 0
                floater_context_rejects = 0
                floater_ambiguous_rejects = 0
                floater_scan_limited = False
                distance_test_limit = max(250, min(8000, len(all_hp_names) * max(len(weak_floater_candidates), 1)))

                floater_link_candidates = {}

                def _claim_score(hp_name):
                    try:
                        return float(hp_claims.get(hp_name, {}).get("score", 0.0) or 0.0)
                    except Exception:
                        return 0.0

                def _claim_source(hp_name):
                    return hp_claims.get(hp_name, {}).get("source")

                def _is_manual_hp(hp_name):
                    uid = self.hp_data.get(hp_name, {}).get("uuid")
                    return bool(uid and uid in self.manual_uuid_set)

                def _add_floater_candidate(floater_name, record):
                    floater_link_candidates.setdefault(floater_name, []).append(record)

                for parent_idx, parent_hp in enumerate(all_hp_names):
                    if self.is_cancelled: return True
                    if floater_scan_limited:
                        break
                    if parent_hp in floaters_assigned: continue
                    if parent_idx % max(1, len(all_hp_names) // 20) == 0:
                        self.progress_value.emit(35 + int((parent_idx / float(max(len(all_hp_names), 1))) * 5))

                    hole_boundary_verts = _hole_points(parent_hp)
                    parent_info = self.hp_data.get(parent_hp, {})
                    parent_verts = self.hp_verts_cache.get(parent_hp, [])
                    parent_lp_name = hp_owner_lp.get(parent_hp)
                    if not parent_lp_name:
                        continue
                    if _is_manual_hp(parent_hp):
                        continue
                    parent_claim = hp_claims.get(parent_hp, {})
                    if (
                        _has_scale_matched_lp_claim(parent_hp, parent_claim)
                        and hp_vtx_frequency.get(parent_info.get("vtx"), 0) >= 4
                    ):
                        # Repeated resolved hardware is a leaf detail, never the
                        # surface that should absorb another HP as its floater.
                        continue
                    parent_claim_score = _claim_score(parent_hp)
                    # Avoid cascading from weak floater links. A dependent mesh should
                    # attach to a stable bake owner, not to another uncertain detail.
                    if parent_hp in unstable_floater_parents or _claim_source(parent_hp) == "floater" or parent_claim_score < 35.0:
                        continue

                    for potential_floater in sorted(weak_floater_candidates, key=str):
                        if potential_floater == parent_hp or potential_floater in floaters_assigned:
                            continue

                        floater_verts = self.hp_verts_cache.get(potential_floater, [])
                        floater_info = self.hp_data.get(potential_floater, {})
                        if not floater_verts: continue
                        if floater_info.get("is_zbrush", False):
                            continue
                        if _is_manual_hp(potential_floater):
                            continue
                        current_claim = hp_claims.get(potential_floater, {})
                        current_owner_lp = current_claim.get("owner_lp")
                        try:
                            current_claim_score = float(current_claim.get("score", 0.0) or 0.0)
                        except Exception:
                            current_claim_score = 0.0
                        repeated_fastener_with_lp = (
                            current_owner_lp
                            and current_claim_score >= 45.0
                            and _is_repeated_fastener_like(
                                potential_floater, floater_info, floater_verts
                            )
                        )
                        if current_owner_lp and (
                            _has_own_lp_claim(potential_floater, current_claim)
                            or _has_scale_matched_lp_claim(potential_floater, current_claim)
                            or repeated_fastener_with_lp
                        ):
                            # A confident direct LP match has priority over indirect
                            # floater evidence such as an outward-facing open border.
                            # Repeated bolts/rivets commonly overlap several nearby
                            # LP bboxes, so their strong match is accepted even when
                            # the initial candidate list was not unique.
                            continue
                        if current_owner_lp == parent_lp_name:
                            continue
                        candidate_reason = floater_candidate_reason.get(potential_floater, "unknown")
                        floater_hole_orientation = _hole_orientation(potential_floater)
                        floater_parent_tests += 1

                        if not _bbox_overlaps_expanded(parent_info, floater_info, max(self.floater_radius, 0.05)):
                            floater_bbox_rejects += 1
                            continue

                        parent_diag = max(parent_info.get("diag", parent_info.get("radius", 1.0) * 2.0), 0.001)
                        floater_diag = max(floater_info.get("diag", floater_info.get("radius", 1.0) * 2.0), 0.001)
                        if parent_diag <= floater_diag * 1.20:
                            floater_size_rejects += 1
                            continue

                        floater_hole_boundary_verts = _hole_points(potential_floater)
                        has_hole_context = bool(hole_boundary_verts or floater_hole_boundary_verts)
                        if not has_hole_context:
                            parent_vol = max(parent_info.get("bbox_vol", 0.0), 0.000001)
                            floater_vol = max(floater_info.get("bbox_vol", 0.0), 0.0)
                            if floater_diag > parent_diag * 0.45 and floater_vol > parent_vol * 0.20:
                                floater_size_rejects += 1
                                continue

                        if floater_distance_tests >= distance_test_limit:
                            floater_scan_limited = True
                            break

                        dist = float('inf')
                        dist_mode = "none"
                        surface_decal_match = False
                        surface_decal_coverage = 0.0
                        surface_decal_avg_dist = float('inf')
                        surface_decal_bbox_fraction = 0.0

                        if floater_hole_boundary_verts and parent_verts:
                            try:
                                dist = bg_math_core.calculate_min_distance(
                                    _sample_flat_verts(floater_hole_boundary_verts, 500),
                                    _sample_flat_verts(parent_verts, 700)
                                )
                                dist_mode = "candidate_hole"
                                floater_distance_tests += 1
                            except AttributeError:
                                pass

                        if hole_boundary_verts:
                            try:
                                parent_hole_dist = bg_math_core.calculate_min_distance(
                                    _sample_flat_verts(hole_boundary_verts, 500),
                                    _sample_flat_verts(floater_verts, 700)
                                )
                                if parent_hole_dist < dist:
                                    dist = parent_hole_dist
                                    dist_mode = "parent_hole"
                                floater_distance_tests += 1
                            except AttributeError:
                                pass

                        if dist > self.floater_radius:
                            if parent_verts and hasattr(bg_math_core, 'calculate_min_distance'):
                                fallback_dist = bg_math_core.calculate_min_distance(
                                    _sample_flat_verts(parent_verts, 700),
                                    _sample_flat_verts(floater_verts, 700)
                                )
                                if fallback_dist < dist:
                                    dist = fallback_dist
                                    dist_mode = "surface"
                                floater_distance_tests += 1
                            else:
                                dist = 0.0
                                dist_mode = "fallback"

                        if floater_hole_boundary_verts and parent_verts:
                            surface_threshold = max(
                                min(self.floater_radius * 1.25, parent_diag * 0.035),
                                min(self.floater_radius * 2.0, floater_diag * 0.12),
                                0.005
                            )
                            surface_decal_coverage, surface_decal_avg_dist = _surface_near_coverage(
                                floater_hole_boundary_verts,
                                parent_verts,
                                surface_threshold,
                                max_source=220,
                                max_target=1000
                            )
                            if self.is_cancelled or self.isInterruptionRequested():
                                return True
                            surface_decal_bbox_fraction = _bbox_intersection_fraction(
                                floater_info,
                                parent_info,
                                max(surface_threshold, self.floater_radius * 0.5)
                            )
                            floater_surface_tests += 1
                            required_coverage = 0.58 if current_owner_lp else 0.48
                            if (
                                surface_decal_coverage >= required_coverage
                                and surface_decal_bbox_fraction >= 0.35
                                and floater_diag <= parent_diag * 0.55
                            ):
                                surface_decal_match = True
                                floater_surface_matches += 1
                                if surface_decal_avg_dist < dist:
                                    dist = surface_decal_avg_dist
                                dist_mode = "surface_decal"

                            if (
                                not surface_decal_match
                                and surface_decal_bbox_fraction >= 0.82
                                and floater_diag <= parent_diag * 0.32
                                and not _is_repeated_fastener_like(potential_floater, floater_info, floater_verts)
                            ):
                                surface_decal_match = True
                                floater_surface_matches += 1
                                surface_decal_coverage = max(surface_decal_coverage, 0.50)
                                if surface_decal_avg_dist < dist:
                                    dist = surface_decal_avg_dist
                                dist_mode = "surface_bbox_decal"

                        if dist <= self.floater_radius or surface_decal_match:
                            current_score = _claim_score(potential_floater)
                            candidate_lps = list(hp_candidates.get(potential_floater, []))
                            lp_context = parent_lp_name in candidate_lps
                            distance_limit = max(self.floater_radius, surface_decal_avg_dist if surface_decal_match else 0.0, 0.000001)
                            distance_score = max(0.0, 100.0 * (1.0 - (dist / distance_limit)))
                            score = distance_score + min(parent_claim_score, 100.0) * 0.35
                            if lp_context:
                                score += 35.0
                            elif current_owner_lp:
                                score -= 8.0 if surface_decal_match else 25.0
                            if dist_mode in ("candidate_hole", "parent_hole"):
                                score += 15.0
                            if floater_hole_orientation == "outward" and dist_mode == "candidate_hole":
                                score += 25.0
                            if candidate_reason == "inward_hole_no_lp":
                                score += 12.0
                            if surface_decal_match:
                                score += 45.0
                                score += surface_decal_coverage * 65.0
                                score += surface_decal_bbox_fraction * 20.0
                            if parent_info.get("is_zbrush", False) or "zbrush" in str(parent_hp).lower():
                                score += 10.0

                            _add_floater_candidate(potential_floater, {
                                "parent_hp": parent_hp,
                                "parent_lp": parent_lp_name,
                                "dist": dist,
                                "dist_mode": dist_mode,
                                "score": score,
                                "current_owner_lp": current_owner_lp,
                                "current_score": current_score,
                                "lp_context": lp_context,
                                "surface_decal": surface_decal_match,
                                "surface_coverage": surface_decal_coverage,
                                "surface_bbox_fraction": surface_decal_bbox_fraction,
                                "hole_orientation": floater_hole_orientation,
                                "candidate_reason": candidate_reason
                            })

                for potential_floater, candidate_records in sorted(floater_link_candidates.items(), key=lambda kv: _short_name(kv[0])):
                    if potential_floater in floaters_assigned:
                        continue
                    if not candidate_records:
                        continue
                    candidate_records.sort(key=lambda rec: (
                        -float(rec.get("score", 0.0) or 0.0),
                        _short_name(rec.get("parent_hp")),
                        str(rec.get("parent_hp"))
                    ))
                    best = candidate_records[0]
                    second_score = candidate_records[1].get("score", 0.0) if len(candidate_records) > 1 else -999.0
                    current_owner_lp = best.get("current_owner_lp")
                    current_score = float(best.get("current_score", 0.0) or 0.0)
                    best_score = float(best.get("score", 0.0) or 0.0)
                    margin = best_score - second_score

                    if best_score < 60.0:
                        floater_context_rejects += 1
                        _debug("  FLOATER_REJECT_LOW_SCORE: HP='{}' | best_parent='{}' | score={:.2f} | mode={} | dist={:.6f}".format(
                            _short_name(potential_floater),
                            _short_name(best.get("parent_hp")),
                            best_score,
                            best.get("dist_mode"),
                            best.get("dist", 0.0)
                        ))
                        continue

                    if len(candidate_records) > 1 and margin < 12.0:
                        floater_ambiguous_rejects += 1
                        _debug("  FLOATER_REJECT_AMBIGUOUS: HP='{}' | best='{}' {:.2f} | second='{}' {:.2f}".format(
                            _short_name(potential_floater),
                            _short_name(best.get("parent_hp")),
                            best_score,
                            _short_name(candidate_records[1].get("parent_hp")),
                            second_score
                        ))
                        continue

                    strong_surface_decal = (
                        best.get("surface_decal")
                        and float(best.get("surface_coverage", 0.0) or 0.0) >= 0.62
                        and best_score >= current_score + 8.0
                    )
                    forced_hole_relink = best.get("hole_orientation") == "outward"
                    if (
                        current_owner_lp
                        and not best.get("lp_context")
                        and current_score >= 45.0
                        and best_score < current_score + 30.0
                        and not strong_surface_decal
                        and not forced_hole_relink
                    ):
                        floater_context_rejects += 1
                        _debug("  FLOATER_REJECT_OWNER_CONTEXT: HP='{}' | current_lp='{}' score={:.2f} | best_parent='{}' best_score={:.2f}".format(
                            _short_name(potential_floater),
                            _short_name(current_owner_lp),
                            current_score,
                            _short_name(best.get("parent_hp")),
                            best_score
                        ))
                        continue

                    parent_lp_name = best.get("parent_lp")
                    parent_hp = best.get("parent_hp")
                    owned_hps = lp_to_owned_hps.get(parent_lp_name)
                    if owned_hps is None:
                        floater_context_rejects += 1
                        continue

                    if current_owner_lp and current_owner_lp in lp_to_owned_hps:
                        lp_to_owned_hps[current_owner_lp] = [
                            _hp for _hp in lp_to_owned_hps[current_owner_lp]
                            if _hp.get("name") != potential_floater
                        ]
                        floater_reassigned_count += 1
                    if not any(_hp.get("name") == potential_floater for _hp in owned_hps):
                        owned_hps.append(self.hp_data[potential_floater])
                    hp_claims[potential_floater] = {
                        "owner_lp": parent_lp_name,
                        "candidate_lps": list(hp_candidates.get(potential_floater, [])),
                        "candidate_count": len(hp_candidates.get(potential_floater, [])),
                        "driver_hp": parent_hp,
                        "source": "floater",
                        "reason": "floater/decal owner='{}' mode={} hole={} candidate={} distance={:.6f} score={:.2f} margin={:.2f}".format(
                            _short_name(parent_hp),
                            best.get("dist_mode"),
                            best.get("hole_orientation", "unknown"),
                            best.get("candidate_reason", "unknown"),
                            best.get("dist", 0.0),
                            best_score,
                            margin
                        ),
                        "score": min(85.0, max(60.0, best_score))
                    }

                    floater_info = self.hp_data.get(potential_floater, {})
                    parent_info = self.hp_data.get(parent_hp, {})
                    floater_uuid = floater_info.get('uuid')
                    parent_uuid = parent_info.get('uuid')

                    if floater_uuid and parent_uuid:
                        group_key = "AutoFloater_{}".format(parent_uuid)
                        if group_key not in self.custom_clusters_dict:
                            self.custom_clusters_dict[group_key] = [parent_uuid]
                        if floater_uuid not in self.custom_clusters_dict[group_key]:
                            self.custom_clusters_dict[group_key].append(floater_uuid)
                        if group_key not in effective_custom_clusters:
                            effective_custom_clusters[group_key] = [parent_uuid]
                        if floater_uuid not in effective_custom_clusters[group_key]:
                            effective_custom_clusters[group_key].append(floater_uuid)

                    if potential_floater in unassigned_hps:
                        unassigned_hps.remove(potential_floater)
                    floaters_assigned.add(potential_floater)
                    floater_assigned_count += 1
                    _debug("  FLOATER_ASSIGN: HP='{}' -> parent='{}' LP='{}' | score={:.2f} | margin={:.2f} | mode={} | hole={} | candidate={} | dist={:.6f} | coverage={:.1f}% | bbox={:.1f}%".format(
                        _short_name(potential_floater),
                        _short_name(parent_hp),
                        _short_name(parent_lp_name),
                        best_score,
                        margin,
                        best.get("dist_mode"),
                        best.get("hole_orientation", "unknown"),
                        best.get("candidate_reason", "unknown"),
                        best.get("dist", 0.0),
                        float(best.get("surface_coverage", 0.0) or 0.0) * 100.0,
                        float(best.get("surface_bbox_fraction", 0.0) or 0.0) * 100.0
                    ))

                _debug("Step 2.5: floater/decal pass | parents={} | weak_candidates={} | parent_candidate_tests={} | bbox_rejects={} | size_rejects={} | distance_tests={} | surface_tests={} | surface_matches={} | assigned={} | reassigned={} | low_context_rejects={} | ambiguous_rejects={} | limited={}.".format(
                    len(all_hp_names),
                    len(weak_floater_candidates),
                    floater_parent_tests,
                    floater_bbox_rejects,
                    floater_size_rejects,
                    floater_distance_tests,
                    floater_surface_tests,
                    floater_surface_matches,
                    floater_assigned_count,
                    floater_reassigned_count,
                    floater_context_rejects,
                    floater_ambiguous_rejects,
                    "yes" if floater_scan_limited else "no"
                ))
                logs.append("Floater/decal pass: tested {} nearby candidate pair(s), surface matches {}, assigned {} HP mesh(es), reassigned {} weak match(es), skipped {} ambiguous/low-confidence case(s).".format(
                    floater_distance_tests,
                    floater_surface_matches,
                    floater_assigned_count,
                    floater_reassigned_count,
                    floater_context_rejects + floater_ambiguous_rejects
                ))
                if floater_scan_limited:
                    logs.append("[Warning] Floater/decal pass reached the safety limit and was stopped early to keep Analyze HP responsive.")
            else:
                _debug("Step 2.5: floater/decal pass skipped | detect_floaters={} | math_core={}.".format(
                    self.detect_floaters,
                    HAS_MATH_CORE
                ))
        if _step2_5_floater_decal_pass():
            return


        self.progress_value.emit(40)
        self.progress_text.emit("Step 3: Clustering Similar LP meshes...")

        # --- STEP 3: Clustering Similar LP Meshes ---
        # LP families are hints, not final bake groups. Use union-find so exact
        # hash/UV matches remain transitive and do not depend on list order.
        lp_parent = {}

        def _lp_find(lp_name):
            lp_parent.setdefault(lp_name, lp_name)
            if lp_parent[lp_name] != lp_name:
                lp_parent[lp_name] = _lp_find(lp_parent[lp_name])
            return lp_parent[lp_name]

        def _lp_union(a, b):
            if not _lp_material_compatible(a, b):
                return False
            root_a = _lp_find(a)
            root_b = _lp_find(b)
            if root_a == root_b:
                return False
            lp_parent[root_b] = root_a
            return True

        for lp_name in sorted_lp_names:
            lp_parent[lp_name] = lp_name

        hash_buckets = {}
        uv_buckets = {}
        for lp_name in sorted_lp_names:
            info = self.lp_data[lp_name]
            h = info.get("hash", "empty")
            uv = info.get("uv_signature", "empty")
            if h and h != "empty":
                hash_buckets.setdefault(h, []).append(lp_name)
            if uv and uv != "empty":
                uv_buckets.setdefault(uv, []).append(lp_name)

        lp_hash_links = 0
        lp_uv_links = 0
        lp_topology_links = 0
        lp_symmetry_links = 0

        for bucket in hash_buckets.values():
            if len(bucket) < 2:
                continue
            anchor = bucket[0]
            for lp_name in bucket[1:]:
                if _lp_union(anchor, lp_name):
                    lp_hash_links += 1

        for bucket in uv_buckets.values():
            if len(bucket) < 2:
                continue
            anchor = bucket[0]
            for lp_name in bucket[1:]:
                if _lp_union(anchor, lp_name):
                    lp_uv_links += 1

        for i in range(len(sorted_lp_names)):
            lp1 = sorted_lp_names[i]
            m1 = self.lp_data[lp1]
            h1 = m1.get("hash", "empty")
            uv1 = m1.get("uv_signature", "empty")
            for j in range(i + 1, len(sorted_lp_names)):
                lp2 = sorted_lp_names[j]
                if _lp_find(lp1) == _lp_find(lp2):
                    continue
                m2 = self.lp_data[lp2]
                h2 = m2.get("hash", "empty")
                uv2 = m2.get("uv_signature", "empty")
                topology_match = (
                    m1.get("vtx", -1) == m2.get("vtx", -2)
                    and m1.get("edges", -1) == m2.get("edges", -2)
                )
                has_strong_signature = (
                    bool(h1 and h1 != "empty")
                    or bool(h2 and h2 != "empty")
                    or bool(uv1 and uv1 != "empty")
                    or bool(uv2 and uv2 != "empty")
                )

                if topology_match and not has_strong_signature:
                    if not bg_core.MathUtils.is_overlapping(m1, m2):
                        if _lp_union(lp1, lp2):
                            lp_topology_links += 1
                    continue

                if self.use_symmetry and HAS_MATH_CORE and hasattr(bg_math_core, 'are_symmetric'):
                    if m1.get("vtx", -1) == m2.get("vtx", -2):
                        v1 = self.lp_verts_cache.get(lp1, [])
                        v2 = self.lp_verts_cache.get(lp2, [])
                        if v1 and v2 and bg_math_core.are_symmetric(v1, v2, tolerance=0.01):
                            if _lp_union(lp1, lp2):
                                lp_symmetry_links += 1

        lp_clusters_by_root = {}
        lp_order = dict((lp_name, idx) for idx, lp_name in enumerate(sorted_lp_names))
        for lp_name in sorted_lp_names:
            lp_clusters_by_root.setdefault(_lp_find(lp_name), []).append(lp_name)

        lp_clusters = sorted(
            lp_clusters_by_root.values(),
            key=lambda cluster: min(lp_order.get(lp_name, 999999) for lp_name in cluster)
        )

        lp_cluster_id_by_lp = {}
        lp_cluster_size_by_id = {}
        for _idx, _cluster in enumerate(lp_clusters, 1):
            lp_cluster_size_by_id[_idx] = len(_cluster)
            for _lp_name in _cluster:
                lp_cluster_id_by_lp[_lp_name] = _idx

        item_meta_by_key = {}

        def _item_key(item):
            return tuple(sorted([
                str(m.get('uuid') or m.get('name') or id(m))
                for m in (item or [])
            ]))

        def _set_item_meta(item, **meta):
            item_meta_by_key[_item_key(item)] = dict(meta)

        def _get_item_meta(item):
            return item_meta_by_key.get(_item_key(item), {})

        def _item_material_slots(item):
            slots = set()
            meta = _get_item_meta(item)
            if meta.get('material_slot'):
                slots.add(str(meta.get('material_slot')))
            for hp_info in item or []:
                slot = _claim_material_slot(hp_info.get('name'))
                if slot:
                    slots.add(str(slot))
            return slots

        def _item_primary_material_slot(item):
            slots = sorted(_item_material_slots(item))
            return slots[0] if len(slots) == 1 else None

        def _item_group_prefix(prefix, item):
            slot = _item_primary_material_slot(item)
            if slot and not str(prefix).startswith(slot + "_"):
                return "{}_{}".format(slot, prefix)
            return prefix

        named_clusters = []
        unlinked_meshes = [self.hp_data[hp] for hp in sorted(unassigned_hps, key=str)]

        lp_base_to_lps = {}
        lp_base_order = {}
        for _order_idx, lp_name in enumerate(sorted_lp_names):
            _base = _lp_base_name(lp_name)
            lp_base_to_lps.setdefault(_base, []).append(lp_name)
            lp_base_order.setdefault(_base, _order_idx)

        lp_base_pack_count = 0
        lp_shell_pack_merged = 0
        for lp_base in sorted(lp_base_to_lps.keys(), key=lambda base: lp_base_order.get(base, 999999)):
            base_lps = lp_base_to_lps.get(lp_base, [])
            owned_hps = []
            seen_hp_names = set()
            for lp_name in base_lps:
                for hp_info in lp_to_owned_hps.get(lp_name, []):
                    hp_name = hp_info.get('name')
                    if hp_name and hp_name in seen_hp_names:
                        continue
                    if hp_name:
                        seen_hp_names.add(hp_name)
                    owned_hps.append(hp_info)
            if not owned_hps:
                continue

            # LP shells belong to one final LP object. Keep their HP together so
            # bolts/details on a combined LP mesh do not fall into generic buckets.
            named_clusters.append(owned_hps)
            lp_base_pack_count += 1
            if len(base_lps) > 1:
                lp_shell_pack_merged += len(base_lps) - 1
            _owner_lp = base_lps[0] if base_lps else lp_base
            _lp_cluster_id = lp_cluster_id_by_lp.get(_owner_lp)
            _set_item_meta(
                owned_hps,
                source='lp',
                owner_lp=_owner_lp,
                owner_lp_short=_short_name(_owner_lp),
                owner_lp_base=lp_base,
                owner_lp_base_short=_short_name(lp_base),
                material_slot=_lp_material_slot(_owner_lp),
                material_name=self.lp_data.get(_owner_lp, {}).get("material_name"),
                lp_cluster_id=_lp_cluster_id,
                lp_cluster_size=len(base_lps)
            )
            linked_mesh_names.update([m['name'] for m in owned_hps if m.get('name')])

        _debug("Step 3: LP similarity clustering.")
        _debug("  LP clusters={} | LP-guided HP cluster items={} | HP items left for later packing={}.".format(
            len(lp_clusters), len(named_clusters), len(unlinked_meshes)
        ))
        _debug("  LP base grouping: pack_items={} | merged_shell_refs={}.".format(
            lp_base_pack_count, lp_shell_pack_merged
        ))
        _debug("  LP family links: hash={}, uv={}, topology={}, symmetry={}.".format(
            lp_hash_links, lp_uv_links, lp_topology_links, lp_symmetry_links
        ))
        for idx, lp_cluster in enumerate(lp_clusters, 1):
            cluster_hp_count = sum(len(lp_to_owned_hps.get(lp_name, [])) for lp_name in lp_cluster)
            if cluster_hp_count:
                lp_names = [_short_name(lp) for lp in lp_cluster]
                if len(lp_names) > 20:
                    lp_names = lp_names[:20] + ["... +{} more".format(len(lp_names) - 20)]
                _debug("  LP_CLUSTER_{}: lp_count={} | hp_count={} | pack_items={} | LP={}".format(
                    idx,
                    len(lp_cluster),
                    cluster_hp_count,
                    sum(1 for lp_name in lp_cluster if lp_to_owned_hps.get(lp_name)),
                    ", ".join(lp_names)
                ))
        _debug("")

        self.progress_value.emit(45)
        self.progress_text.emit("Step 4: Calculating Smart Size Boundaries...")

        # --- STEP 4: Threshold Calculations & OUTLIER VALIDATION ---
        all_meshes_list = list(self.hp_data.values())

        def _percentile(values, fraction):
            ordered = sorted(float(value) for value in values if _is_finite_value(value))
            if not ordered:
                return 0.0
            if len(ordered) == 1:
                return ordered[0]
            position = max(0.0, min(1.0, float(fraction))) * (len(ordered) - 1)
            lower = int(math.floor(position))
            upper = int(math.ceil(position))
            if lower == upper:
                return ordered[lower]
            weight = position - lower
            return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

        raw_diags = [m.get("diag", 0) for m in all_meshes_list if m.get("diag", 0) > 0.001 and m.get("bbox_vol", 0) > 1e-6]
        median_scene_diag = bg_core.StatsUtils.median(raw_diags) if raw_diags else 1.0

        # Keep the proven historical scale on ordinary scenes, while allowing
        # the upper population percentile to lift the cutoff when many repeated
        # tiny details pull the median down.
        raw_q95 = _percentile(raw_diags, 0.95) if raw_diags else median_scene_diag
        outlier_diag_limit = max(median_scene_diag * 10.0, raw_q95 * 1.10, 0.001)
        valid_diags = []
        for m in all_meshes_list:
            d = m.get("diag", 0)
            if 0.001 < d <= outlier_diag_limit and m.get("bbox_vol", 0) > 1e-6:
                valid_diags.append(d)
            elif d > outlier_diag_limit:
                logs.append("[Warning] Mesh '{}' has anomalous bounding box (Diag: {:.2f}). Excluded from threshold calc.".format(m.get('name'), d))

        valid_diags.sort()
        # The 95th percentile remains stable as repeated chapters/details are
        # added and closely matches the old upper-shelf calibration without
        # depending on exactly ten mesh records.
        upper_shelf_diag = min(raw_q95, valid_diags[-1]) if valid_diags else 1.0

        bolt_median_diag = 0.0
        vtx_dict = {}
        for m in all_meshes_list:
            d = m.get("diag", 0)
            if not m.get("is_zbrush", False) and 1e-6 < m.get("bbox_vol", 0) and d <= outlier_diag_limit:
                vtx_dict.setdefault(m.get("vtx", 0), []).append(d)

        repeated_scale_candidates = []
        for vtx, diags in sorted(vtx_dict.items(), key=lambda item: item[0]):
            if len(diags) < 2:
                continue
            cluster_mean = sum(diags) / float(len(diags))
            if cluster_mean <= upper_shelf_diag * 0.40:
                repeated_scale_candidates.append((
                    -len(diags),
                    vtx,
                    cluster_mean,
                ))

        if repeated_scale_candidates:
            repeated_scale_candidates.sort()
            bolt_median_diag = repeated_scale_candidates[0][2]

        if bolt_median_diag == 0.0 or bolt_median_diag > upper_shelf_diag * 0.25:
            bolt_median_diag = _percentile(valid_diags, 0.10) if valid_diags else 0.1

        small_threshold = bolt_median_diag * 1.5
        large_threshold = max(upper_shelf_diag * 0.6, small_threshold * 1.5)
        medium_threshold = (small_threshold + large_threshold) / 2.0
        bolt_vtx_values = set()
        for vtx, diags in vtx_dict.items():
            if len(diags) < 2:
                continue
            avg_diag = sum(diags) / float(len(diags))
            if avg_diag <= (medium_threshold * 1.15):
                bolt_vtx_values.add(vtx)
        _debug("Step 4: size thresholds.")
        _debug("  median_scene_diag={:.6f} | valid_diag_count={} | upper_shelf_diag={:.6f}".format(
            median_scene_diag, len(valid_diags), upper_shelf_diag
        ))
        _debug("  bolt_median_diag={:.6f} | small={:.6f} | medium={:.6f} | large={:.6f}".format(
            bolt_median_diag, small_threshold, medium_threshold, large_threshold
        ))
        _debug("  repeated bolt-like topology signatures={}".format(len(bolt_vtx_values)))
        _debug("")

        self.progress_value.emit(50)
        self.progress_text.emit("Step 5: Computing mesh clusters for singletons...")

        # --- STEP 5: Singletons Handling ---
        shape_metrics_cache = {}

        def get_shape_metrics(mesh_info):
            name = mesh_info.get("name")
            if name in shape_metrics_cache:
                return shape_metrics_cache[name]
            result = None
            if HAS_MATH_CORE:
                verts = self.hp_verts_cache.get(name, [])
                if verts:
                    try:
                        metrics = _analyze_shape_metrics(verts)
                        result = (float(metrics.elongation), float(metrics.symmetry_score))
                    except Exception:
                        result = None
            shape_metrics_cache[name] = result
            return result

        def empty_non_zb_can_cluster(m1, m2):
            if self.strategy == 2:
                return m1.get("vtx", -1) == m2.get("vtx", -2) and m1.get("edges", -1) == m2.get("edges", -2)

            v1 = m1.get("bbox_vol", 0)
            v2 = m2.get("bbox_vol", 0)
            vol_max = max(v1, v2, 0.0001)
            volume_match = abs(v1 - v2) / vol_max <= 0.05

            if self.strategy == 1:
                metrics1 = get_shape_metrics(m1)
                metrics2 = get_shape_metrics(m2)
                if metrics1 and metrics2:
                    elong_max = max(abs(metrics1[0]), abs(metrics2[0]), 0.0001)
                    elong_match = abs(metrics1[0] - metrics2[0]) / elong_max <= 0.20
                    sym_match = (not self.use_symmetry) or abs(metrics1[1] - metrics2[1]) <= 0.20
                    return volume_match and elong_match and sym_match

            return volume_match

        def compute_clusters_fast(mesh_list):
            clusters = []
            processed = set()

            hash_buckets = {}
            for i, m in enumerate(mesh_list):
                h = m.get("hash", "empty")
                is_zb = m.get("is_zbrush", False)
                if is_zb:
                    hash_buckets.setdefault("__zbrush__", []).append(i)
                elif h == "empty":
                    hash_buckets.setdefault("__empty_non_zb__", []).append(i)
                else:
                    hash_buckets.setdefault(h, []).append(i)

            # РћР±СЂР°Р±РѕС‚РєР° РѕР±С‹С‡РЅС‹С… С…СЌС€РµР№
            for h_key, indices in hash_buckets.items():
                if h_key in ("__zbrush__", "__empty_non_zb__"):
                    continue

                local_processed = set()
                for i in indices:
                    if i in local_processed:
                        continue
                    current_cluster = [mesh_list[i]]
                    local_processed.add(i)
                    processed.add(i)

                    for j in indices:
                        if j in local_processed:
                            continue
                        has_collision = any(bg_core.MathUtils.is_overlapping(mesh_list[j], cm) for cm in current_cluster)
                        if not has_collision:
                            current_cluster.append(mesh_list[j])
                            local_processed.add(j)
                            processed.add(j)

                    if current_cluster:
                        clusters.append(current_cluster)

            zb_indices = hash_buckets.get("__zbrush__", [])
            for idx_i in zb_indices:
                if idx_i in processed:
                    continue
                m1 = mesh_list[idx_i]
                current_cluster = [m1]
                processed.add(idx_i)
                v1 = m1.get("bbox_vol", 0)

                for idx_j in zb_indices:
                    if idx_j in processed:
                        continue
                    m2 = mesh_list[idx_j]
                    v2 = m2.get("bbox_vol", 0)
                    vol_max = max(v1, v2, 0.0001)
                    if abs(v1 - v2) / vol_max <= 0.05:
                        if not any(bg_core.MathUtils.is_overlapping(m2, cm) for cm in current_cluster):
                            current_cluster.append(m2)
                            processed.add(idx_j)
                if current_cluster:
                    clusters.append(current_cluster)

            empty_non_zb_indices = hash_buckets.get("__empty_non_zb__", [])
            for idx_i in empty_non_zb_indices:
                if idx_i in processed:
                    continue
                m1 = mesh_list[idx_i]
                current_cluster = [m1]
                processed.add(idx_i)
                for idx_j in empty_non_zb_indices:
                    if idx_j in processed:
                        continue
                    m2 = mesh_list[idx_j]
                    if empty_non_zb_can_cluster(m1, m2):
                        if not any(bg_core.MathUtils.is_overlapping(m2, cm) for cm in current_cluster):
                            current_cluster.append(m2)
                            processed.add(idx_j)
                clusters.append(current_cluster)

            return clusters

        identical_clusters = compute_clusters_fast(unlinked_meshes)
        for _cluster in identical_clusters:
            _set_item_meta(_cluster, source='shape', owner_lp=None, owner_lp_base=None, lp_cluster_id=None, lp_cluster_size=0)

        all_items = []
        all_items.extend(named_clusters)
        all_items.extend(identical_clusters)

        # --- STEP 5.5: Inject GT Matcher/manual clusters into packing queue ---
        # Custom clusters should behave as cluster items, not as their own subgroups.
        # They replace auto cluster items containing the same HP meshes, then continue
        # through size categorization and spatial packing like every other cluster.
        gt_cluster_items = []
        gt_used_uuids = set()
        gt_protected_uuids = set()
        gt_unprotected_uuids = set()
        gt_hard_uuids = set()

        def _set_normal_split_meta(item):
            lp_counts = {}
            for _mesh_info in item:
                _hp_name = _mesh_info.get('name')
                _owner_lp = hp_claims.get(_hp_name, {}).get('owner_lp')
                if _owner_lp:
                    lp_counts[_owner_lp] = lp_counts.get(_owner_lp, 0) + 1
            if lp_counts:
                _dominant_lp = max(lp_counts.keys(), key=lambda lp: (lp_counts[lp], -len(str(lp))))
                _lp_cluster_id = lp_cluster_id_by_lp.get(_dominant_lp)
                _set_item_meta(
                    item,
                    source='lp',
                    owner_lp=_dominant_lp,
                    owner_lp_short=_short_name(_dominant_lp),
                    owner_lp_base=_lp_base_name(_dominant_lp),
                    owner_lp_base_short=_short_name(_lp_base_name(_dominant_lp)),
                    material_slot=_lp_material_slot(_dominant_lp),
                    material_name=self.lp_data.get(_dominant_lp, {}).get("material_name"),
                    lp_cluster_id=_lp_cluster_id,
                    lp_cluster_size=lp_cluster_size_by_id.get(_lp_cluster_id, 0)
                )
            else:
                _set_item_meta(item, source='shape', owner_lp=None, owner_lp_base=None, lp_cluster_id=None, lp_cluster_size=0)

        def _set_material_custom_meta(item, cluster_name):
            lp_counts = {}
            slot_counts = {}
            cluster_lp = _cluster_owner_lp(cluster_name)
            for _mesh_info in item:
                _hp_name = _mesh_info.get('name')
                _claim = hp_claims.get(_hp_name, {})
                _owner_lp = _claim.get('owner_lp')
                if _owner_lp:
                    lp_counts[_owner_lp] = lp_counts.get(_owner_lp, 0) + 1
                _slot = _claim_material_slot(_hp_name)
                if _slot:
                    slot_counts[_slot] = slot_counts.get(_slot, 0) + 1

            _dominant_lp = None
            if lp_counts:
                _dominant_lp = max(lp_counts.keys(), key=lambda lp: (lp_counts[lp], -len(str(lp))))
            elif cluster_lp:
                _dominant_lp = cluster_lp

            _slot = _lp_material_slot(_dominant_lp) if _dominant_lp else None
            if not _slot and slot_counts:
                _slot = max(slot_counts.keys(), key=lambda slot: (slot_counts[slot], str(slot)))

            if _dominant_lp:
                _lp_cluster_id = lp_cluster_id_by_lp.get(_dominant_lp)
                _set_item_meta(
                    item,
                    source='custom',
                    custom_cluster=cluster_name,
                    owner_lp=_dominant_lp,
                    owner_lp_short=_short_name(_dominant_lp),
                    owner_lp_base=_lp_base_name(_dominant_lp),
                    owner_lp_base_short=_short_name(_lp_base_name(_dominant_lp)),
                    material_slot=_slot,
                    material_name=self.lp_data.get(_dominant_lp, {}).get("material_name"),
                    lp_cluster_id=_lp_cluster_id,
                    lp_cluster_size=lp_cluster_size_by_id.get(_lp_cluster_id, 0)
                )
            else:
                _set_item_meta(
                    item,
                    source='custom',
                    custom_cluster=cluster_name,
                    owner_lp=None,
                    owner_lp_base=None,
                    material_slot=_slot,
                    lp_cluster_id=None,
                    lp_cluster_size=0
                )

        def _split_custom_item_by_material(item, cluster_name):
            material_parts = {}
            unknown_part = []
            cluster_lp = _cluster_owner_lp(cluster_name)
            cluster_slot = _lp_material_slot(cluster_lp) if cluster_lp else None
            for _mesh_info in item:
                _slot = _claim_material_slot(_mesh_info.get('name')) or cluster_slot
                if _slot:
                    material_parts.setdefault(_slot, []).append(_mesh_info)
                else:
                    unknown_part.append(_mesh_info)
            if not material_parts:
                return None
            result = []
            for _slot in sorted(material_parts.keys()):
                result.append((material_parts[_slot], "{}::{}".format(cluster_name, _slot), 'custom_material'))
            if unknown_part:
                result.append((unknown_part, cluster_name, 'custom_hard'))
            return result

        if effective_custom_clusters:
            uuid_to_hp_info = {}
            for _hp_name, _hp_info in self.hp_data.items():
                _uid = _hp_info.get('uuid')
                if _uid:
                    uuid_to_hp_info[_uid] = _hp_info

            # Manual clusters are already first in effective_custom_clusters; semantic
            # fallback clusters skip UUIDs already occupied by manual links.
            for _cluster_name, _uuids in effective_custom_clusters.items():
                _item = []
                _local_seen = set()
                _is_hard_cluster = bool(_cluster_name in self.manual_cluster_names)
                for _uid in (_uuids or []):
                    if not _uid or _uid in gt_used_uuids or _uid in _local_seen:
                        continue
                    _info = uuid_to_hp_info.get(_uid)
                    if _info:
                        _item.append(_info)
                        _local_seen.add(_uid)
                    if _uid in self.manual_uuid_set:
                        _is_hard_cluster = True

                if _item:
                    zbrush_part = [m for m in _item if m.get("is_zbrush", False)]
                    normal_part = [m for m in _item if not m.get("is_zbrush", False)]
                    split_parts = []
                    protected_local_uuids = set()
                    material_split_parts = _split_custom_item_by_material(_item, _cluster_name) if lp_material_slots else None
                    if _is_hard_cluster and material_split_parts:
                        split_parts = material_split_parts
                        protected_local_uuids.update(_local_seen)
                        for _split_item, _split_name, _split_source in split_parts:
                            if _split_source == 'custom_hard':
                                gt_hard_uuids.update([m.get('uuid') for m in _split_item if m.get('uuid')])
                    elif _is_hard_cluster:
                        split_parts = [(_item, _cluster_name, 'custom_hard')]
                        protected_local_uuids.update(_local_seen)
                        gt_hard_uuids.update([u for u in _local_seen if u])
                    elif zbrush_part and normal_part:
                        normal_subclusters = compute_clusters_fast(normal_part) if len(normal_part) > 1 else [normal_part]
                        for _sub_item in normal_subclusters:
                            split_parts.append((_sub_item, None, 'shape'))
                        split_parts.append((zbrush_part, _cluster_name + "::zbrush", 'custom'))
                        gt_unprotected_uuids.update([m.get('uuid') for m in normal_part if m.get('uuid')])
                        protected_local_uuids.update([m.get('uuid') for m in zbrush_part if m.get('uuid')])
                        _debug("  CUSTOM_SPLIT_MIXED_ZBRUSH: '{}' | normal={} | normal_subitems={} | zbrush={}".format(
                            _cluster_name,
                            len(normal_part),
                            len(normal_subclusters),
                            len(zbrush_part)
                        ))
                    else:
                        split_parts = [(_item, _cluster_name, 'custom')]
                        protected_local_uuids.update(_local_seen)

                    for _split_item, _split_name, _split_source in split_parts:
                        if not _split_item:
                            continue
                        gt_cluster_items.append(_split_item)
                        if _split_source == 'shape' and _split_name is None:
                            _set_normal_split_meta(_split_item)
                        elif _split_source == 'custom_material':
                            _set_material_custom_meta(_split_item, _split_name)
                        elif _split_source == 'custom_hard':
                            _set_item_meta(_split_item, source='custom', custom_cluster=_split_name, hard_custom=True, owner_lp_base=None, lp_cluster_id=None, lp_cluster_size=0)
                        else:
                            _set_item_meta(_split_item, source=_split_source, custom_cluster=_split_name, owner_lp_base=None, lp_cluster_id=None, lp_cluster_size=0)
                    gt_used_uuids.update(_local_seen)
                    gt_protected_uuids.update([u for u in protected_local_uuids if u])

        if gt_cluster_items:
                rebuilt_items = []
                for _item in all_items:
                    _remaining = [m for m in _item if m.get('uuid') not in gt_used_uuids]
                    if _remaining:
                        rebuilt_items.append(_remaining)
                        _old_meta = dict(_get_item_meta(_item))
                        if _old_meta:
                            _set_item_meta(_remaining, **_old_meta)

                all_items = rebuilt_items + gt_cluster_items
                linked_mesh_names.update([m.get('name') for item in gt_cluster_items for m in item if m.get('name')])
                logs.append("Injected {} GT/custom cluster item(s) into normal packing queue.".format(len(gt_cluster_items)))
                _debug("Step 5.5: injected {} GT/custom cluster item(s); replaced UUID count={} | protected UUID count={}.".format(
                    len(gt_cluster_items), len(gt_used_uuids), len(gt_protected_uuids)
                ))

        # Hoisted out of _step6_7_categorize_and_pack(): also called from the
        # Step 7.5 ownership-repair / final-merge code below, which lives
        # outside that nested function.
        def item_bbox(item):
            if not item:
                return [0, 0, 0, 0, 0, 0]
            return [
                min(m.get("bbox", [0, 0, 0, 0, 0, 0])[0] for m in item),
                min(m.get("bbox", [0, 0, 0, 0, 0, 0])[1] for m in item),
                min(m.get("bbox", [0, 0, 0, 0, 0, 0])[2] for m in item),
                max(m.get("bbox", [0, 0, 0, 0, 0, 0])[3] for m in item),
                max(m.get("bbox", [0, 0, 0, 0, 0, 0])[4] for m in item),
                max(m.get("bbox", [0, 0, 0, 0, 0, 0])[5] for m in item),
            ]

        def bbox_center(bbox):
            return [
                (bbox[0] + bbox[3]) * 0.5,
                (bbox[1] + bbox[4]) * 0.5,
                (bbox[2] + bbox[5]) * 0.5,
            ]

        def bbox_diag(bbox):
            return math.sqrt(
                max(bbox[3] - bbox[0], 0.0) ** 2 +
                max(bbox[4] - bbox[1], 0.0) ** 2 +
                max(bbox[5] - bbox[2], 0.0) ** 2
            )

        def point_distance(a, b):
            return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)

        def check_collision(m1, m2):
            bb1 = m1.get("bbox", [0, 0, 0, 0, 0, 0])
            bb2 = m2.get("bbox", [0, 0, 0, 0, 0, 0])

            if (bb1[3] < bb2[0] or bb1[0] > bb2[3] or
                bb1[4] < bb2[1] or bb1[1] > bb2[4] or
                bb1[5] < bb2[2] or bb1[2] > bb2[5]):
                return False

            if HAS_MATH_CORE:
                v1 = self.hp_verts_cache.get(m1.get("name"), [])
                v2 = self.hp_verts_cache.get(m2.get("name"), [])
                if v1 and v2:
                    return bg_math_core.check_mesh_collision(v1, v2, self.collision_tolerance)

            return bg_core.MathUtils.is_overlapping(m1, m2)

        def _step6_7_categorize_and_pack():
            self.progress_value.emit(55)
            self.progress_text.emit("Step 6: Categorizing cluster metrics...")

            # --- STEP 6: CATEGORIZATION ---
            def get_cluster_metrics(item):
                total_vol = sum(m.get("bbox_vol", 0) for m in item)
                min_x = min([m.get("bbox", [0, 0, 0, 0, 0, 0])[0] for m in item] or [0])
                min_y = min([m.get("bbox", [0, 0, 0, 0, 0, 0])[1] for m in item] or [0])
                min_z = min([m.get("bbox", [0, 0, 0, 0, 0, 0])[2] for m in item] or [0])
                max_x = max([m.get("bbox", [0, 0, 0, 0, 0, 0])[3] for m in item] or [0])
                max_y = max([m.get("bbox", [0, 0, 0, 0, 0, 0])[4] for m in item] or [0])
                max_z = max([m.get("bbox", [0, 0, 0, 0, 0, 0])[5] for m in item] or [0])

                overall_bbox_vol = max((max_x - min_x) * (max_y - min_y) * (max_z - min_z), 0.0001)
                fill_ratio = total_vol / overall_bbox_vol
                is_scattered = len(item) > 1 and fill_ratio < 0.05

                if is_scattered:
                    max_single_vol = max([m.get("bbox_vol", 0) for m in item] or [0])
                    equiv_vol = max_single_vol * 2.0
                else:
                    equiv_vol = total_vol

                cube_side = equiv_vol ** (1.0 / 3.0) if equiv_vol > 0 else 0.001
                cluster_diag = math.sqrt(3) * cube_side
                return total_vol, cluster_diag

            def is_zbrush_cluster(cluster):
                return any(m.get("is_zbrush", False) for m in cluster)

            huge_items, large_items, medium_items, small_items, bolt_items = [], [], [], [], []
            huge_zb, large_zb, medium_zb, small_zb, bolt_zb = [], [], [], [], []
            bolt_mixed_reclass_count = 0

            def _item_is_hard_custom(item):
                meta = _get_item_meta(item)
                if meta.get('hard_custom'):
                    return True
                return any((m.get('uuid') in gt_hard_uuids) for m in (item or []))

            def _mesh_is_bolt_like(mesh_info):
                if mesh_info.get("is_zbrush", False):
                    return False
                if mesh_info.get("uuid") in gt_hard_uuids:
                    return False
                diag = mesh_info.get("diag", mesh_info.get("radius", 0) * 2)
                if diag <= small_threshold:
                    return True
                if mesh_info.get("vtx") in bolt_vtx_values and diag <= (medium_threshold * 1.15):
                    return True
                metrics = get_shape_metrics(mesh_info)
                if metrics:
                    elongation, symmetry_score = metrics
                    symmetry_ok = (not self.use_symmetry) or symmetry_score < self.bolt_symmetry
                    if elongation < self.bolt_elongation and symmetry_ok and diag <= medium_threshold:
                        return True
                return False

            for item_idx, item in enumerate(all_items):
                if self.is_cancelled: return True
                if item_idx % max(1, len(all_items) // 25) == 0:
                    self.progress_value.emit(55 + int((item_idx / float(max(len(all_items), 1))) * 15))

                true_vol, cluster_diag = get_cluster_metrics(item)
                is_hard_custom = _item_is_hard_custom(item)
                any_zb_in_item = is_zbrush_cluster(item)
                all_zb_in_item = bool(item) and all(m.get("is_zbrush", False) for m in item)
                # HP-LP Matcher clusters are hard, but a mixed normal+ZBrush cluster
                # should stay in the normal packing family. ZBrush handling is still
                # per-mesh later, while the subgroup name no longer becomes misleading.
                is_zb = all_zb_in_item if is_hard_custom else any_zb_in_item
                if is_hard_custom and any_zb_in_item and not all_zb_in_item:
                    _debug("  CUSTOM_HARD_MIXED_AS_NORMAL: item_size={} | item={}".format(
                        len(item),
                        ", ".join(_short_name(m.get("name")) for m in item if m.get("name"))
                    ))

                if true_vol < 1e-6 and not is_hard_custom:
                    target_list = bolt_items
                    target_list.append(item)
                    continue

                max_single_diag = max([m.get("diag", m.get("radius", 0) * 2) for m in item] or [0])

                is_bolt_shape = False
                is_wire_shape = False
                valid_shape_count = 0
                wire_like_count = 0
                bolt_shape_count = 0
                for mesh_info in item:
                    mesh_metrics = get_shape_metrics(mesh_info)
                    if not mesh_metrics:
                        continue
                    valid_shape_count += 1
                    mesh_elongation = mesh_metrics[0]
                    mesh_symmetry = mesh_metrics[1]
                    mesh_wire_threshold = 8.0 if mesh_info.get("is_zbrush", False) else self.wire_elongation
                    if mesh_elongation > mesh_wire_threshold and mesh_info.get("bbox_vol", 0.0) < 0.05:
                        wire_like_count += 1
                    symmetry_ok = (not self.use_symmetry) or mesh_symmetry < self.bolt_symmetry
                    if (
                        not mesh_info.get("is_zbrush", False)
                        and mesh_elongation < self.bolt_elongation
                        and symmetry_ok
                    ):
                        bolt_shape_count += 1

                # Classify the complete item so a different first member cannot
                # change the result. Missing metrics cannot form a majority.
                has_shape_quorum = valid_shape_count * 2 > len(item)
                is_wire_shape = bool(
                    has_shape_quorum
                    and wire_like_count * 2 > valid_shape_count
                    and true_vol < 0.05
                )
                is_bolt_shape = bool(
                    not is_zb
                    and not is_hard_custom
                    and has_shape_quorum
                    and bolt_shape_count * 2 > valid_shape_count
                    and cluster_diag <= medium_threshold
                )

                if not valid_shape_count and not is_zb and not is_hard_custom:
                    avg_variance = sum(float(m.get("variance", 999.0)) for m in item) / max(len(item), 1)
                    is_bolt_shape = avg_variance < 0.5

                bolt_like_count = 0 if (is_zb or is_hard_custom) else sum(1 for m in item if _mesh_is_bolt_like(m))
                all_parts_bolt_like = bolt_like_count == len(item)
                partial_bolt_like = 0 < bolt_like_count < len(item)
                is_mixed_bolt_item = (
                    not is_zb
                    and not is_hard_custom
                    and len(item) > 1
                    and bolt_like_count > 0
                    and (
                        (all_parts_bolt_like and cluster_diag <= (medium_threshold * 1.35))
                        or (partial_bolt_like and cluster_diag <= medium_threshold)
                    )
                    and max_single_diag <= large_threshold
                )

                if not is_wire_shape and not is_hard_custom and (is_bolt_shape or is_mixed_bolt_item or (max_single_diag <= small_threshold and not is_zb)):
                    if is_mixed_bolt_item:
                        bolt_mixed_reclass_count += 1
                        _debug("  BOLT_RECLASS: item_size={} | bolt_like_parts={} | cluster_diag={:.6f} | item={}".format(
                            len(item),
                            bolt_like_count,
                            cluster_diag,
                            ", ".join(_short_name(m.get("name")) for m in item if m.get("name"))
                        ))
                    bolt_items.append(item)
                elif cluster_diag <= medium_threshold:
                    target_list = medium_zb if is_zb else medium_items
                    target_list.append(item)
                elif cluster_diag <= large_threshold:
                    target_list = large_zb if is_zb else large_items
                    target_list.append(item)
                else:
                    target_list = huge_zb if is_zb else huge_items
                    target_list.append(item)

            def _build_lp_family_category_stats():
                stats = {}

                def _register(items, category):
                    for item in items:
                        meta = _get_item_meta(item)
                        lp_cluster_id = meta.get('lp_cluster_id')
                        if not lp_cluster_id:
                            continue
                        family = stats.setdefault(lp_cluster_id, {
                            'huge': 0,
                            'large': 0,
                            'medium': 0,
                            'small': 0,
                            'bolts': 0,
                            'huge_meshes': 0,
                            'large_meshes': 0,
                            'medium_meshes': 0,
                            'small_meshes': 0,
                            'bolt_meshes': 0
                        })
                        family[category] += 1
                        mesh_key = 'bolt_meshes' if category == 'bolts' else category + '_meshes'
                        family[mesh_key] += len(item)

                _register(huge_items, 'huge')
                _register(large_items, 'large')
                _register(medium_items, 'medium')
                _register(small_items, 'small')
                _register(bolt_items, 'bolts')
                return stats

            lp_family_category_stats = _build_lp_family_category_stats()
            lp_family_reclass_count = 0

            def _should_reclass_lp_family_bolt(item):
                meta = _get_item_meta(item)
                if meta.get('source') != 'lp':
                    return False
                if meta.get('custom_cluster'):
                    return False
                if is_zbrush_cluster(item):
                    return False

                lp_cluster_id = meta.get('lp_cluster_id')
                if not lp_cluster_id:
                    return False

                stats = lp_family_category_stats.get(lp_cluster_id)
                if not stats:
                    return False

                true_vol, cluster_diag = get_cluster_metrics(item)
                max_single_diag = max([m.get("diag", m.get("radius", 0) * 2) for m in item] or [0])
                if cluster_diag > medium_threshold or max_single_diag > large_threshold:
                    return False

                bolt_meshes = stats.get('bolt_meshes', 0)
                medium_meshes = stats.get('medium_meshes', 0)
                small_meshes = stats.get('small_meshes', 0)
                comparable_meshes = max(bolt_meshes + medium_meshes + small_meshes, 1)
                bolt_ratio = bolt_meshes / float(comparable_meshes)
                return bolt_meshes >= 4 and bolt_ratio >= 0.50

            def _promote_lp_family_bolts(items, source_category):
                promoted = []
                kept = []
                for item in items:
                    if _should_reclass_lp_family_bolt(item):
                        promoted.append(item)
                        meta = _get_item_meta(item)
                        _debug("  LP_FAMILY_RECLASS: {} -> Bolts | lp_cluster={} | item={}".format(
                            source_category,
                            meta.get('lp_cluster_id'),
                            ", ".join(_short_name(m.get("name")) for m in item if m.get("name"))
                        ))
                    else:
                        kept.append(item)
                return kept, promoted

            medium_items, _promoted_medium_bolts = _promote_lp_family_bolts(medium_items, "Medium")
            small_items, _promoted_small_bolts = _promote_lp_family_bolts(small_items, "Small")
            if _promoted_medium_bolts or _promoted_small_bolts:
                lp_family_reclass_count = len(_promoted_medium_bolts) + len(_promoted_small_bolts)
                bolt_items.extend(_promoted_medium_bolts)
                bolt_items.extend(_promoted_small_bolts)
                logs.append("LP family bolt reclass: moved {} Medium/Small item(s) into Bolts.".format(lp_family_reclass_count))

            for q in [huge_items, huge_zb, large_items, large_zb, medium_items, medium_zb, small_items, small_zb, bolt_items, bolt_zb]:
                q.sort(key=lambda x: get_cluster_metrics(x)[0], reverse=True)

            _debug("Step 6: size/category buckets.")
            _debug("  normal: huge={}, large={}, medium={}, small={}, bolts={}".format(
                len(huge_items), len(large_items), len(medium_items), len(small_items), len(bolt_items)
            ))
            _debug("  zbrush: huge={}, large={}, medium={}, small={}, bolts={}".format(
                len(huge_zb), len(large_zb), len(medium_zb), len(small_zb), len(bolt_zb)
            ))
            if bolt_mixed_reclass_count:
                _debug("  mixed bolt-like item(s) reclassified to bolts={}".format(bolt_mixed_reclass_count))
            if lp_family_reclass_count:
                _debug("  LP family bolt reclass item(s)={}".format(lp_family_reclass_count))
            _debug("")

            self.progress_value.emit(70)
            self.progress_text.emit("Step 7: Spatial Hashing and Packing...")

            # --- STEP 7: SPATIAL PACKING ---
            cell_size = max(median_scene_diag, 0.1)
            grid = {}

            def get_cells(bbox, max_cells=4096):
                return bounded_grid_cells(bbox, cell_size, max_cells=max_cells)

            buckets = []
            bucket_names = []
            bucket_metas = []
            pack_queues = [huge_zb, large_zb, medium_zb, small_zb, huge_items, large_items, medium_items, small_items]
            if safe_grouping:
                pack_queues.extend([bolt_items, bolt_zb])
            pack_total = sum(len(q) for q in pack_queues)
            pack_done = [0]

            def _item_label(item, limit=5):
                names = [_short_name(m.get("name")) for m in (item or []) if m.get("name")]
                if len(names) > limit:
                    names = names[:limit] + ["... +{} more".format(len(names) - limit)]
                return ", ".join(names) if names else "empty"

            def _bucket_prefix_counts():
                counts = {}
                for b_name in bucket_names:
                    prefix = b_name.split('.')[0]
                    counts[prefix] = counts.get(prefix, 0) + 1
                return ", ".join("{}={}".format(k, v) for k, v in sorted(counts.items())) or "none"

            def generate_name(prefix):
                idx = 1
                while True:
                    name = "{}.{:03d}".format(prefix, idx)
                    if name not in self.locked_names and name not in bucket_names:
                        return name
                    idx += 1

            def _new_bucket_meta(item, prefix):
                meta = dict(_get_item_meta(item))
                lp_cluster_id = meta.get('lp_cluster_id')
                lp_cluster_size = int(meta.get('lp_cluster_size') or 0)
                return {
                    'prefix': prefix,
                    'owner_lps': set([meta.get('owner_lp')]) if meta.get('owner_lp') else set(),
                    'owner_lp_bases': set([meta.get('owner_lp_base')]) if meta.get('owner_lp_base') else set(),
                    'lp_clusters': set([lp_cluster_id]) if lp_cluster_id else set(),
                    'small_lp_clusters': set([lp_cluster_id]) if lp_cluster_id and lp_cluster_size <= 12 else set(),
                    'custom_clusters': set([meta.get('custom_cluster')]) if meta.get('custom_cluster') else set(),
                    'material_slots': set([meta.get('material_slot')]) if meta.get('material_slot') else set(_item_material_slots(item)),
                    'sources': set([meta.get('source')]) if meta.get('source') else set(),
                    'hard_custom': bool(meta.get('hard_custom')),
                }

            def _update_bucket_meta(bucket_meta, item):
                meta = _get_item_meta(item)
                if meta.get('owner_lp'):
                    bucket_meta['owner_lps'].add(meta.get('owner_lp'))
                if meta.get('owner_lp_base'):
                    bucket_meta['owner_lp_bases'].add(meta.get('owner_lp_base'))
                lp_cluster_id = meta.get('lp_cluster_id')
                lp_cluster_size = int(meta.get('lp_cluster_size') or 0)
                if lp_cluster_id:
                    bucket_meta['lp_clusters'].add(lp_cluster_id)
                    if lp_cluster_size <= 12:
                        bucket_meta['small_lp_clusters'].add(lp_cluster_id)
                if meta.get('custom_cluster'):
                    bucket_meta['custom_clusters'].add(meta.get('custom_cluster'))
                if meta.get('material_slot'):
                    bucket_meta.setdefault('material_slots', set()).add(meta.get('material_slot'))
                for slot in _item_material_slots(item):
                    bucket_meta.setdefault('material_slots', set()).add(slot)
                if meta.get('source'):
                    bucket_meta['sources'].add(meta.get('source'))
                if meta.get('hard_custom'):
                    bucket_meta['hard_custom'] = True

            def _has_collision_with_bucket(item, bucket):
                for m_item in item:
                    for m_bucket in bucket:
                        if safe_grouping and bg_core.MathUtils.is_overlapping(
                                m_item, m_bucket, padding=self.match_padding):
                            return True
                        if check_collision(m_item, m_bucket):
                            return True
                return False

            def _pack_candidate_score(item, bucket, bucket_meta, bucket_idx, potential_bucket_idx):
                meta = _get_item_meta(item)
                if meta.get('hard_custom') or bucket_meta.get('hard_custom'):
                    return -999999.0, False, "hard_custom"
                item_material_slots = _item_material_slots(item)
                bucket_material_slots = bucket_meta.get('material_slots') or set()
                if item_material_slots and bucket_material_slots and not item_material_slots.intersection(bucket_material_slots):
                    return -999999.0, False, "material"
                same_owner = bool(meta.get('owner_lp') and meta.get('owner_lp') in bucket_meta['owner_lps'])
                same_owner_base = bool(meta.get('owner_lp_base') and meta.get('owner_lp_base') in bucket_meta['owner_lp_bases'])
                same_custom = bool(meta.get('custom_cluster') and meta.get('custom_cluster') in bucket_meta['custom_clusters'])
                lp_cluster_id = meta.get('lp_cluster_id')
                lp_cluster_size = int(meta.get('lp_cluster_size') or 0)
                same_small_lp_cluster = bool(
                    lp_cluster_id and lp_cluster_size <= 12 and lp_cluster_id in bucket_meta['small_lp_clusters']
                )
                has_lp_context = bool(meta.get('owner_lp') or meta.get('owner_lp_base') or lp_cluster_id)
                spatial_candidate = bucket_idx in potential_bucket_idx

                item_bb = item_bbox(item)
                bucket_bb = item_bbox(bucket)
                item_center = bbox_center(item_bb)
                bucket_center = bbox_center(bucket_bb)
                dist = point_distance(item_center, bucket_center)
                scale = max(bbox_diag(item_bb), bbox_diag(bucket_bb), median_scene_diag, 0.001)
                nearby = dist <= scale * 2.5

                score = 0.0
                reasons = []
                if same_custom:
                    score += 1000.0
                    reasons.append("custom")
                if same_owner:
                    score += 800.0
                    reasons.append("same_lp")
                if same_owner_base:
                    score += 650.0
                    reasons.append("same_lp_base")
                if same_small_lp_cluster:
                    score += 350.0
                    reasons.append("lp_cluster")
                if spatial_candidate:
                    score += 160.0
                    reasons.append("spatial")
                if nearby:
                    score += 80.0
                    reasons.append("near")

                score -= min(dist / scale, 20.0)
                score -= len(bucket) * 0.005
                meaningful = same_custom or same_owner or same_owner_base or same_small_lp_cluster
                if not meaningful and not has_lp_context:
                    meaningful = spatial_candidate or nearby
                return score, meaningful, ",".join(reasons) if reasons else "loose"

            def pack_queue(items, prefix, is_zb):
                for item in items:
                    if self.is_cancelled:
                        return True
                    pack_done[0] += 1
                    if pack_done[0] % max(1, pack_total // 25) == 0:
                        self.progress_value.emit(70 + int((pack_done[0] / float(max(pack_total, 1))) * 25))
                    placed = False
                    item_prefix = _item_group_prefix(prefix, item)

                    potential_bucket_idx = set()
                    for m in item:
                        for cell in get_cells(m["bbox"]):
                            if cell in grid:
                                potential_bucket_idx.update(grid[cell])

                    if _get_item_meta(item).get('hard_custom'):
                        new_idx = len(buckets)
                        buckets.append(list(item))
                        b_name = generate_name(item_prefix)
                        bucket_names.append(b_name)
                        bucket_metas.append(_new_bucket_meta(item, item_prefix))
                        for m in item:
                            for cell in get_cells(m["bbox"]):
                                grid.setdefault(cell, set()).add(new_idx)
                        _debug("  PACK_CUSTOM_HARD: prefix={} | item_size={} | placed=new '{}' | cluster={} | item={}".format(
                            item_prefix,
                            len(item),
                            b_name,
                            _get_item_meta(item).get('custom_cluster'),
                            _item_label(item)
                        ))
                        continue

                    candidates = []
                    for i, bucket in enumerate(buckets):
                        if not bucket_names[i].startswith(item_prefix + "."):
                            continue
                        if _has_collision_with_bucket(item, bucket):
                            continue

                        score, meaningful, reason = _pack_candidate_score(
                            item, bucket, bucket_metas[i], i, potential_bucket_idx
                        )
                        if meaningful:
                            candidates.append((score, i, reason))

                    if candidates:
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        _, i, reason = candidates[0]
                        bucket = buckets[i]
                        bucket.extend(item)
                        _update_bucket_meta(bucket_metas[i], item)
                        for m in item:
                            for cell in get_cells(m["bbox"]):
                                grid.setdefault(cell, set()).add(i)
                        placed = True
                        _debug("  PACK: prefix={} | item_size={} | placed=existing '{}' | candidates={} | reason={} | item={}".format(
                            item_prefix, len(item), bucket_names[i], len(candidates), reason, _item_label(item)
                        ))

                    if not placed:
                        if prefix == "Medium":
                            existing_medium_indices = [
                                idx for idx, b_name in enumerate(bucket_names)
                                if b_name.startswith(item_prefix + ".")
                            ]
                            if safe_grouping:
                                existing_medium_indices = [
                                    idx for idx in existing_medium_indices
                                    if not _has_collision_with_bucket(item, buckets[idx])
                                ]
                            if existing_medium_indices:
                                item_center = bbox_center(item_bbox(item))
                                relaxed_idx = min(
                                    existing_medium_indices,
                                    key=lambda idx: (
                                        point_distance(item_center, bbox_center(item_bbox(buckets[idx]))),
                                        len(buckets[idx])
                                    )
                                )
                                buckets[relaxed_idx].extend(item)
                                _update_bucket_meta(bucket_metas[relaxed_idx], item)
                                for m in item:
                                    for cell in get_cells(m["bbox"]):
                                        grid.setdefault(cell, set()).add(relaxed_idx)
                                placed = True
                                _debug("  PACK_MEDIUM_SINGLE: prefix={} | item_size={} | placed=existing '{}' | targets={} | item={}".format(
                                    item_prefix, len(item), bucket_names[relaxed_idx], len(existing_medium_indices), _item_label(item)
                                ))

                        if not placed and prefix == "Medium":
                            medium_indices = [
                                idx for idx, b_name in enumerate(bucket_names)
                                if b_name.startswith(item_prefix + ".") and not _has_collision_with_bucket(item, buckets[idx])
                            ]
                            # Let the first far-away medium item create a second bucket,
                            # then fold later loose medium items into the smaller existing
                            # medium bucket instead of spawning Medium.003/004/005.
                            if len(medium_indices) >= 2:
                                smallest_idx = min(
                                    medium_indices,
                                    key=lambda idx: (len(buckets[idx]), sum(m.get("bbox_vol", 0) for m in buckets[idx]))
                                )
                                buckets[smallest_idx].extend(item)
                                _update_bucket_meta(bucket_metas[smallest_idx], item)
                                for m in item:
                                    for cell in get_cells(m["bbox"]):
                                        grid.setdefault(cell, set()).add(smallest_idx)
                                placed = True
                                _debug("  PACK_MEDIUM_BALANCE: prefix={} | item_size={} | placed=existing '{}' | candidates={} | item={}".format(
                                    item_prefix, len(item), bucket_names[smallest_idx], len(medium_indices), _item_label(item)
                                ))

                    if not placed:
                        if prefix == "Medium":
                            all_medium_indices = [
                                idx for idx, b_name in enumerate(bucket_names)
                                if b_name.startswith(item_prefix + ".")
                            ]
                            if safe_grouping:
                                all_medium_indices = [
                                    idx for idx in all_medium_indices
                                    if not _has_collision_with_bucket(item, buckets[idx])
                                ]
                            # Medium groups are helper buckets, not strict collision cages.
                            # After two buckets exist, avoid spawning Medium.003+ and fold
                            # loose leftovers into the closest/smaller medium bucket.
                            if len(all_medium_indices) >= 2:
                                item_center = bbox_center(item_bbox(item))
                                relaxed_idx = min(
                                    all_medium_indices,
                                    key=lambda idx: (
                                        point_distance(item_center, bbox_center(item_bbox(buckets[idx]))),
                                        len(buckets[idx])
                                    )
                                )
                                buckets[relaxed_idx].extend(item)
                                _update_bucket_meta(bucket_metas[relaxed_idx], item)
                                for m in item:
                                    for cell in get_cells(m["bbox"]):
                                        grid.setdefault(cell, set()).add(relaxed_idx)
                                placed = True
                                _debug("  PACK_MEDIUM_RELAXED: prefix={} | item_size={} | placed=existing '{}' | targets={} | item={}".format(
                                    item_prefix, len(item), bucket_names[relaxed_idx], len(all_medium_indices), _item_label(item)
                                ))

                    if not placed:
                        if len(buckets) >= self.group_limit:
                            valid_indices = []
                            for idx, b_name in enumerate(bucket_names):
                                if ("ZBrush" in b_name) == is_zb and b_name.startswith(item_prefix + "."):
                                    if not _has_collision_with_bucket(item, buckets[idx]):
                                        score, meaningful, reason = _pack_candidate_score(
                                            item, buckets[idx], bucket_metas[idx], idx, potential_bucket_idx
                                        )
                                        if meaningful:
                                            valid_indices.append((score, idx, reason))

                            if valid_indices:
                                valid_indices.sort(key=lambda x: (x[0], -sum(m.get("bbox_vol", 0) for m in buckets[x[1]])), reverse=True)
                                _, smallest_idx, reason = valid_indices[0]

                                buckets[smallest_idx].extend(item)
                                _update_bucket_meta(bucket_metas[smallest_idx], item)
                                for m in item:
                                    for cell in get_cells(m["bbox"]):
                                        grid.setdefault(cell, set()).add(smallest_idx)
                                placed = True
                                _debug("  PACK_LIMIT: prefix={} | item_size={} | placed=existing '{}' | valid_targets={} | reason={} | item={}".format(
                                    item_prefix, len(item), bucket_names[smallest_idx], len(valid_indices), reason, _item_label(item)
                                ))

                        if not placed:
                            new_idx = len(buckets)
                            buckets.append(list(item))
                            b_name = generate_name(item_prefix)
                            bucket_names.append(b_name)
                            bucket_metas.append(_new_bucket_meta(item, item_prefix))
                            for m in item:
                                for cell in get_cells(m["bbox"]):
                                    grid.setdefault(cell, set()).add(new_idx)
                            _debug("  PACK: prefix={} | item_size={} | placed=new '{}' | bucket_counts_after={} | item={}".format(
                                item_prefix, len(item), b_name, _bucket_prefix_counts(), _item_label(item)
                            ))
                return False

            if pack_queue(huge_zb, "ZBrush_Huge", True): return True
            if pack_queue(large_zb, "ZBrush_Large", True): return True
            if pack_queue(medium_zb, "ZBrush_Medium", True): return True
            if pack_queue(small_zb, "ZBrush_Small", True): return True

            if pack_queue(huge_items, "Huge", False): return True
            if pack_queue(large_items, "Large", False): return True
            if pack_queue(medium_items, "Medium", False): return True
            if pack_queue(small_items, "Small", False): return True
            if safe_grouping:
                if pack_queue(bolt_items, "Bolts", False): return True
                if pack_queue(bolt_zb, "ZBrush_Bolts", True): return True

            def _unique_group_name(base_name):
                if base_name not in groups:
                    return base_name
                idx = 1
                while True:
                    candidate = "{}_Auto_{:03d}".format(base_name, idx)
                    if candidate not in groups:
                        return candidate
                    idx += 1

            for name, b_items in zip(bucket_names, buckets):
                groups[_unique_group_name(name)] = b_items

            def _add_bolt_groups(items, prefix):
                by_prefix = {}
                for item in items:
                    by_prefix.setdefault(_item_group_prefix(prefix, item), []).extend(item)
                for item_prefix in sorted(by_prefix.keys()):
                    groups[_unique_group_name("{}.001".format(item_prefix))] = by_prefix[item_prefix]

            if bolt_items and not safe_grouping:
                _add_bolt_groups(bolt_items, "Bolts")

            if bolt_zb and not safe_grouping:
                _add_bolt_groups(bolt_zb, "ZBrush_Bolts")

            # Keep separate ZBrush semantic/custom clusters. Tandem-style chapters can
            # have several distinct ZBrush islands that should not be flattened into
            # one big ZBrush group after packing.

            # No late GT/manual hard override here: GT clusters were already injected
            # as cluster items before categorization/packing. Creating named groups here
            # would multiply subgroups again.

        if _step6_7_categorize_and_pack():
            return
        self.progress_value.emit(96)
        self.progress_text.emit("Step 7.5: Validating LP ownership...")

        ownership_repair_moves = 0
        ownership_conflict_count = 0
        ownership_skipped_protected = 0

        def _is_zbrush_group_name(group_name):
            return str(group_name).startswith("ZBrush_")

        def _is_protected_hp_info(hp_info):
            uid = hp_info.get('uuid')
            if uid and uid in gt_hard_uuids:
                return True
            if uid and uid in gt_unprotected_uuids:
                return False
            return bool(uid and (uid in gt_protected_uuids or uid in self.manual_uuid_set))

        def _build_group_maps():
            hp_to_group = {}
            hp_info_by_name = {}
            lp_to_group_hps = {}
            unowned = []
            for group_name, meshes in groups.items():
                for hp_info in meshes:
                    hp_name = hp_info.get("name")
                    if not hp_name:
                        continue
                    hp_to_group[hp_name] = group_name
                    hp_info_by_name[hp_name] = hp_info
                    claim = hp_claims.get(hp_name)
                    owner_lp = claim.get("owner_lp") if claim else None
                    if owner_lp and owner_lp in self.lp_data:
                        lp_to_group_hps.setdefault(owner_lp, {}).setdefault(group_name, []).append(hp_name)
                    elif not hp_info.get("is_zbrush", False) and not _is_protected_hp_info(hp_info):
                        unowned.append(hp_name)
            return hp_to_group, hp_info_by_name, lp_to_group_hps, unowned

        hp_to_group, hp_info_by_name, lp_to_group_hps, unowned_hp_names = _build_group_maps()

        move_plan = {}
        for owner_lp, group_map in sorted(lp_to_group_hps.items(), key=lambda kv: _short_name(kv[0])):
            active_group_map = dict((g, list(names)) for g, names in group_map.items() if g in groups and names)
            if len(active_group_map) <= 1:
                continue

            ownership_conflict_count += 1

            def _group_claim_tuple(group_name):
                hp_names = active_group_map.get(group_name, [])
                claim_score = sum(float(hp_claims.get(hp, {}).get("score", 0.0)) for hp in hp_names)
                volume_score = sum(float(hp_info_by_name.get(hp, {}).get("bbox_vol", 0.0)) for hp in hp_names)
                return (claim_score, volume_score, len(hp_names))

            normal_target_candidates = [
                group_name for group_name in active_group_map.keys()
                if not _is_zbrush_group_name(group_name)
            ]
            target_candidates = normal_target_candidates or list(active_group_map.keys())
            target_group = max(target_candidates, key=_group_claim_tuple)
            _debug("  OWNERSHIP_CONFLICT: LP='{}' | groups={} | target='{}'".format(
                _short_name(owner_lp),
                ", ".join("{}:{}".format(g, len(active_group_map[g])) for g in sorted(active_group_map.keys())),
                target_group
            ))

            for source_group, hp_names in active_group_map.items():
                if source_group == target_group:
                    continue
                movable_names = []
                for hp_name in hp_names:
                    hp_info = hp_info_by_name.get(hp_name, {})
                    if hp_info.get("is_zbrush", False) or _is_protected_hp_info(hp_info):
                        ownership_skipped_protected += 1
                        continue
                    if _is_zbrush_group_name(target_group):
                        ownership_skipped_protected += 1
                        continue
                    movable_names.append(hp_name)

                if not movable_names:
                    continue

                move_plan.setdefault(source_group, {}).setdefault(target_group, set()).update(movable_names)

        for source_group, target_map in sorted(move_plan.items()):
            if source_group not in groups:
                continue
            for target_group, hp_names in sorted(target_map.items()):
                if target_group not in groups:
                    continue
                moving = []
                keep = []
                hp_name_set = set(hp_names)
                for hp_info in groups.get(source_group, []):
                    if hp_info.get("name") in hp_name_set:
                        moving.append(hp_info)
                    else:
                        keep.append(hp_info)
                if not moving:
                    continue
                groups[source_group] = keep
                groups[target_group].extend(moving)
                ownership_repair_moves += len(moving)
                _debug("  OWNERSHIP_MOVE: '{}' -> '{}' | moved={} | HP={}".format(
                    source_group,
                    target_group,
                    len(moving),
                    ", ".join(_short_name(m.get("name")) for m in moving if m.get("name"))
                ))

        empty_groups = [group_name for group_name, meshes in groups.items() if not meshes]
        for group_name in empty_groups:
            del groups[group_name]
            _debug("  OWNERSHIP_DROP_EMPTY_GROUP: '{}'".format(group_name))

        if unowned_hp_names:
            _debug_list("  UNOWNED_HP_AFTER_PACK: ", unowned_hp_names)
        _debug("Step 7.5: LP ownership validation | conflicts={} | moved_hp={} | skipped_protected={} | unowned={}.".format(
            ownership_conflict_count,
            ownership_repair_moves,
            ownership_skipped_protected,
            len(unowned_hp_names)
        ))
        if ownership_repair_moves:
            logs.append("LP ownership repair: moved {} HP mesh(es) between groups after packing.".format(ownership_repair_moves))
        if unowned_hp_names:
            logs.append("[Warning] {} HP mesh(es) have no reliable LP owner after Analyze HP. See debug log.".format(len(unowned_hp_names)))

        if self.grouping_mode == "one_lp_per_group":
            strict_groups = {}
            strict_assigned_names = set()

            def _strict_unique_name(base_name):
                candidate = base_name
                suffix = 1
                while candidate in strict_groups:
                    suffix += 1
                    candidate = "{}_{:03d}".format(base_name, suffix)
                return candidate

            # Explicit GT/manual links take priority over automatic LP boundaries.
            for old_group_name in sorted(groups.keys()):
                old_meshes = groups.get(old_group_name, [])
                has_hard_override = any(
                    m.get("uuid") in gt_hard_uuids or m.get("uuid") in self.manual_uuid_set
                    for m in old_meshes
                )
                if not has_hard_override:
                    continue
                strict_groups[_strict_unique_name(old_group_name)] = list(old_meshes)
                strict_assigned_names.update(
                    m.get("name") for m in old_meshes if m.get("name")
                )

            for lp_base in sorted(lp_base_to_lps.keys(), key=lambda value: lp_base_order.get(value, 999999)):
                owned_hps = []
                seen_names = set()
                for lp_name in lp_base_to_lps.get(lp_base, []):
                    for hp_info in lp_to_owned_hps.get(lp_name, []):
                        hp_name = hp_info.get("name")
                        if not hp_name or hp_name in seen_names or hp_name in strict_assigned_names:
                            continue
                        seen_names.add(hp_name)
                        owned_hps.append(hp_info)
                if not owned_hps:
                    continue

                lp_token = _short_name(lp_base)
                lp_token = re.sub(r"(?i)(?:_LP|_low)$", "", lp_token)
                lp_token = re.sub(r"[^0-9A-Za-z_]+", "_", lp_token).strip("_")
                group_name = _strict_unique_name("Pair_{}".format(lp_token or "LP"))
                strict_groups[group_name] = owned_hps
                strict_assigned_names.update(seen_names)

            # Keep unresolved HP meshes instead of silently dropping them.
            for old_group_name in sorted(groups.keys()):
                remaining = [
                    hp_info for hp_info in groups.get(old_group_name, [])
                    if hp_info.get("name") not in strict_assigned_names
                ]
                if remaining:
                    strict_groups[_strict_unique_name("Unmatched_{}".format(old_group_name))] = remaining

            groups = strict_groups
            logs.append("One LP per group: rebuilt {} bake group(s) from LP ownership.".format(len(groups)))
            _debug("Step 7.6: one-LP regroup complete | groups={}.".format(len(groups)))

        matched_count = len(self.hp_data) - len(unassigned_hps)
        logs.append("Matched {} objects via Broad/Narrow phase.".format(matched_count))
        if self.grouping_mode == "one_lp_per_group":
            logs.append("Total groups created: {} (one LP per group; automatic limit ignored)".format(len(groups)))
        else:
            logs.append("Total groups created: {} (Limit: {})".format(len(groups), self.group_limit))

        self.progress_value.emit(96)

        # --- ENFORCED COMPLIANCE WITH SUBGROUP LIMITS (SMART MERGE) ---
        # GT/manual/custom links are already baked into cluster items, so normal
        # subgroup merging can run without creating protected one-cluster groups.
        def _is_limit_exempt_group(group_name):
            name = re.sub(r"^M\d{2,}_", "", str(group_name))
            return (
                name.startswith("Bolts.")
                or name.startswith("Bolts_")
                or name.startswith("ZBrush_Bolts.")
                or name.startswith("ZBrush_Bolts_")
            )

        group_order_by_name = dict(
            (group_name, index) for index, group_name in enumerate(groups.keys())
        )
        group_revisions = dict((group_name, 0) for group_name in groups.keys())
        group_metadata_cache = {}

        def _group_revision(group_name):
            if group_name not in groups:
                return None
            return group_revisions.get(group_name, 0)

        def _invalidate_group_metadata(group_name):
            group_revisions[group_name] = group_revisions.get(group_name, 0) + 1
            group_metadata_cache.pop(group_name, None)

        def _group_metadata(group_name):
            revision = _group_revision(group_name)
            cached = group_metadata_cache.get(group_name)
            if cached is not None and cached[0] == revision:
                return cached[1]

            meshes = groups.get(group_name, [])
            owner_lp_bases = set()
            material_slots = set()
            signature_values = []
            for hp_info in meshes:
                hp_name = hp_info.get("name")
                claim = hp_claims.get(hp_name, {}) if hp_name else {}
                owner_lp = claim.get("owner_lp")
                if owner_lp:
                    owner_lp_bases.add(_lp_base_name(owner_lp))
                slot = _claim_material_slot(hp_name)
                if slot:
                    material_slots.add(slot)
                signature_values.append(str(
                    hp_info.get("uuid") or hp_name or id(hp_info)
                ))

            metadata = {
                "bbox": item_bbox(meshes),
                "volume": sum(float(m.get("bbox_vol", 0.0) or 0.0) for m in meshes),
                "count": len(meshes),
                "is_zbrush": bool(meshes) and all(m.get("is_zbrush", False) for m in meshes),
                "hard_custom": any(m.get("uuid") in gt_hard_uuids for m in meshes),
                "owner_lp_bases": owner_lp_bases,
                "material_slots": material_slots,
                "signature": tuple(sorted(signature_values))
            }
            group_metadata_cache[group_name] = (revision, metadata)
            return metadata

        def _group_is_zbrush_like(group_name):
            if str(group_name).startswith("ZBrush_"):
                return True
            return _group_metadata(group_name)["is_zbrush"]

        def _group_has_hard_custom(group_name):
            return _group_metadata(group_name)["hard_custom"]

        def _group_owner_lp_bases(group_name):
            return _group_metadata(group_name)["owner_lp_bases"]

        def _group_material_slots(group_name):
            return _group_metadata(group_name)["material_slots"]

        def _groups_share_bake_context(group_a, group_b):
            slots_a = _group_material_slots(group_a)
            slots_b = _group_material_slots(group_b)
            if slots_a and slots_b and not slots_a.intersection(slots_b):
                return False
            bases_a = _group_owner_lp_bases(group_a)
            bases_b = _group_owner_lp_bases(group_b)
            if bases_a and bases_b:
                return bool(bases_a.intersection(bases_b))
            # Groups with no LP context are fallback/shape leftovers; they can
            # still merge by space because there is no better bake owner signal.
            return not bases_a and not bases_b

        def _group_bbox(group_name):
            return _group_metadata(group_name)["bbox"]

        def _bbox_is_disjoint(bb_a, bb_b):
            return (
                bb_a[3] < bb_b[0] or bb_a[0] > bb_b[3] or
                bb_a[4] < bb_b[1] or bb_a[1] > bb_b[4] or
                bb_a[5] < bb_b[2] or bb_a[2] > bb_b[5]
            )

        def _group_volume(group_name):
            return _group_metadata(group_name)["volume"]

        def _group_prefix(group_name):
            name = str(group_name)
            material_match = re.match(r"^(M\d{2,})_(.+)$", name)
            if material_match:
                name = material_match.group(2)
            if name.startswith("ZBrush_"):
                name = name[len("ZBrush_"):]
            return name.split(".")[0].split("_Auto_")[0]

        def _group_size_rank(group_name):
            prefix = _group_prefix(group_name)
            if prefix.startswith("Small"):
                return 0
            if prefix.startswith("Medium"):
                return 1
            if prefix.startswith("Large"):
                return 2
            if prefix.startswith("Huge"):
                return 3
            return 1

        collision_cache = {}

        def _group_signature(group_name):
            return _group_metadata(group_name)["signature"]

        def _group_has_collision(group_a, group_b):
            sig_a = _group_signature(group_a)
            sig_b = _group_signature(group_b)
            cache_key = (sig_a, sig_b) if sig_a <= sig_b else (sig_b, sig_a)
            if cache_key in collision_cache:
                return collision_cache[cache_key]

            bb_a = _group_bbox(group_a)
            bb_b = _group_bbox(group_b)
            if not safe_grouping and _bbox_is_disjoint(bb_a, bb_b):
                collision_cache[cache_key] = False
                return False
            for mesh_a in groups.get(group_a, []):
                for mesh_b in groups.get(group_b, []):
                    if safe_grouping and bg_core.MathUtils.is_overlapping(
                            mesh_a, mesh_b, padding=self.match_padding):
                        collision_cache[cache_key] = True
                        return True
                    if check_collision(mesh_a, mesh_b):
                        collision_cache[cache_key] = True
                        return True
            collision_cache[cache_key] = False
            return False

        def _final_merge_candidate(source_group, target_group, mode="strict"):
            if source_group == target_group:
                return None
            if source_group not in groups or target_group not in groups:
                return None
            if source_group in protected_group_names or target_group in protected_group_names:
                return None
            if _is_limit_exempt_group(source_group) or _is_limit_exempt_group(target_group):
                return None

            source_is_zb = _group_is_zbrush_like(source_group)
            target_is_zb = _group_is_zbrush_like(target_group)
            if source_is_zb != target_is_zb:
                return None
            source_slots = _group_material_slots(source_group)
            target_slots = _group_material_slots(target_group)
            if source_slots and target_slots and not source_slots.intersection(target_slots):
                return None

            relaxed = mode in ("relaxed", "macro", "polish")
            macro = mode == "macro"
            polish = mode == "polish"

            source_count = len(groups.get(source_group, []))
            target_count = len(groups.get(target_group, []))
            if source_count <= 0 or target_count <= 0:
                return None

            source_vol = max(_group_volume(source_group), 0.000001)
            target_vol = max(_group_volume(target_group), 0.000001)
            # Move the smaller/more local island into a larger compatible bucket.
            if source_vol > target_vol * 1.15 and source_count > target_count:
                return None

            source_bb = _group_bbox(source_group)
            target_bb = _group_bbox(target_group)
            source_diag = max(bbox_diag(source_bb), 0.000001)
            target_diag = max(bbox_diag(target_bb), 0.000001)
            if source_vol > target_vol * 1.25 and source_diag > target_diag * 1.05:
                return None
            diag_ratio = min(source_diag, target_diag) / max(source_diag, target_diag, 0.000001)

            source_rank = _group_size_rank(source_group)
            target_rank = _group_size_rank(target_group)
            rank_delta = abs(source_rank - target_rank)
            source_lacks_lp_context = not bool(_group_owner_lp_bases(source_group))
            same_context = _groups_share_bake_context(source_group, target_group)
            same_prefix = _group_prefix(source_group) == _group_prefix(target_group)
            if polish and not same_prefix:
                return None
            is_zbrush_detail = (
                source_is_zb
                and (
                    source_count <= 12
                    or source_rank <= 1
                    or source_lacks_lp_context
                    or source_diag <= target_diag * 0.35
                )
            )

            if source_is_zb:
                if not is_zbrush_detail:
                    if rank_delta > (3 if macro else (2 if relaxed else 1)):
                        return None
                    if diag_ratio < (0.08 if macro else (0.12 if relaxed else 0.20)):
                        return None
                else:
                    if diag_ratio < (0.008 if macro else (0.02 if relaxed else 0.035)):
                        return None
            else:
                if polish:
                    if rank_delta > 0:
                        return None
                    if diag_ratio < 0.08 and not same_context:
                        return None
                elif rank_delta > (2 if macro else (1 if relaxed else 0)):
                    return None
                elif diag_ratio < (0.10 if macro else (0.18 if relaxed else 0.30)) and not same_context:
                    return None

            has_collision = _group_has_collision(source_group, target_group)
            allow_zbrush_surface_attach = (
                has_collision
                and source_is_zb
                and is_zbrush_detail
                and (macro or source_lacks_lp_context)
                and source_diag <= target_diag * 0.55
                and source_vol <= target_vol * 0.45
            )
            if has_collision and not allow_zbrush_surface_attach:
                return None

            source_center = bbox_center(source_bb)
            target_center = bbox_center(target_bb)
            dist = point_distance(source_center, target_center)
            dist_scale = max(source_diag, target_diag, median_scene_diag, 0.001)
            near_range = 6.0 if polish else (5.0 if macro else (3.5 if relaxed else 2.25))
            near_score = max(0.0, 1.0 - min(dist / (dist_scale * near_range), 1.0))
            if polish and near_score <= 0.0 and not same_context:
                return None

            score = 0.0
            score += 500.0 if same_prefix else 0.0
            score += 350.0 if same_context else 0.0
            score += 240.0 if source_is_zb and is_zbrush_detail else 0.0
            score += 220.0 if allow_zbrush_surface_attach else 0.0
            score += 120.0 if macro and source_lacks_lp_context else 0.0
            score += 120.0 if polish else 0.0
            score += 180.0 * diag_ratio
            score += 160.0 * near_score
            score += min(target_count, 80) * 0.5
            score -= rank_delta * 45.0
            score -= 30.0 if (not same_context and mode == "strict") else 0.0
            score -= 80.0 if macro and not (same_context or same_prefix or is_zbrush_detail) else 0.0

            reasons = []
            if same_prefix:
                reasons.append("same_size")
            if same_context:
                reasons.append("same_lp_base")
            if source_is_zb and is_zbrush_detail:
                reasons.append("zbrush_detail")
            if allow_zbrush_surface_attach:
                reasons.append("zbrush_surface_attach")
            else:
                reasons.append("no_collision")
            if near_score > 0.0:
                reasons.append("near")
            reasons.append(mode)
            return score, ",".join(reasons), diag_ratio, dist

        def _non_exempt_group_names():
            return [
                k for k in groups.keys()
                if k not in protected_group_names and not _is_limit_exempt_group(k)
            ]

        def _run_final_group_clustering(target_count, mode="strict", stop_at_target=True, max_merges=None):
            merge_count = 0
            active_names = _non_exempt_group_names()
            if stop_at_target and len(active_names) <= target_count:
                return merge_count
            if max_merges is not None and merge_count >= max_merges:
                return merge_count

            queue = RevisionedCandidateQueue(
                candidate_getter=lambda source, target: _final_merge_candidate(
                    source, target, mode=mode
                ),
                revision_getter=_group_revision,
                source_key_getter=lambda name: (
                    _group_volume(name),
                    len(groups.get(name, [])),
                    _short_name(name)
                ),
                order_by_name=group_order_by_name,
                cancelled_getter=lambda: self.is_cancelled or self.isInterruptionRequested()
            )
            if not queue.seed(active_names):
                return merge_count

            while True:
                if self.is_cancelled or self.isInterruptionRequested():
                    return merge_count
                if stop_at_target and len(_non_exempt_group_names()) <= target_count:
                    break
                if max_merges is not None and merge_count >= max_merges:
                    break
                best = queue.pop_best()
                if best is None:
                    break

                score, source_group, target_group, reason, diag_ratio, dist = best
                moved = len(groups.get(source_group, []))
                groups[target_group].extend(groups[source_group])
                del groups[source_group]
                _invalidate_group_metadata(target_group)
                merge_count += 1
                _debug("  FINAL_GROUP_CLUSTER: '{}' -> '{}' | moved={} | reason={} | score={:.2f} | size_ratio={:.3f} | dist={:.6f}".format(
                    source_group,
                    target_group,
                    moved,
                    reason,
                    score,
                    diag_ratio,
                    dist
                ))
                if not queue.refresh(target_group, _non_exempt_group_names()):
                    return merge_count
            _debug("  FINAL_GROUP_CLUSTER_QUEUE: mode={} | merges={} | candidate_evaluations={}.".format(
                mode, merge_count, queue.evaluation_count
            ))
            return merge_count

        non_exempt_group_count = len([k for k in groups.keys() if not _is_limit_exempt_group(k)])
        if (self.grouping_mode != "one_lp_per_group" and
                self.group_limit > 0 and non_exempt_group_count > self.group_limit):
            logs.append("[Optimization] The number of non-bolt groups ({}) has exceeded the specified limit ({}).".format(non_exempt_group_count, self.group_limit))
            self.progress_text.emit("Step 8: Final group clustering...")
            _debug("Step 8: final group clustering | start_non_bolt={} | target={}.".format(
                non_exempt_group_count,
                self.group_limit
            ))
            strict_merges = _run_final_group_clustering(self.group_limit, mode="strict")
            relaxed_merges = 0
            if len(_non_exempt_group_names()) > self.group_limit:
                relaxed_merges = _run_final_group_clustering(self.group_limit, mode="relaxed")
            macro_merges = 0
            if len(_non_exempt_group_names()) > self.group_limit:
                macro_merges = _run_final_group_clustering(self.group_limit, mode="macro")
            # Polish is an extra cleanup pass, not a mandate to crush the scene
            # far below HP Max Groups. Let it work only when the initial overflow
            # was meaningful, and cap how far it can go below the user target.
            polish_overflow = max(0, non_exempt_group_count - self.group_limit)
            polish_budget = max(0, min(6, polish_overflow - 1))
            polish_merges = 0
            if polish_budget > 0:
                polish_floor = max(1, self.group_limit - polish_budget)
                polish_merges = _run_final_group_clustering(
                    polish_floor,
                    mode="polish",
                    stop_at_target=True,
                    max_merges=polish_budget
                )
            final_non_exempt_count = len(_non_exempt_group_names())
            logs.append("[Optimization] Final group clustering merged {} group(s). Total number of non-bolt subgroups: {}".format(
                strict_merges + relaxed_merges + macro_merges + polish_merges,
                final_non_exempt_count
            ))
            if final_non_exempt_count > self.group_limit:
                logs.append("[Optimization] Stop merge: {} group(s) remain because no compatible same-type target was found.".format(
                    final_non_exempt_count
                ))
            _debug("Step 8: final group clustering complete | strict_merges={} | relaxed_merges={} | macro_merges={} | polish_merges={} | final_non_bolt={}.".format(
                strict_merges,
                relaxed_merges,
                macro_merges,
                polish_merges,
                final_non_exempt_count
            ))

        if self.is_cancelled:
            return

        _debug("Step 7: final packed groups.")
        for group_name in sorted(groups.keys()):
            meshes = groups[group_name]
            _debug("  GROUP: '{}' | hp_count={}".format(group_name, len(meshes)))
            _debug_list("    HP: ", [m.get("name") for m in meshes if m.get("name")])
        _debug("")

        self.summary_lines = [
            "Analyze HP: {} HP mesh(es) processed into {} group(s).".format(len(self.hp_data), len(groups)),
            "Bake grouping mode: {}.".format(self.grouping_mode),
            "LP-guided matching: {} HP mesh(es) resolved before final packing.".format(matched_count),
            "Compound HP linking: {} component(s), {} linked pair(s), min vertices={}, distance={}%. ".format(
                len(compound_components), compound_hit_pairs, compound_min_hits, self.compound_link_dist_pct
            ).strip(),
            "Debug report is ready: right-click the log window and choose Save Debug Log."
        ]

        self.progress_value.emit(100)
        return groups, logs

    def stop(self):
        with self._terminal_lock:
            self.is_cancelled = True
            self.requestInterruption()
