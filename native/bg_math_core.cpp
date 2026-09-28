#include <pybind11/pybind11.h>
#include <pybind11/stl.h> // Автоматически конвертирует tuple/list в std::vector
#include <vector>
#include <cmath>
#include <thread>
#include <limits>
#include <algorithm>
#include <string>
#include <sstream>
#include <iomanip>
#include <unordered_map>
#include <numeric>
#include <tuple>
#include <utility>
#include <stdexcept>
#include <memory>
#include <array>
#include <functional>
namespace py = pybind11;

// ============================================================================
// 0. СТРУКТУРЫ ДАННЫХ
// ============================================================================

struct MeshMetrics {
    double elongation = 1.0;
    double symmetry_score = 0.0;
    std::vector<double> dimensions = {0.0, 0.0, 0.0};
    std::vector<double> center = {0.0, 0.0, 0.0};
};

struct PosHash {
    size_t operator()(const std::tuple<long long, long long, long long>& v) const {
        const size_t x = std::hash<long long>()(std::get<0>(v));
        const size_t y = std::hash<long long>()(std::get<1>(v));
        const size_t z = std::hash<long long>()(std::get<2>(v));
        return x ^ (y << 1) ^ (z << 7);
    }
};

struct KDNode {
    size_t point_index;
    int left;
    int right;
    unsigned char axis;
};

class KDTree {
public:
    explicit KDTree(const std::vector<double>& points)
        : points_(points), root_(-1) {
        const size_t count = points_.size() / 3;
        indices_.reserve(count);
        for (size_t index = 0; index < count; ++index) {
            const double x = points_[index * 3];
            const double y = points_[index * 3 + 1];
            const double z = points_[index * 3 + 2];
            if (std::isfinite(x) && std::isfinite(y) && std::isfinite(z)) {
                indices_.push_back(index);
            }
        }
        nodes_.reserve(indices_.size());
        root_ = build(0, indices_.size(), 0);
    }

    bool empty() const { return root_ < 0; }

    double nearest_sq(double x, double y, double z) const {
        if (root_ < 0 || !std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) {
            return std::numeric_limits<double>::max();
        }
        double best = std::numeric_limits<double>::max();
        nearest(root_, x, y, z, best);
        return best;
    }

private:
    const std::vector<double>& points_;
    std::vector<size_t> indices_;
    std::vector<KDNode> nodes_;
    int root_;

    double coordinate(size_t point_index, int axis) const {
        return points_[point_index * 3 + static_cast<size_t>(axis)];
    }

    int build(size_t begin, size_t end, int depth) {
        if (begin >= end) return -1;
        const int axis = depth % 3;
        const size_t middle = begin + (end - begin) / 2;
        std::nth_element(
            indices_.begin() + static_cast<std::ptrdiff_t>(begin),
            indices_.begin() + static_cast<std::ptrdiff_t>(middle),
            indices_.begin() + static_cast<std::ptrdiff_t>(end),
            [&](size_t left, size_t right) {
                return coordinate(left, axis) < coordinate(right, axis);
            }
        );
        const int node_index = static_cast<int>(nodes_.size());
        nodes_.push_back({indices_[middle], -1, -1, static_cast<unsigned char>(axis)});
        const int left = build(begin, middle, depth + 1);
        const int right = build(middle + 1, end, depth + 1);
        nodes_[node_index].left = left;
        nodes_[node_index].right = right;
        return node_index;
    }

    void nearest(int node_index, double x, double y, double z, double& best) const {
        if (node_index < 0) return;
        const KDNode& node = nodes_[static_cast<size_t>(node_index)];
        const size_t offset = node.point_index * 3;
        const double dx = x - points_[offset];
        const double dy = y - points_[offset + 1];
        const double dz = z - points_[offset + 2];
        const double distance_sq = dx * dx + dy * dy + dz * dz;
        if (distance_sq < best) best = distance_sq;

        const double query_axis = node.axis == 0 ? x : (node.axis == 1 ? y : z);
        const double split_axis = points_[offset + node.axis];
        const double delta = query_axis - split_axis;
        const int near_node = delta <= 0.0f ? node.left : node.right;
        const int far_node = delta <= 0.0f ? node.right : node.left;
        nearest(near_node, x, y, z, best);
        if (delta * delta <= best) nearest(far_node, x, y, z, best);
    }
};

static unsigned int adaptive_thread_count(size_t outer_count, size_t estimated_work) {
    if (outer_count < 256 || estimated_work < 100000) return 1;
    unsigned int hardware = std::thread::hardware_concurrency();
    if (hardware == 0) hardware = 4;
    return static_cast<unsigned int>(std::max<size_t>(1, std::min<size_t>(16, std::min<size_t>(hardware, outer_count))));
}

// ============================================================================
// 1. АНАЛИЗ ДИСТАНЦИЙ (С МНОГОПОТОЧНОСТЬЮ)
// ============================================================================

double calculate_avg_distance_with_tree(
    const std::vector<double>& lp_verts,
    const KDTree& hp_tree)
{
    const size_t lp_count = lp_verts.size() / 3;
    if (lp_count == 0 || lp_verts.size() % 3 != 0 || hp_tree.empty()) {
        return std::numeric_limits<double>::max();
    }

    const unsigned int num_threads = adaptive_thread_count(lp_count, lp_count * 16);
    std::vector<double> thread_sums(num_threads, 0.0);
    std::vector<size_t> thread_counts(num_threads, 0);

    auto worker = [&](unsigned int thread_id) {
        const size_t start = (lp_count * thread_id) / num_threads;
        const size_t end = (lp_count * (thread_id + 1)) / num_threads;
        double sum = 0.0;
        size_t valid_count = 0;
        for (size_t index = start; index < end; ++index) {
            const double x = lp_verts[index * 3];
            const double y = lp_verts[index * 3 + 1];
            const double z = lp_verts[index * 3 + 2];
            if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) continue;
            const double nearest = hp_tree.nearest_sq(x, y, z);
            if (std::isfinite(nearest) && nearest < std::numeric_limits<double>::max()) {
                sum += std::sqrt(nearest);
                ++valid_count;
            }
        }
        thread_sums[thread_id] = sum;
        thread_counts[thread_id] = valid_count;
    };

    if (num_threads == 1) {
        worker(0);
    } else {
        std::vector<std::thread> threads;
        threads.reserve(num_threads);
        for (unsigned int index = 0; index < num_threads; ++index) threads.emplace_back(worker, index);
        for (auto& thread : threads) thread.join();
    }

    double total_sum = 0.0;
    size_t total_count = 0;
    for (unsigned int index = 0; index < num_threads; ++index) {
        total_sum += thread_sums[index];
        total_count += thread_counts[index];
    }
    return total_count ? total_sum / static_cast<double>(total_count) : std::numeric_limits<double>::max();
}

// Средняя дистанция (используется для тендера HP -> LP)
double calculate_avg_distance(const std::vector<double>& lp_verts, const std::vector<double>& hp_verts) {
    if (hp_verts.empty() || hp_verts.size() % 3 != 0) {
        return std::numeric_limits<double>::max();
    }
    KDTree tree(hp_verts);
    return calculate_avg_distance_with_tree(lp_verts, tree);
}

static std::vector<double> validated_index_target(
    const std::vector<double>& target_verts)
{
    if (target_verts.empty() || target_verts.size() % 3 != 0) {
        throw std::invalid_argument("PointCloudIndex requires complete XYZ points");
    }
    return target_verts;
}

class PointCloudIndex {
public:
    explicit PointCloudIndex(const std::vector<double>& target_verts)
        : target_verts_(validated_index_target(target_verts)), tree_(target_verts_) {
        if (tree_.empty()) {
            throw std::invalid_argument("PointCloudIndex requires at least one finite XYZ point");
        }
    }

    PointCloudIndex(const PointCloudIndex&) = delete;
    PointCloudIndex& operator=(const PointCloudIndex&) = delete;
    PointCloudIndex(PointCloudIndex&&) = delete;
    PointCloudIndex& operator=(PointCloudIndex&&) = delete;

    double average_distance(const std::vector<double>& source_verts) const {
        return calculate_avg_distance_with_tree(source_verts, tree_);
    }

    size_t target_point_count() const {
        return target_verts_.size() / 3;
    }

private:
    std::vector<double> target_verts_;
    KDTree tree_;
};

double calculate_bidirectional_avg_distance(
    const std::vector<double>& verts_a,
    const std::vector<double>& verts_b)
{
    if (verts_a.empty() || verts_b.empty())
        return std::numeric_limits<double>::max();

    const double forward = calculate_avg_distance(verts_a, verts_b);
    const double backward = calculate_avg_distance(verts_b, verts_a);

    return (forward + backward) * 0.5f;
}

// НОВОЕ: Минимальная дистанция (используется для флоатеров и декалей)
double calculate_min_distance(const std::vector<double>& verts_a, const std::vector<double>& verts_b) {
    const size_t count_a = verts_a.size() / 3;
    const size_t count_b = verts_b.size() / 3;
    if (count_a == 0 || count_b == 0 || verts_a.size() % 3 != 0 || verts_b.size() % 3 != 0) {
        return std::numeric_limits<double>::max();
    }
    KDTree tree(verts_b);
    if (tree.empty()) return std::numeric_limits<double>::max();
    double best = std::numeric_limits<double>::max();
    for (size_t index = 0; index < count_a; ++index) {
        best = std::min(best, tree.nearest_sq(
            verts_a[index * 3], verts_a[index * 3 + 1], verts_a[index * 3 + 2]));
        if (best == 0.0) break;
    }
    return best < std::numeric_limits<double>::max() ? std::sqrt(best) : best;
}

std::pair<double, double> calculate_coverage_stats(
    const std::vector<double>& source_verts,
    const std::vector<double>& target_verts,
    double tolerance)
{
    const size_t source_count = source_verts.size() / 3;
    const size_t target_count = target_verts.size() / 3;
    if (source_count == 0 || target_count == 0 ||
            source_verts.size() % 3 != 0 || target_verts.size() % 3 != 0 ||
            !std::isfinite(tolerance) || tolerance < 0.0) {
        return {0.0, std::numeric_limits<double>::infinity()};
    }

    KDTree tree(target_verts);
    if (tree.empty()) return {0.0, std::numeric_limits<double>::infinity()};

    const double tolerance_sq = tolerance * tolerance;
    const unsigned int num_threads = adaptive_thread_count(source_count, source_count * 16);
    std::vector<double> thread_sums(num_threads, 0.0);
    std::vector<size_t> thread_counts(num_threads, 0);
    std::vector<size_t> thread_hits(num_threads, 0);

    auto worker = [&](unsigned int thread_id) {
        const size_t start = (source_count * thread_id) / num_threads;
        const size_t end = (source_count * (thread_id + 1)) / num_threads;
        double sum = 0.0;
        size_t valid_count = 0;
        size_t hits = 0;
        for (size_t index = start; index < end; ++index) {
            const double x = source_verts[index * 3];
            const double y = source_verts[index * 3 + 1];
            const double z = source_verts[index * 3 + 2];
            if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) continue;
            const double nearest = tree.nearest_sq(x, y, z);
            if (!std::isfinite(nearest) || nearest == std::numeric_limits<double>::max()) continue;
            sum += std::sqrt(nearest);
            ++valid_count;
            if (nearest <= tolerance_sq) ++hits;
        }
        thread_sums[thread_id] = sum;
        thread_counts[thread_id] = valid_count;
        thread_hits[thread_id] = hits;
    };

    if (num_threads == 1) {
        worker(0);
    } else {
        std::vector<std::thread> threads;
        threads.reserve(num_threads);
        for (unsigned int index = 0; index < num_threads; ++index) threads.emplace_back(worker, index);
        for (auto& thread : threads) thread.join();
    }

    double total_sum = 0.0;
    size_t total_count = 0;
    size_t total_hits = 0;
    for (unsigned int index = 0; index < num_threads; ++index) {
        total_sum += thread_sums[index];
        total_count += thread_counts[index];
        total_hits += thread_hits[index];
    }
    if (total_count == 0) return {0.0, std::numeric_limits<double>::infinity()};
    return {
        static_cast<double>(total_hits) / static_cast<double>(total_count),
        total_sum / static_cast<double>(total_count)
    };
}


// Обертки GIL
double py_calculate_avg_distance(const std::vector<double>& lp_verts, const std::vector<double>& hp_verts) {
    py::gil_scoped_release release;
    return calculate_avg_distance(lp_verts, hp_verts);
}

double py_calculate_bidirectional_avg_distance(
    const std::vector<double>& verts_a,
    const std::vector<double>& verts_b)
{
    py::gil_scoped_release release;
    return calculate_bidirectional_avg_distance(verts_a, verts_b);
}

double py_calculate_min_distance(const std::vector<double>& verts_a, const std::vector<double>& verts_b) {
    py::gil_scoped_release release;
    return calculate_min_distance(verts_a, verts_b);
}

std::pair<double, double> py_calculate_coverage_stats(
    const std::vector<double>& source_verts,
    const std::vector<double>& target_verts,
    double tolerance)
{
    py::gil_scoped_release release;
    return calculate_coverage_stats(source_verts, target_verts, tolerance);
}

// ============================================================================
// 2. АНАЛИЗ HP И КОЛЛИЗИЙ
// ============================================================================

bool check_mesh_collision(const std::vector<double>& verts_a, const std::vector<double>& verts_b, double threshold) {
    if (verts_a.empty() || verts_b.empty() || verts_a.size() % 3 != 0 || verts_b.size() % 3 != 0 ||
            !std::isfinite(threshold) || threshold <= 0.0) return false;

    const double cell_size = threshold;
    const double threshold_sq = threshold * threshold;
    std::unordered_map<std::tuple<long long, long long, long long>, std::vector<size_t>, PosHash> grid;
    grid.reserve((verts_a.size() / 3) * 2);

    for (size_t index = 0; index < verts_a.size() / 3; ++index) {
        const double x = verts_a[index * 3];
        const double y = verts_a[index * 3 + 1];
        const double z = verts_a[index * 3 + 2];
        if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) continue;
        const long long gx = static_cast<long long>(std::floor(x / cell_size));
        const long long gy = static_cast<long long>(std::floor(y / cell_size));
        const long long gz = static_cast<long long>(std::floor(z / cell_size));
        grid[{gx, gy, gz}].push_back(index);
    }

    for (size_t index = 0; index < verts_b.size() / 3; ++index) {
        const double x = verts_b[index * 3];
        const double y = verts_b[index * 3 + 1];
        const double z = verts_b[index * 3 + 2];
        if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) continue;
        const long long gx = static_cast<long long>(std::floor(x / cell_size));
        const long long gy = static_cast<long long>(std::floor(y / cell_size));
        const long long gz = static_cast<long long>(std::floor(z / cell_size));

        for (long long dx = -1; dx <= 1; ++dx) {
            for (long long dy = -1; dy <= 1; ++dy) {
                for (long long dz = -1; dz <= 1; ++dz) {
                    const auto found = grid.find({gx + dx, gy + dy, gz + dz});
                    if (found == grid.end()) continue;
                    for (size_t candidate : found->second) {
                        const double ddx = x - verts_a[candidate * 3];
                        const double ddy = y - verts_a[candidate * 3 + 1];
                        const double ddz = z - verts_a[candidate * 3 + 2];
                        if (ddx * ddx + ddy * ddy + ddz * ddz <= threshold_sq) return true;
                    }
                }
            }
        }
    }
    return false;
}

bool py_check_mesh_collision(const std::vector<double>& verts_a, const std::vector<double>& verts_b, double threshold) {
    py::gil_scoped_release release;
    return check_mesh_collision(verts_a, verts_b, threshold);
}

bool are_symmetric_axis(
    const std::vector<double>& verts_a,
    const std::vector<double>& verts_b,
    int axis,
    double tolerance,
    double min_match_ratio)
{
    size_t count_a = verts_a.size() / 3;
    size_t count_b = verts_b.size() / 3;
    if (count_a == 0 || count_a != count_b) return false;

    double center_a = 0.0;
    double center_b = 0.0;
    for (size_t i = 0; i < count_a; ++i) {
        center_a += verts_a[i * 3 + axis];
        center_b += verts_b[i * 3 + axis];
    }
    center_a /= static_cast<double>(count_a);
    center_b /= static_cast<double>(count_b);

    const double mirror_plane = (center_a + center_b) * 0.5;
    const double cell_size = std::max(tolerance, 1e-9);
    const double tolerance_sq = tolerance * tolerance;

    std::unordered_map<std::tuple<long long, long long, long long>, std::vector<size_t>, PosHash> grid;
    grid.reserve(count_b * 2);

    for (size_t i = 0; i < count_b; ++i) {
        const double x = verts_b[i * 3];
        const double y = verts_b[i * 3 + 1];
        const double z = verts_b[i * 3 + 2];
        if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) continue;
        const long long gx = static_cast<long long>(std::floor(x / cell_size));
        const long long gy = static_cast<long long>(std::floor(y / cell_size));
        const long long gz = static_cast<long long>(std::floor(z / cell_size));
        grid[{gx, gy, gz}].push_back(i);
    }

    std::vector<unsigned char> used(count_b, 0);
    size_t matched = 0;

    for (size_t i = 0; i < count_a; ++i) {
        double p[3] = {verts_a[i * 3], verts_a[i * 3 + 1], verts_a[i * 3 + 2]};
        if (!std::isfinite(p[0]) || !std::isfinite(p[1]) || !std::isfinite(p[2])) continue;
        p[axis] = mirror_plane * 2.0 - p[axis];

        const long long gx = static_cast<long long>(std::floor(p[0] / cell_size));
        const long long gy = static_cast<long long>(std::floor(p[1] / cell_size));
        const long long gz = static_cast<long long>(std::floor(p[2] / cell_size));

        bool found = false;
        for (int dx = -1; dx <= 1 && !found; ++dx) {
            for (int dy = -1; dy <= 1 && !found; ++dy) {
                for (int dz = -1; dz <= 1 && !found; ++dz) {
                    auto it = grid.find({gx + dx, gy + dy, gz + dz});
                    if (it == grid.end()) continue;
                    for (size_t idx : it->second) {
                        if (used[idx]) continue;
                        const double bx = verts_b[idx * 3];
                        const double by = verts_b[idx * 3 + 1];
                        const double bz = verts_b[idx * 3 + 2];
                        const double ddx = p[0] - bx;
                        const double ddy = p[1] - by;
                        const double ddz = p[2] - bz;
                        if ((ddx * ddx + ddy * ddy + ddz * ddz) <= tolerance_sq) {
                            used[idx] = 1;
                            ++matched;
                            found = true;
                            break;
                        }
                    }
                }
            }
        }
    }

    return (static_cast<double>(matched) / static_cast<double>(count_a)) >= min_match_ratio;
}

bool are_symmetric(const std::vector<double>& verts_a, const std::vector<double>& verts_b, double tolerance) {
    if (verts_a.size() < 9 || verts_b.size() < 9) return false;
    if ((verts_a.size() % 3) != 0 || (verts_b.size() % 3) != 0) return false;
    if ((verts_a.size() / 3) != (verts_b.size() / 3)) return false;

    const double safe_tolerance = std::max(tolerance, 1e-9);
    const double min_match_ratio = 0.85;
    for (int axis = 0; axis < 3; ++axis) {
        if (are_symmetric_axis(verts_a, verts_b, axis, safe_tolerance, min_match_ratio)) {
            return true;
        }
    }
    return false;
}

bool py_are_symmetric(const std::vector<double>& verts_a, const std::vector<double>& verts_b, double tolerance) {
    py::gil_scoped_release release;
    return are_symmetric(verts_a, verts_b, tolerance);
}

std::string generate_fingerprint_data(const std::vector<double>& verts, const std::vector<double>& center) {
    if (verts.size() < 3 || center.size() < 3 || verts.size() % 3 != 0) return "empty";

    const double cx = center[0];
    const double cy = center[1];
    const double cz = center[2];

    size_t num_verts = verts.size() / 3;
    std::vector<double> distances;
    distances.reserve(num_verts);

    for (size_t i = 0; i < num_verts; ++i) {
        const double dx = verts[i * 3] - cx;
        const double dy = verts[i * 3 + 1] - cy;
        const double dz = verts[i * 3 + 2] - cz;
        distances.push_back(std::sqrt(dx * dx + dy * dy + dz * dz));
    }

    std::sort(distances.begin(), distances.end());

    std::ostringstream oss;
    oss << "v" << num_verts;

    if (num_verts > 0) {
        const int num_samples = 20;
        for (int i = 0; i <= num_samples; ++i) {
            size_t idx = (i * (num_verts - 1)) / num_samples;
            oss << "_" << std::fixed << std::setprecision(4) << distances[idx];
        }
    }

    return oss.str();
}

std::string py_generate_fingerprint_data(const std::vector<double>& verts, const std::vector<double>& center) {
    py::gil_scoped_release release;
    return generate_fingerprint_data(verts, center);
}

int resolve_hp_collision(const std::vector<double>& hp_verts, const std::vector<std::vector<double>>& lp_candidates_verts) {
    size_t hp_count = hp_verts.size() / 3;
    if (hp_count == 0 || lp_candidates_verts.empty()) return 0;

    const size_t sample_step = std::max<size_t>(1, (hp_count + 119) / 120);

    int best_idx = -1;
    double min_total_distance = std::numeric_limits<double>::max();
    size_t num_candidates = lp_candidates_verts.size();

    for (size_t c = 0; c < num_candidates; ++c) {
        const auto& lp_verts = lp_candidates_verts[c];
        size_t lp_count = lp_verts.size() / 3;
        if (lp_count == 0) continue;

        double current_candidate_distance = 0.0;
        size_t samples_checked = 0;

        const size_t lp_step = std::max<size_t>(1, (lp_count + 249) / 250);

        for (size_t i = 0; i < hp_count; i += sample_step) {
            const double h_x = hp_verts[i * 3];
            const double h_y = hp_verts[i * 3 + 1];
            const double h_z = hp_verts[i * 3 + 2];

            double min_v_dist = std::numeric_limits<double>::max();

            for (size_t j = 0; j < lp_count; j += lp_step) {
                const double dx = h_x - lp_verts[j * 3];
                const double dy = h_y - lp_verts[j * 3 + 1];
                const double dz = h_z - lp_verts[j * 3 + 2];
                const double d2 = dx * dx + dy * dy + dz * dz;
                if (d2 < min_v_dist) {
                    min_v_dist = d2;
                }
            }
            current_candidate_distance += std::sqrt(min_v_dist);
            samples_checked++;
        }

        if (samples_checked > 0) {
            current_candidate_distance /= samples_checked;
        }

        if (current_candidate_distance < min_total_distance) {
            min_total_distance = current_candidate_distance;
            best_idx = static_cast<int>(c);
        }
    }

    return best_idx;
}

int py_resolve_hp_collision(const std::vector<double>& hp_verts, const std::vector<std::vector<double>>& lp_candidates_verts) {
    py::gil_scoped_release release;
    return resolve_hp_collision(hp_verts, lp_candidates_verts);
}


// ============================================================================
// 3. ФУНКЦИИ АНАЛИЗА ФОРМЫ (PCA + Центроиды)
// ============================================================================

std::vector<std::tuple<int, int, double, double, int, double>> calculate_vertex_owner_scores(
    const std::vector<std::vector<double>>& lp_point_sets,
    const std::vector<std::vector<double>>& hp_point_sets,
    const std::vector<std::pair<int, int>>& candidate_pairs)
{
    const size_t lp_count = lp_point_sets.size();
    const size_t hp_count = hp_point_sets.size();
    std::vector<std::vector<int>> hp_candidates_by_lp(lp_count);
    std::vector<std::vector<int>> lp_candidates_by_hp(hp_count);

    for (const auto& pair : candidate_pairs) {
        int lp_idx = pair.first;
        int hp_idx = pair.second;
        if (lp_idx < 0 || hp_idx < 0) continue;
        if (static_cast<size_t>(lp_idx) >= lp_count || static_cast<size_t>(hp_idx) >= hp_count) continue;
        auto& hp_candidates = hp_candidates_by_lp[static_cast<size_t>(lp_idx)];
        if (std::find(hp_candidates.begin(), hp_candidates.end(), hp_idx) == hp_candidates.end()) hp_candidates.push_back(hp_idx);
        auto& lp_candidates = lp_candidates_by_hp[static_cast<size_t>(hp_idx)];
        if (std::find(lp_candidates.begin(), lp_candidates.end(), lp_idx) == lp_candidates.end()) lp_candidates.push_back(lp_idx);
    }

    std::vector<std::unique_ptr<KDTree>> lp_trees(lp_count);
    std::vector<std::unique_ptr<KDTree>> hp_trees(hp_count);
    for (size_t index = 0; index < lp_count; ++index) {
        if (!lp_candidates_by_hp.empty() && !hp_candidates_by_lp[index].empty()) {
            lp_trees[index].reset(new KDTree(lp_point_sets[index]));
        }
    }
    for (size_t index = 0; index < hp_count; ++index) {
        if (!lp_candidates_by_hp[index].empty()) hp_trees[index].reset(new KDTree(hp_point_sets[index]));
    }

    std::vector<std::unordered_map<int, double>> lp_claim(lp_count);
    std::vector<std::unordered_map<int, double>> hp_owner_by_hp(hp_count);

    const unsigned int lp_threads = adaptive_thread_count(lp_count, lp_count * 1000);
    auto lp_worker = [&](unsigned int thread_id) {
        const size_t start = (lp_count * thread_id) / lp_threads;
        const size_t end = (lp_count * (thread_id + 1)) / lp_threads;
        for (size_t lp_idx = start; lp_idx < end; ++lp_idx) {
            const auto& lp_points = lp_point_sets[lp_idx];
            const auto& candidates = hp_candidates_by_lp[lp_idx];
            size_t point_count = lp_points.size() / 3;
            if (point_count == 0 || candidates.empty()) continue;

            std::unordered_map<int, size_t> counts;
            for (int hp_idx : candidates) counts[hp_idx] = 0;
            for (size_t p = 0; p < point_count; ++p) {
                const double px = lp_points[p * 3];
                const double py = lp_points[p * 3 + 1];
                const double pz = lp_points[p * 3 + 2];
                int best_hp = -1;
                double best_dist = std::numeric_limits<double>::max();
                for (int hp_idx : candidates) {
                    const KDTree* tree = hp_trees[static_cast<size_t>(hp_idx)].get();
                    if (!tree || tree->empty()) continue;
                    const double distance = tree->nearest_sq(px, py, pz);
                    if (distance < best_dist) {
                        best_dist = distance;
                        best_hp = hp_idx;
                    }
                }
                if (best_hp >= 0) counts[best_hp]++;
            }
            for (int hp_idx : candidates) {
                lp_claim[lp_idx][hp_idx] = (static_cast<double>(counts[hp_idx]) / static_cast<double>(point_count)) * 100.0;
            }
        }
    };

    if (lp_threads == 1) lp_worker(0);
    else {
        std::vector<std::thread> threads;
        threads.reserve(lp_threads);
        for (unsigned int index = 0; index < lp_threads; ++index) threads.emplace_back(lp_worker, index);
        for (auto& thread : threads) thread.join();
    }

    const unsigned int hp_threads = adaptive_thread_count(hp_count, hp_count * 1000);
    auto hp_worker = [&](unsigned int thread_id) {
        const size_t start = (hp_count * thread_id) / hp_threads;
        const size_t end = (hp_count * (thread_id + 1)) / hp_threads;
        for (size_t hp_idx = start; hp_idx < end; ++hp_idx) {
            const auto& hp_points = hp_point_sets[hp_idx];
            const auto& candidates = lp_candidates_by_hp[hp_idx];
            size_t point_count = hp_points.size() / 3;
            if (point_count == 0 || candidates.empty()) continue;

            std::unordered_map<int, size_t> counts;
            for (int lp_idx : candidates) counts[lp_idx] = 0;
            for (size_t p = 0; p < point_count; ++p) {
                const double px = hp_points[p * 3];
                const double py = hp_points[p * 3 + 1];
                const double pz = hp_points[p * 3 + 2];
                int best_lp = -1;
                double best_dist = std::numeric_limits<double>::max();
                for (int lp_idx : candidates) {
                    const KDTree* tree = lp_trees[static_cast<size_t>(lp_idx)].get();
                    if (!tree || tree->empty()) continue;
                    const double distance = tree->nearest_sq(px, py, pz);
                    if (distance < best_dist) {
                        best_dist = distance;
                        best_lp = lp_idx;
                    }
                }
                if (best_lp >= 0) counts[best_lp]++;
            }
            for (int lp_idx : candidates) {
                hp_owner_by_hp[hp_idx][lp_idx] = (static_cast<double>(counts[lp_idx]) / static_cast<double>(point_count)) * 100.0;
            }
        }
    };

    if (hp_threads == 1) hp_worker(0);
    else {
        std::vector<std::thread> threads;
        threads.reserve(hp_threads);
        for (unsigned int index = 0; index < hp_threads; ++index) threads.emplace_back(hp_worker, index);
        for (auto& thread : threads) thread.join();
    }

    std::vector<int> owner_lp_by_hp(hp_count, -1);
    std::vector<double> owner_pct_by_hp(hp_count, 0.0);
    for (size_t hp_idx = 0; hp_idx < hp_count; ++hp_idx) {
        for (const auto& item : hp_owner_by_hp[hp_idx]) {
            if (owner_lp_by_hp[hp_idx] < 0 || item.second > owner_pct_by_hp[hp_idx]) {
                owner_lp_by_hp[hp_idx] = item.first;
                owner_pct_by_hp[hp_idx] = item.second;
            }
        }
    }

    std::vector<std::tuple<int, int, double, double, int, double>> result;
    result.reserve(candidate_pairs.size());
    for (const auto& pair : candidate_pairs) {
        int lp_idx = pair.first;
        int hp_idx = pair.second;
        if (lp_idx < 0 || hp_idx < 0) continue;
        if (static_cast<size_t>(lp_idx) >= lp_count || static_cast<size_t>(hp_idx) >= hp_count) continue;
        const auto claim = lp_claim[static_cast<size_t>(lp_idx)].find(hp_idx);
        const auto owner = hp_owner_by_hp[static_cast<size_t>(hp_idx)].find(lp_idx);
        result.emplace_back(
            lp_idx,
            hp_idx,
            claim == lp_claim[static_cast<size_t>(lp_idx)].end() ? 0.0 : claim->second,
            owner == hp_owner_by_hp[static_cast<size_t>(hp_idx)].end() ? 0.0 : owner->second,
            owner_lp_by_hp[static_cast<size_t>(hp_idx)],
            owner_pct_by_hp[static_cast<size_t>(hp_idx)]);
    }
    return result;
}

std::vector<std::tuple<int, int, double, double, int, double>> py_calculate_vertex_owner_scores(
    const std::vector<std::vector<double>>& lp_point_sets,
    const std::vector<std::vector<double>>& hp_point_sets,
    const std::vector<std::pair<int, int>>& candidate_pairs)
{
    py::gil_scoped_release release;
    return calculate_vertex_owner_scores(lp_point_sets, hp_point_sets, candidate_pairs);
}

static std::array<double, 3> symmetric_eigenvalues(double matrix[3][3]) {
    for (int iteration = 0; iteration < 18; ++iteration) {
        int p = 0;
        int q = 1;
        double largest = std::abs(matrix[0][1]);
        if (std::abs(matrix[0][2]) > largest) { p = 0; q = 2; largest = std::abs(matrix[0][2]); }
        if (std::abs(matrix[1][2]) > largest) { p = 1; q = 2; largest = std::abs(matrix[1][2]); }
        if (largest < 1e-15) break;

        const double angle = 0.5 * std::atan2(2.0 * matrix[p][q], matrix[q][q] - matrix[p][p]);
        const double cosine = std::cos(angle);
        const double sine = std::sin(angle);
        const double app = cosine * cosine * matrix[p][p] - 2.0 * sine * cosine * matrix[p][q] + sine * sine * matrix[q][q];
        const double aqq = sine * sine * matrix[p][p] + 2.0 * sine * cosine * matrix[p][q] + cosine * cosine * matrix[q][q];
        for (int index = 0; index < 3; ++index) {
            if (index == p || index == q) continue;
            const double aip = cosine * matrix[index][p] - sine * matrix[index][q];
            const double aiq = sine * matrix[index][p] + cosine * matrix[index][q];
            matrix[index][p] = matrix[p][index] = aip;
            matrix[index][q] = matrix[q][index] = aiq;
        }
        matrix[p][p] = app;
        matrix[q][q] = aqq;
        matrix[p][q] = matrix[q][p] = 0.0;
    }
    std::array<double, 3> values = {
        std::max(matrix[0][0], 0.0),
        std::max(matrix[1][1], 0.0),
        std::max(matrix[2][2], 0.0)
    };
    std::sort(values.begin(), values.end(), std::greater<double>());
    return values;
}

MeshMetrics analyze_mesh_shape(const std::vector<double>& verts) {
    MeshMetrics m;
    const size_t n = verts.size() / 3;
    if (n < 3 || verts.size() % 3 != 0) return m;

    double cx = 0.0, cy = 0.0, cz = 0.0;
    double min_x = verts[0], max_x = verts[0];
    double min_y = verts[1], max_y = verts[1];
    double min_z = verts[2], max_z = verts[2];

    for (size_t i = 0; i < verts.size(); i += 3) {
        cx += verts[i]; cy += verts[i+1]; cz += verts[i+2];
        min_x = std::min(min_x, verts[i]); max_x = std::max(max_x, verts[i]);
        min_y = std::min(min_y, verts[i+1]); max_y = std::max(max_y, verts[i+1]);
        min_z = std::min(min_z, verts[i+2]); max_z = std::max(max_z, verts[i+2]);
    }
    cx /= static_cast<double>(n); cy /= static_cast<double>(n); cz /= static_cast<double>(n);

    const double geom_center_x = (min_x + max_x) * 0.5;
    const double geom_center_y = (min_y + max_y) * 0.5;
    const double geom_center_z = (min_z + max_z) * 0.5;

    m.symmetry_score = std::sqrt(std::pow(cx - geom_center_x, 2) +
                                 std::pow(cy - geom_center_y, 2) +
                                 std::pow(cz - geom_center_z, 2));

    m.center = {geom_center_x, geom_center_y, geom_center_z};

    double covariance[3][3] = {{0.0, 0.0, 0.0}, {0.0, 0.0, 0.0}, {0.0, 0.0, 0.0}};
    for (size_t i = 0; i < verts.size(); i += 3) {
        const double dx = verts[i] - cx;
        const double dy = verts[i + 1] - cy;
        const double dz = verts[i + 2] - cz;
        covariance[0][0] += dx * dx;
        covariance[0][1] += dx * dy;
        covariance[0][2] += dx * dz;
        covariance[1][1] += dy * dy;
        covariance[1][2] += dy * dz;
        covariance[2][2] += dz * dz;
    }
    covariance[1][0] = covariance[0][1];
    covariance[2][0] = covariance[0][2];
    covariance[2][1] = covariance[1][2];
    const double inverse_count = 1.0 / static_cast<double>(n);
    for (int row = 0; row < 3; ++row) {
        for (int column = 0; column < 3; ++column) covariance[row][column] *= inverse_count;
    }

    const std::array<double, 3> eigenvalues = symmetric_eigenvalues(covariance);
    const double reference = std::max(eigenvalues[0], 1e-18);
    const double secondary = std::max(eigenvalues[1], reference * 1e-12);
    m.elongation = std::sqrt(reference / secondary);
    m.dimensions = {
        std::sqrt(eigenvalues[0]),
        std::sqrt(eigenvalues[1]),
        std::sqrt(eigenvalues[2])
    };

    return m;
}

MeshMetrics py_analyze_mesh_shape(const std::vector<double>& verts) {
    py::gil_scoped_release release;
    return analyze_mesh_shape(verts);
}


// Bounded proxy surface queries. Double precision preserves small differences
// on assets placed far from the world origin.
using SurfacePoint = std::array<double, 3>;

static SurfacePoint surface_sub(const SurfacePoint& a, const SurfacePoint& b) {
    return {{a[0] - b[0], a[1] - b[1], a[2] - b[2]}};
}

static double surface_dot(const SurfacePoint& a, const SurfacePoint& b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

static double point_segment_distance_sq(const SurfacePoint& p, const SurfacePoint& a,
                                        const SurfacePoint& b) {
    const SurfacePoint ab = surface_sub(b, a);
    const SurfacePoint ap = surface_sub(p, a);
    const double length_sq = surface_dot(ab, ab);
    const double t = length_sq > 0.0 ? std::max(0.0, std::min(1.0, surface_dot(ap, ab) / length_sq)) : 0.0;
    const SurfacePoint offset = {{ap[0] - t * ab[0], ap[1] - t * ab[1], ap[2] - t * ab[2]}};
    return surface_dot(offset, offset);
}

static double point_triangle_distance_sq(const SurfacePoint& p, const SurfacePoint& a,
                                         const SurfacePoint& b, const SurfacePoint& c) {
    const SurfacePoint ab = surface_sub(b, a);
    const SurfacePoint ac = surface_sub(c, a);
    const SurfacePoint ap = surface_sub(p, a);
    const SurfacePoint cross = {{ab[1] * ac[2] - ab[2] * ac[1],
                                ab[2] * ac[0] - ab[0] * ac[2],
                                ab[0] * ac[1] - ab[1] * ac[0]}};
    const double edge_scale = std::max(surface_dot(ab, ab), surface_dot(ac, ac));
    if (surface_dot(cross, cross) <= edge_scale * edge_scale * 1e-24) {
        return std::min(point_segment_distance_sq(p, a, b),
                        std::min(point_segment_distance_sq(p, a, c), point_segment_distance_sq(p, b, c)));
    }
    const double d1 = surface_dot(ab, ap), d2 = surface_dot(ac, ap);
    if (d1 <= 0.0 && d2 <= 0.0) return surface_dot(ap, ap);
    const SurfacePoint bp = surface_sub(p, b);
    const double d3 = surface_dot(ab, bp), d4 = surface_dot(ac, bp);
    if (d3 >= 0.0 && d4 <= d3) return surface_dot(bp, bp);
    const double vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0) return point_segment_distance_sq(p, a, b);
    const SurfacePoint cp = surface_sub(p, c);
    const double d5 = surface_dot(ab, cp), d6 = surface_dot(ac, cp);
    if (d6 >= 0.0 && d5 <= d6) return surface_dot(cp, cp);
    const double vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0) return point_segment_distance_sq(p, a, c);
    const double va = d3 * d6 - d5 * d4;
    if (va <= 0.0 && d4 >= d3 && d5 >= d6) return point_segment_distance_sq(p, b, c);
    const double denom = va + vb + vc;
    const double v = vb / denom, w = vc / denom;
    const SurfacePoint offset = {{ap[0] - v * ab[0] - w * ac[0],
                                  ap[1] - v * ab[1] - w * ac[1],
                                  ap[2] - v * ab[2] - w * ac[2]}};
    return std::max(0.0, surface_dot(offset, offset));
}

struct SurfaceTriangle {
    SurfacePoint a, b, c, minimum, maximum;
};

static std::pair<double, double> calculate_surface_direction(
        const std::vector<double>& samples, const std::vector<double>& triangles,
        double tolerance) {
    const size_t sample_count = samples.size() / 3;
    const size_t triangle_count = triangles.size() / 9;
    const auto invalid = std::make_pair(std::numeric_limits<double>::infinity(), 0.0);
    if (sample_count == 0 || triangle_count == 0 || samples.size() % 3 != 0 ||
            triangles.size() % 9 != 0 || !std::isfinite(tolerance) || tolerance < 0.0) return invalid;
    for (double value : samples) if (!std::isfinite(value)) return invalid;
    for (double value : triangles) if (!std::isfinite(value)) return invalid;

    std::vector<SurfaceTriangle> records;
    records.reserve(triangle_count);
    for (size_t index = 0; index < triangle_count; ++index) {
        const size_t base = index * 9;
        SurfaceTriangle triangle;
        for (size_t axis = 0; axis < 3; ++axis) {
            triangle.a[axis] = triangles[base + axis];
            triangle.b[axis] = triangles[base + 3 + axis];
            triangle.c[axis] = triangles[base + 6 + axis];
            triangle.minimum[axis] = std::min(triangle.a[axis], std::min(triangle.b[axis], triangle.c[axis]));
            triangle.maximum[axis] = std::max(triangle.a[axis], std::max(triangle.b[axis], triangle.c[axis]));
        }
        records.push_back(triangle);
    }
    const unsigned int count = adaptive_thread_count(sample_count, sample_count * triangle_count);
    std::vector<double> sums(count, 0.0);
    std::vector<size_t> hits(count, 0);
    auto evaluate = [&](unsigned int thread_id) {
        const size_t start = sample_count * thread_id / count;
        const size_t end = sample_count * (thread_id + 1) / count;
        for (size_t index = start; index < end; ++index) {
            const SurfacePoint point = {{samples[index * 3], samples[index * 3 + 1], samples[index * 3 + 2]}};
            double best_sq = std::numeric_limits<double>::infinity();
            for (const auto& triangle : records) {
                double lower_sq = 0.0;
                for (size_t axis = 0; axis < 3; ++axis) {
                    const double gap = std::max(0.0, std::max(triangle.minimum[axis] - point[axis], point[axis] - triangle.maximum[axis]));
                    lower_sq += gap * gap;
                }
                if (lower_sq >= best_sq) continue;
                best_sq = std::min(best_sq, point_triangle_distance_sq(point, triangle.a, triangle.b, triangle.c));
                if (best_sq == 0.0) break;
            }
            sums[thread_id] += std::sqrt(best_sq);
            if (best_sq <= tolerance * tolerance) ++hits[thread_id];
        }
    };
    if (count == 1) {
        evaluate(0);
    } else {
        std::vector<std::thread> threads;
        for (unsigned int index = 0; index < count; ++index) threads.emplace_back(evaluate, index);
        for (auto& thread : threads) thread.join();
    }
    return {std::accumulate(sums.begin(), sums.end(), 0.0) / static_cast<double>(sample_count),
            static_cast<double>(std::accumulate(hits.begin(), hits.end(), static_cast<size_t>(0))) / static_cast<double>(sample_count)};
}

py::dict py_calculate_surface_match(
        const std::vector<double>& hp_samples, const std::vector<double>& hp_triangles,
        const std::vector<double>& lp_samples, const std::vector<double>& lp_triangles,
        double tolerance) {
    std::pair<double, double> hp_to_lp, lp_to_hp;
    {
        py::gil_scoped_release release;
        hp_to_lp = calculate_surface_direction(hp_samples, lp_triangles, tolerance);
        lp_to_hp = calculate_surface_direction(lp_samples, hp_triangles, tolerance);
    }
    py::dict result;
    result["hp_to_lp_distance"] = hp_to_lp.first;
    result["lp_to_hp_distance"] = lp_to_hp.first;
    result["hp_coverage"] = hp_to_lp.second;
    result["lp_coverage"] = lp_to_hp.second;
    result["average_distance"] = (hp_to_lp.first + lp_to_hp.first) * 0.5;
    result["coverage"] = std::min(hp_to_lp.second, lp_to_hp.second);
    return result;
}


// Rotation- and scale-invariant shape metrics for Analyze HP. The legacy
// function above remains available for compatibility with older callers.
static void jacobi_eigen_3x3(double matrix[3][3], double vectors[3][3]) {
    for (int row = 0; row < 3; ++row) {
        for (int col = 0; col < 3; ++col) {
            vectors[row][col] = row == col ? 1.0 : 0.0;
        }
    }

    for (int iteration = 0; iteration < 24; ++iteration) {
        int p = 0;
        int q = 1;
        double largest = std::fabs(matrix[0][1]);
        if (std::fabs(matrix[0][2]) > largest) {
            p = 0;
            q = 2;
            largest = std::fabs(matrix[0][2]);
        }
        if (std::fabs(matrix[1][2]) > largest) {
            p = 1;
            q = 2;
            largest = std::fabs(matrix[1][2]);
        }
        const double matrix_scale = std::max(std::fabs(matrix[0][0]),
            std::max(std::fabs(matrix[1][1]), std::fabs(matrix[2][2])));
        if (largest <= std::max(matrix_scale * 1e-14, 1e-300)) break;

        const double angle = 0.5 * std::atan2(
            2.0 * matrix[p][q], matrix[q][q] - matrix[p][p]);
        const double c = std::cos(angle);
        const double s = std::sin(angle);
        const double app = matrix[p][p];
        const double aqq = matrix[q][q];
        const double apq = matrix[p][q];

        matrix[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq;
        matrix[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq;
        matrix[p][q] = matrix[q][p] = 0.0;

        for (int axis = 0; axis < 3; ++axis) {
            if (axis == p || axis == q) continue;
            const double aip = matrix[axis][p];
            const double aiq = matrix[axis][q];
            matrix[axis][p] = matrix[p][axis] = c * aip - s * aiq;
            matrix[axis][q] = matrix[q][axis] = s * aip + c * aiq;
        }
        for (int row = 0; row < 3; ++row) {
            const double vip = vectors[row][p];
            const double viq = vectors[row][q];
            vectors[row][p] = c * vip - s * viq;
            vectors[row][q] = s * vip + c * viq;
        }
    }
}

MeshMetrics analyze_mesh_shape_v2(const std::vector<double>& verts) {
    MeshMetrics metrics;
    metrics.elongation = 1.0;
    metrics.symmetry_score = 0.0;
    metrics.dimensions = {0.0, 0.0, 0.0};
    metrics.center = {0.0, 0.0, 0.0};

    const size_t point_count = verts.size() / 3;
    if (point_count < 3 || verts.size() % 3 != 0) return metrics;
    for (double value : verts) {
        if (!std::isfinite(value)) throw std::invalid_argument("Shape metrics require finite XYZ points");
    }

    double center[3] = {0.0, 0.0, 0.0};
    for (size_t point = 0; point < point_count; ++point) {
        center[0] += static_cast<double>(verts[point * 3]);
        center[1] += static_cast<double>(verts[point * 3 + 1]);
        center[2] += static_cast<double>(verts[point * 3 + 2]);
    }
    for (int axis = 0; axis < 3; ++axis) {
        center[axis] /= static_cast<double>(point_count);
    }

    double covariance[3][3] = {{0.0, 0.0, 0.0}, {0.0, 0.0, 0.0}, {0.0, 0.0, 0.0}};
    for (size_t point = 0; point < point_count; ++point) {
        const double value[3] = {
            static_cast<double>(verts[point * 3]) - center[0],
            static_cast<double>(verts[point * 3 + 1]) - center[1],
            static_cast<double>(verts[point * 3 + 2]) - center[2]
        };
        for (int row = 0; row < 3; ++row) {
            for (int col = row; col < 3; ++col) {
                covariance[row][col] += value[row] * value[col];
            }
        }
    }
    const double inv_count = 1.0 / static_cast<double>(point_count);
    for (int row = 0; row < 3; ++row) {
        for (int col = row; col < 3; ++col) {
            covariance[row][col] *= inv_count;
            covariance[col][row] = covariance[row][col];
        }
    }

    double eigenvectors[3][3];
    jacobi_eigen_3x3(covariance, eigenvectors);
    int order[3] = {0, 1, 2};
    std::sort(order, order + 3, [&](int left, int right) {
        return covariance[left][left] > covariance[right][right];
    });

    double min_projection[3] = {
        std::numeric_limits<double>::max(),
        std::numeric_limits<double>::max(),
        std::numeric_limits<double>::max()
    };
    double max_projection[3] = {
        -std::numeric_limits<double>::max(),
        -std::numeric_limits<double>::max(),
        -std::numeric_limits<double>::max()
    };
    for (size_t point = 0; point < point_count; ++point) {
        const double value[3] = {
            static_cast<double>(verts[point * 3]) - center[0],
            static_cast<double>(verts[point * 3 + 1]) - center[1],
            static_cast<double>(verts[point * 3 + 2]) - center[2]
        };
        for (int rank = 0; rank < 3; ++rank) {
            const int axis = order[rank];
            const double projection =
                value[0] * eigenvectors[0][axis] +
                value[1] * eigenvectors[1][axis] +
                value[2] * eigenvectors[2][axis];
            min_projection[rank] = std::min(min_projection[rank], projection);
            max_projection[rank] = std::max(max_projection[rank], projection);
        }
    }

    double extent_diag_sq = 0.0;
    double midpoint_offset_sq = 0.0;
    for (int rank = 0; rank < 3; ++rank) {
        const double extent = std::max(max_projection[rank] - min_projection[rank], 0.0);
        const double midpoint = (max_projection[rank] + min_projection[rank]) * 0.5;
        metrics.dimensions[rank] = static_cast<double>(extent);
        extent_diag_sq += extent * extent;
        midpoint_offset_sq += midpoint * midpoint;
    }

    const double largest_variance = std::max(covariance[order[0]][order[0]], 0.0);
    // Preserve Bake Master's existing longest/secondary-axis convention:
    // flat plates should not turn into wires solely because of zero thickness.
    const double secondary_variance = std::max(covariance[order[1]][order[1]], 0.0);
    const double variance_floor = std::max(largest_variance * 1e-12, 1e-300);
    metrics.elongation = static_cast<double>(
        std::sqrt(largest_variance / std::max(secondary_variance, variance_floor)));

    // Keep the existing 0.8 UI threshold useful while removing scene scale.
    const double extent_diag = std::sqrt(std::max(extent_diag_sq, 1e-300));
    metrics.symmetry_score = static_cast<double>(
        4.0 * std::sqrt(midpoint_offset_sq) / extent_diag);
    metrics.center = {
        static_cast<double>(center[0]),
        static_cast<double>(center[1]),
        static_cast<double>(center[2])
    };
    return metrics;
}


MeshMetrics py_analyze_mesh_shape_v2(const std::vector<double>& verts) {
    py::gil_scoped_release release;
    return analyze_mesh_shape_v2(verts);
}


// ============================================================================
// 4. РЕГИСТРАЦИЯ МОДУЛЯ ДЛЯ PYTHON
// ============================================================================
PYBIND11_MODULE(bg_math_core, m) {
    m.doc() = "Optimized High-performance C++ math utilities for Bake Master";

    m.def(
        "calculate_bidirectional_avg_distance",
        &py_calculate_bidirectional_avg_distance,
        "Symmetric average nearest-neighbor distance"
    );

    py::class_<MeshMetrics>(m, "MeshMetrics")
        .def_readonly("elongation", &MeshMetrics::elongation)
        .def_readonly("symmetry_score", &MeshMetrics::symmetry_score)
        .def_readonly("dimensions", &MeshMetrics::dimensions)
        .def_readonly("center", &MeshMetrics::center);

    py::class_<PointCloudIndex>(m, "PointCloudIndex")
        .def(
            py::init<const std::vector<double>&>(),
            py::call_guard<py::gil_scoped_release>(),
            py::arg("target_vertices")
        )
        .def(
            "average_distance",
            &PointCloudIndex::average_distance,
            py::call_guard<py::gil_scoped_release>(),
            py::arg("source_vertices"),
            "Calculate average source-to-target distance using the cached KD-tree"
        )
        .def_property_readonly(
            "target_point_count",
            &PointCloudIndex::target_point_count
        );

    m.def("calculate_avg_distance", &py_calculate_avg_distance, "Calculate average distance between two vertex clouds (Multi-threaded)");

    // Новая функция вынесена в Python-пространство
    m.def("calculate_min_distance", &py_calculate_min_distance, "Calculate absolute minimum distance between two vertex clouds (Multi-threaded)");

    m.def(
        "calculate_coverage_stats",
        &py_calculate_coverage_stats,
        "Calculate nearest-point coverage and average distance for sampled point clouds"
    );

    m.def("check_mesh_collision", &py_check_mesh_collision, "Fast spatial hash-based collision detection between vertex clouds");
    m.def("are_symmetric", &py_are_symmetric, py::arg("verts_a"), py::arg("verts_b"), py::arg("tolerance") = 0.01f, "Check mirrored point-cloud symmetry across the best world axis");
    m.def("resolve_hp_collision", &py_resolve_hp_collision, "Resolve high-poly to low-poly candidate assignment collisions");
    m.def("calculate_vertex_owner_scores", &py_calculate_vertex_owner_scores, "Calculate LP/HP nearest-vertex ownership scores for candidate pairs");

    m.def("calculate_surface_match", &py_calculate_surface_match, "Directional point-to-triangle coverage for bounded surface proxies");
    m.def("analyze_mesh_shape_v2", &py_analyze_mesh_shape_v2, "Rotation/scale-normalized PCA shape metrics with plate-safe elongation");
    m.def("analyze_mesh_shape", &py_analyze_mesh_shape, "Analyze mesh elongation and symmetry using PCA principles");
    m.def("generate_fingerprint_data", &py_generate_fingerprint_data, "Generate a geometric string fingerprint for a mesh");
}
