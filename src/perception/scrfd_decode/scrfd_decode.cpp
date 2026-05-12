/*
 * scrfd_decode.cpp — C++ pybind11 extension for SCRFD output decoding.
 *
 * Replaces FaceDetector._decode_scrfd() in Python/numpy.  Fuses all
 * 6 anchor passes (3 strides × 2 anchors) into one tight C++ loop,
 * releasing the GIL during compute and eliminating all Python-level
 * per-cell overhead (~8 400 cells × 6 passes per frame).
 *
 * Function signature (matches the Python version exactly):
 *
 *   decode_scrfd(
 *       score_maps : list[ndarray(H,W,2)],   # one per stride, pre-sigmoid logits
 *       bbox_maps  : list[ndarray(H,W,8)],   # one per stride, LTBR × 2 anchors
 *       kps_maps   : list[ndarray|None],      # one per stride, 10 vals × 2 anchors
 *       strides    : list[int],               # [8, 16, 32]
 *       conf_thr_logit : float,               # logit(conf_threshold)
 *   ) -> tuple[ndarray(N,4), ndarray(N,), ndarray(N,10)|None]
 *       boxes  : float32 (N, 4)  x1y1x2y2
 *       scores : float32 (N,)
 *       kps    : float32 (N,10)  or None
 *
 * Build:
 *   cd src/perception/scrfd_decode && python3 setup.py build_ext --inplace
 */

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include <cmath>
#include <vector>
#include <stdexcept>

namespace py = pybind11;

struct DecodeResult {
    std::vector<float> boxes;   // x1, y1, x2, y2 interleaved
    std::vector<float> scores;
    std::vector<float> kps;     // x0,y0, x1,y1, ... x4,y4 interleaved (10 per det)
    bool has_kps = false;
};

/*
 * Process one stride level. Appends into result vectors.
 *
 * score_map : float32 (H, W, 2)   pre-sigmoid logits
 * bbox_map  : float32 (H, W, 8)
 * kps_map   : float32 (H, W, 20) or nullptr
 * stride    : int
 * conf_thr_logit : logit-space threshold (avoids per-cell sigmoid)
 */
static void decode_stride(
    const float* score_map,
    const float* bbox_map,
    const float* kps_map,
    int H, int W,
    int stride,
    float conf_thr_logit,
    DecodeResult& out
) {
    for (int a = 0; a < 2; ++a) {                      // 2 anchors per cell
        for (int r = 0; r < H; ++r) {
            for (int c = 0; c < W; ++c) {
                int cell = r * W + c;
                // score_map layout: (H, W, 2) → cell*2 + anchor
                float logit = score_map[cell * 2 + a];
                if (logit <= conf_thr_logit) continue;

                // Sigmoid only for survivors
                float score = 1.0f / (1.0f + std::exp(-logit));

                // Grid centre (pixel coords)
                float cx = (c + 0.5f) * stride;
                float cy = (r + 0.5f) * stride;

                // bbox_map layout: (H, W, 8) → (H, W, 2anchors, 4)
                // anchor a offset: cell*8 + a*4
                const float* ltbr = bbox_map + cell * 8 + a * 4;
                float x1 = cx - ltbr[0] * stride;
                float y1 = cy - ltbr[1] * stride;
                float x2 = cx + ltbr[2] * stride;
                float y2 = cy + ltbr[3] * stride;

                out.boxes.push_back(x1);
                out.boxes.push_back(y1);
                out.boxes.push_back(x2);
                out.boxes.push_back(y2);
                out.scores.push_back(score);

                if (kps_map != nullptr) {
                    // kps_map layout: (H, W, 20) → (H, W, 2anchors, 10)
                    const float* kp = kps_map + cell * 20 + a * 10;
                    for (int k = 0; k < 5; ++k) {
                        out.kps.push_back(kp[k * 2 + 0] * stride + cx);
                        out.kps.push_back(kp[k * 2 + 1] * stride + cy);
                    }
                    out.has_kps = true;
                }
            }
        }
    }
}


py::tuple decode_scrfd(
    py::list score_maps_list,
    py::list bbox_maps_list,
    py::list kps_maps_list,
    std::vector<int> strides,
    float conf_thr_logit
) {
    using arr_f = py::array_t<float, py::array::c_style | py::array::forcecast>;

    size_t n_strides = strides.size();
    if (score_maps_list.size() != n_strides ||
        bbox_maps_list.size() != n_strides ||
        kps_maps_list.size()  != n_strides) {
        throw std::runtime_error("decode_scrfd: list lengths must match strides");
    }

    DecodeResult result;

    py::gil_scoped_release release;  // release GIL during all the compute

    for (size_t i = 0; i < n_strides; ++i) {
        py::gil_scoped_acquire acquire;  // need GIL briefly to extract arrays

        auto score_np = py::cast<arr_f>(score_maps_list[i]);
        auto bbox_np  = py::cast<arr_f>(bbox_maps_list[i]);

        py::object kps_obj = kps_maps_list[i];
        bool has_kps_this = !kps_obj.is_none();
        arr_f kps_np;
        if (has_kps_this) kps_np = py::cast<arr_f>(kps_obj);

        auto score_info = score_np.request();
        auto bbox_info  = bbox_np.request();

        if (score_info.ndim != 3 || score_info.shape[2] != 2)
            throw std::runtime_error("decode_scrfd: score_map must be (H,W,2)");
        if (bbox_info.ndim != 3 || bbox_info.shape[2] != 8)
            throw std::runtime_error("decode_scrfd: bbox_map must be (H,W,8)");

        int H = (int)score_info.shape[0];
        int W = (int)score_info.shape[1];

        const float* kps_ptr = nullptr;
        if (has_kps_this) {
            auto kps_info = kps_np.request();
            if (kps_info.ndim != 3 || kps_info.shape[2] != 20)
                throw std::runtime_error("decode_scrfd: kps_map must be (H,W,20)");
            kps_ptr = static_cast<const float*>(kps_info.ptr);
        }

        py::gil_scoped_release rel2;   // release while doing heavy work
        decode_stride(
            static_cast<const float*>(score_info.ptr),
            static_cast<const float*>(bbox_info.ptr),
            kps_ptr,
            H, W, strides[i], conf_thr_logit,
            result
        );
    }

    // -- Build output arrays (need GIL for numpy allocation) --------
    py::gil_scoped_acquire acquire_final;

    size_t N = result.scores.size();
    if (N == 0) {
        std::vector<py::ssize_t> empty_box_shape  = {0, 4};
        std::vector<py::ssize_t> empty_score_shape = {0};
        auto empty_boxes  = py::array_t<float>(empty_box_shape);
        auto empty_scores = py::array_t<float>(empty_score_shape);
        return py::make_tuple(empty_boxes, empty_scores, py::none());
    }

    std::vector<py::ssize_t> box_shape   = {(py::ssize_t)N, 4};
    std::vector<py::ssize_t> score_shape = {(py::ssize_t)N};
    std::vector<py::ssize_t> kps_shape   = {(py::ssize_t)N, 10};

    auto boxes_arr  = py::array_t<float>(box_shape);
    auto scores_arr = py::array_t<float>(score_shape);

    std::copy(result.boxes.begin(),  result.boxes.end(),
              boxes_arr.mutable_unchecked<2>().mutable_data(0, 0));
    std::copy(result.scores.begin(), result.scores.end(),
              scores_arr.mutable_unchecked<1>().mutable_data(0));

    if (!result.has_kps || result.kps.empty()) {
        return py::make_tuple(boxes_arr, scores_arr, py::none());
    }

    auto kps_arr = py::array_t<float>(kps_shape);
    std::copy(result.kps.begin(), result.kps.end(),
              kps_arr.mutable_unchecked<2>().mutable_data(0, 0));

    return py::make_tuple(boxes_arr, scores_arr, kps_arr);
}


PYBIND11_MODULE(scrfd_decode_cpp, m) {
    m.doc() = "C++ SCRFD output decoder — replaces FaceDetector._decode_scrfd()";
    m.def(
        "decode_scrfd",
        &decode_scrfd,
        py::arg("score_maps"),
        py::arg("bbox_maps"),
        py::arg("kps_maps"),
        py::arg("strides"),
        py::arg("conf_thr_logit"),
        R"doc(
Decode raw SCRFD Hailo outputs into boxes, scores, and optional landmarks.

Parameters
----------
score_maps     : list of float32 ndarray (H,W,2)   — one per stride
bbox_maps      : list of float32 ndarray (H,W,8)   — one per stride
kps_maps       : list of float32 ndarray (H,W,20) or None per stride
strides        : list[int]   e.g. [8, 16, 32]
conf_thr_logit : float       logit(conf_threshold)

Returns
-------
tuple (boxes, scores, kps_or_None)
  boxes  : float32 (N, 4)  x1 y1 x2 y2
  scores : float32 (N,)    sigmoid probabilities
  kps    : float32 (N, 10) or None
        )doc"
    );
}
