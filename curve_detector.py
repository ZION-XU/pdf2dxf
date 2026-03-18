"""曲线智能识别模块 - 将PDF贝塞尔曲线转为DXF圆弧/圆/样条曲线"""

import math
from typing import Optional


def _distance(p1: tuple, p2: tuple) -> float:
    """两点距离"""
    return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def _midpoint(p1: tuple, p2: tuple) -> tuple:
    """两点中点"""
    return ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)


def _fit_circle_from_3_points(
    p1: tuple, p2: tuple, p3: tuple
) -> Optional[tuple]:
    """
    从三个点拟合圆心和半径
    返回 (cx, cy, radius) 或 None
    """
    ax, ay = p1
    bx, by = p2
    cx, cy = p3

    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-10:
        return None

    ux = ((ax * ax + ay * ay) * (by - cy) +
          (bx * bx + by * by) * (cy - ay) +
          (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) +
          (bx * bx + by * by) * (ax - cx) +
          (cx * cx + cy * cy) * (bx - ax)) / d

    radius = _distance((ux, uy), p1)
    return (ux, uy, radius)


def _bezier_point(t: float, p0: tuple, p1: tuple, p2: tuple, p3: tuple) -> tuple:
    """计算三次贝塞尔曲线上t处的点"""
    u = 1 - t
    x = (u ** 3 * p0[0] + 3 * u ** 2 * t * p1[0] +
         3 * u * t ** 2 * p2[0] + t ** 3 * p3[0])
    y = (u ** 3 * p0[1] + 3 * u ** 2 * t * p1[1] +
         3 * u * t ** 2 * p2[1] + t ** 3 * p3[1])
    return (x, y)


def _sample_bezier(p0: tuple, p1: tuple, p2: tuple, p3: tuple,
                   n_samples: int = 10) -> list:
    """在贝塞尔曲线上均匀采样n个点"""
    return [_bezier_point(t / n_samples, p0, p1, p2, p3)
            for t in range(n_samples + 1)]


def detect_arc_from_bezier(
    p0: tuple, p1: tuple, p2: tuple, p3: tuple,
    tolerance: float = 0.5
) -> Optional[dict]:
    """
    尝试将一段三次贝塞尔曲线识别为圆弧

    Args:
        p0, p1, p2, p3: 贝塞尔曲线的4个控制点
        tolerance: 拟合容差(点)

    Returns:
        识别成功返回 {"type": "arc", "center": (cx,cy), "radius": r,
                      "start_angle": deg, "end_angle": deg}
        失败返回 None
    """
    # 在曲线上采样多个点
    samples = _sample_bezier(p0, p1, p2, p3, 20)

    # 用首、中、尾三点拟合圆
    mid_idx = len(samples) // 2
    circle = _fit_circle_from_3_points(
        samples[0], samples[mid_idx], samples[-1]
    )
    if circle is None:
        return None

    cx, cy, radius = circle

    # 检查所有采样点是否都在圆上（容差范围内）
    for pt in samples:
        dist = abs(_distance(pt, (cx, cy)) - radius)
        if dist > tolerance:
            return None

    # 计算起始和结束角度
    start_angle = math.degrees(math.atan2(
        samples[0][1] - cy, samples[0][0] - cx))
    end_angle = math.degrees(math.atan2(
        samples[-1][1] - cy, samples[-1][0] - cx))

    return {
        "type": "arc",
        "center": (cx, cy),
        "radius": radius,
        "start_angle": start_angle,
        "end_angle": end_angle,
    }


def detect_circle_from_beziers(curves: list,
                               tolerance: float = 0.5) -> Optional[dict]:
    """
    尝试将多段连续贝塞尔曲线识别为完整的圆

    Args:
        curves: [(p0,p1,p2,p3), ...] 多段贝塞尔曲线
        tolerance: 拟合容差

    Returns:
        识别成功返回 {"type": "circle", "center": (cx,cy), "radius": r}
        失败返回 None
    """
    if len(curves) < 3:
        return None

    # 收集所有采样点
    all_samples = []
    for p0, p1, p2, p3 in curves:
        all_samples.extend(_sample_bezier(p0, p1, p2, p3, 10))

    if len(all_samples) < 10:
        return None

    # 用三个均匀分布的点拟合圆
    n = len(all_samples)
    circle = _fit_circle_from_3_points(
        all_samples[0], all_samples[n // 3], all_samples[2 * n // 3]
    )
    if circle is None:
        return None

    cx, cy, radius = circle

    # 检查所有点
    for pt in all_samples:
        dist = abs(_distance(pt, (cx, cy)) - radius)
        if dist > tolerance:
            return None

    # 检查首尾是否闭合
    if _distance(all_samples[0], all_samples[-1]) > tolerance * 2:
        return None

    return {
        "type": "circle",
        "center": (cx, cy),
        "radius": radius,
    }


def bezier_to_polyline_points(p0: tuple, p1: tuple, p2: tuple, p3: tuple,
                              segments: int = 20) -> list:
    """将贝塞尔曲线转换为多段线的点序列"""
    return _sample_bezier(p0, p1, p2, p3, segments)
