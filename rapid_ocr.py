# -*- coding: utf-8 -*-
"""rapid_ocr.py —— RapidOCR（PP-OCRv5 mobile / ONNX）薄封装。

依赖与模型都随包放在 deps\\site-packages_rapid 和 deps\\rapidocr_models，
不联网、不依赖系统 Python；系统里装没装 rapidocr 都无所谓。
同机实测（RTX 4060 笔记本，纯 CPU）：整屏热态约 0.3 秒，中文认得全。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "deps", "site-packages_rapid")
_MODELS = os.path.join(_HERE, "deps", "rapidocr_models")

# det 限边 512(max)：App 文字大，压到 512 检测照样全出，整屏省一半时间；
# 再压（384）会丢小字，不要动。
_PARAMS = {
    "Det.model_path": os.path.join(_MODELS, "ch_PP-OCRv5_det_mobile.onnx"),
    "Rec.model_path": os.path.join(_MODELS, "ch_PP-OCRv5_rec_mobile.onnx"),
    "Cls.model_path": os.path.join(_MODELS, "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
    "Det.limit_side_len": 512,
    "Det.limit_type": "max",
    # 初始化那几行 [INFO] 不进日志；main.py 会用它 setLevel，必须走这个参数
    "Global.log_level": "warning",
}

_engine = None
_failed = False


def available():
    """deps 里模型和依赖齐不齐（缺了启动时直接提示退出）。"""
    return (os.path.isfile(_PARAMS["Det.model_path"])
            and os.path.isfile(_PARAMS["Rec.model_path"])
            and os.path.isdir(_VENDOR))


def reset():
    """丢掉当前引擎，下次 recognize 重新初始化（偶发失败时用）。"""
    global _engine, _failed
    _engine = None
    _failed = False


def recognize(img, box=None):
    """对 PIL 图（可选 box 裁剪区）跑一遍识别。

    返回 [(text, left, top, right, bottom), ...]，坐标已换回原图；
    引擎不可用/调用失败返回 None，没认出字返回 []。
    偶发失败会重置引擎重试一次，避免一次抖动让整轮答题停摆。
    """
    global _failed
    if _failed:
        return None
    try:
        eng = _get_engine()
    except Exception:
        reset()
        try:
            eng = _get_engine()
        except Exception:
            _failed = True
            return None

    ox, oy = 0, 0
    if box is not None:
        left, top = max(0, int(box[0])), max(0, int(box[1]))
        right = min(img.width, int(box[2]))
        bottom = min(img.height, int(box[3]))
        if right - left < 8 or bottom - top < 8:
            return []
        img, ox, oy = img.crop((left, top, right, bottom)), left, top

    try:
        import numpy
        import cv2
        arr = numpy.asarray(img.convert("RGB"))
        result = eng(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), use_cls=False)
    except Exception:
        reset()                       # 引擎可能坏了：重建后重试一次
        try:
            import numpy
            import cv2
            arr = numpy.asarray(img.convert("RGB"))
            result = _get_engine()(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR),
                                   use_cls=False)
        except Exception:
            _failed = True
            return None

    boxes = [] if result.boxes is None else list(result.boxes)
    txts = [] if result.txts is None else list(result.txts)
    out = []
    for bx, txt in zip(boxes, txts):
        text = str(txt).strip()
        if not text:
            continue
        xs = [p[0] for p in bx]
        ys = [p[1] for p in bx]
        out.append((text, int(min(xs)) + ox, int(min(ys)) + oy,
                    int(max(xs)) + ox, int(max(ys)) + oy))
    return out


def _get_engine():
    global _engine
    if _engine is None:
        if _VENDOR not in sys.path:
            sys.path.insert(0, _VENDOR)
        from rapidocr import RapidOCR
        _engine = RapidOCR(params=_PARAMS)
    return _engine
