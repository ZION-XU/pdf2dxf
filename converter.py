"""PDF转DXF转换引擎核心"""

import math
import os
import tempfile
from pathlib import Path
from typing import Optional

import fitz  # pymupdf
import ezdxf
from ezdxf.math import Vec3

from config import (
    DEFAULT_FONT, DXF_VERSIONS,
    CURVE_MODE_SPLINE, CURVE_MODE_POLYLINE, CURVE_MODE_LINE,
    LAYER_STRATEGY_NONE, LAYER_STRATEGY_PDF,
)
from curve_detector import (
    detect_arc_from_bezier,
    detect_circle_from_beziers,
    bezier_to_polyline_points,
)
from layer_manager import LayerManager


def _rgb_to_dxf_color(rgb: tuple) -> int:
    """将RGB颜色映射到最接近的DXF颜色索引(1-7)"""
    if rgb is None or len(rgb) < 3:
        return 7  # 白色

    r, g, b = rgb[0], rgb[1], rgb[2]

    # 常见颜色映射
    colors = {
        1: (255, 0, 0),      # 红
        2: (255, 255, 0),    # 黄
        3: (0, 255, 0),      # 绿
        4: (0, 255, 255),    # 青
        5: (0, 0, 255),      # 蓝
        6: (255, 0, 255),    # 品红
        7: (255, 255, 255),  # 白
    }

    # 如果太暗则用白色
    if r + g + b < 30:
        return 7

    best_color = 7
    min_dist = float('inf')
    for idx, (cr, cg, cb) in colors.items():
        dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if dist < min_dist:
            min_dist = dist
            best_color = idx

    return best_color


def _fitz_color_to_rgb_int(color: tuple) -> Optional[tuple]:
    """将fitz颜色(0-1浮点)转为(0-255整数)"""
    if color is None or len(color) < 3:
        return None
    return (int(color[0] * 255), int(color[1] * 255), int(color[2] * 255))


def _linewidth_mm_to_dxf(width_pt: float) -> float:
    """将点(pt)线宽转为mm（DXF线宽单位是百分之一毫米的整数）"""
    mm = width_pt * 0.3528  # 1pt = 0.3528mm
    # DXF线宽取整到标准值
    standard = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50,
                53, 60, 70, 80, 90, 100, 106, 120, 140, 158,
                200, 211]
    mm_100 = int(mm * 100)
    closest = min(standard, key=lambda x: abs(x - mm_100))
    return closest


def _distance(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def _is_close_point(
    p1: tuple[float, float],
    p2: tuple[float, float],
    tolerance: float = 1.0,
) -> bool:
    return _distance(p1, p2) <= tolerance


def _point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    seg_len_sq = (end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2
    if seg_len_sq <= 1e-9:
        return _distance(point, start)
    t = (
        (point[0] - start[0]) * (end[0] - start[0])
        + (point[1] - start[1]) * (end[1] - start[1])
    ) / seg_len_sq
    t = max(0.0, min(1.0, t))
    proj = (
        start[0] + t * (end[0] - start[0]),
        start[1] + t * (end[1] - start[1]),
    )
    return _distance(point, proj)


class PdfToDxfConverter:
    """PDF转DXF转换器"""

    STROKE_WIDTH_SCALE = 1.8

    def __init__(
        self,
        curve_mode: str = CURVE_MODE_SPLINE,
        layer_strategy: str = LAYER_STRATEGY_NONE,
        dxf_version: str = "R2018",
        preserve_colors: bool = True,
        extract_images: bool = False,
        page_range: Optional[str] = None,
    ):
        self.curve_mode = curve_mode
        self.layer_strategy = layer_strategy
        self.dxf_version = dxf_version
        self.preserve_colors = preserve_colors
        self.extract_images = extract_images
        self.page_range = page_range

        self._progress_callback = None
        self._cancel_flag = False

    def set_progress_callback(self, callback):
        """设置进度回调函数: callback(current, total, message)"""
        self._progress_callback = callback

    def cancel(self):
        """取消转换"""
        self._cancel_flag = True

    def _report_progress(self, current: int, total: int, message: str):
        if self._progress_callback:
            self._progress_callback(current, total, message)

    def _parse_page_range(self, total_pages: int) -> list:
        """解析页码范围字符串"""
        if not self.page_range or self.page_range.strip() == "":
            return list(range(total_pages))

        pages = set()
        for part in self.page_range.split(','):
            part = part.strip()
            if '-' in part:
                start, end = part.split('-', 1)
                start = max(1, int(start.strip()))
                end = min(total_pages, int(end.strip()))
                pages.update(range(start - 1, end))
            else:
                p = int(part.strip()) - 1
                if 0 <= p < total_pages:
                    pages.add(p)
        return sorted(pages)

    def convert(self, input_path: str, output_path: str):
        """
        执行转换

        Args:
            input_path: PDF文件路径
            output_path: 输出DXF文件路径
        """
        self._cancel_flag = False

        # 打开PDF
        doc = fitz.open(input_path)
        total_pages = doc.page_count
        pages_to_convert = self._parse_page_range(total_pages)

        # 创建DXF
        dwg = ezdxf.new(self.dxf_version)
        msp = dwg.modelspace()
        self._ensure_linetypes(dwg)

        # 创建中文字体样式
        try:
            dwg.styles.add("CHINESE", font=DEFAULT_FONT)
        except ezdxf.DXFTableEntryError:
            pass

        # 图层管理器
        layer_mgr = LayerManager(dwg, self.layer_strategy)

        total = len(pages_to_convert)
        for idx, page_num in enumerate(pages_to_convert):
            if self._cancel_flag:
                doc.close()
                return

            self._report_progress(idx, total, f"正在转换第 {page_num + 1} 页...")

            page = doc[page_num]
            page_height = page.rect.height
            y_offset = idx * page_height  # 多页纵向排列

            # 创建PDF图层（仅PDF图层策略）
            if self.layer_strategy == LAYER_STRATEGY_PDF:
                self._build_ocg_layer_maps(doc, page, layer_mgr)

            # 1. 提取矢量图形
            self._extract_vectors(
                page, msp, layer_mgr, page_num, page_height, y_offset,
            )

            # 2. 提取文字
            self._extract_text(
                page, msp, layer_mgr, page_num, page_height, y_offset,
            )

            # 3. 提取图片（可选）
            if self.extract_images:
                self._extract_images(
                    doc, page, msp, layer_mgr, page_num, page_height, y_offset
                )

        self._report_progress(total, total, "正在保存DXF文件...")
        doc.close()
        dwg.saveas(output_path)
        self._report_progress(total, total, "转换完成")
    def _path_fingerprint(self, path: dict) -> str:
        """为矢量路径生成稳定指纹，用于跨OCG可见性切换时识别同一图形。"""
        parts = []
        for item in path.get("items", []):
            cmd = item[0]
            if cmd == 'l':
                parts.append(
                    f"l{item[1].x:.1f},{item[1].y:.1f},{item[2].x:.1f},{item[2].y:.1f}")
            elif cmd == 'c':
                parts.append(
                    f"c{item[1].x:.1f},{item[1].y:.1f},{item[2].x:.1f},{item[2].y:.1f},"
                    f"{item[3].x:.1f},{item[3].y:.1f},{item[4].x:.1f},{item[4].y:.1f}")
            elif cmd == 're':
                r = item[1]
                parts.append(f"r{r.x0:.1f},{r.y0:.1f},{r.x1:.1f},{r.y1:.1f}")
            elif cmd == 'qu':
                q = item[1]
                parts.append(
                    f"q{q.ul.x:.1f},{q.ul.y:.1f},{q.ur.x:.1f},{q.ur.y:.1f},"
                    f"{q.lr.x:.1f},{q.lr.y:.1f},{q.ll.x:.1f},{q.ll.y:.1f}")
        color = path.get("color")
        fill = path.get("fill")
        c_str = f"{color}" if color else ""
        f_str = f"{fill}" if fill else ""
        return f"{';'.join(parts)}|{c_str}|{f_str}|{path.get('width', 0):.2f}"

    def _text_fingerprint(self, span: dict) -> str:
        """为文字span生成稳定指纹。"""
        bbox = span.get('bbox', (0, 0, 0, 0))
        text = span.get('text', '')
        return f"{bbox[0]:.1f},{bbox[1]:.1f},{bbox[2]:.1f},{bbox[3]:.1f}|{text}"

    def _build_ocg_layer_maps(self, doc, page, layer_mgr: LayerManager):
        """
        从 page.get_drawings() 的 layer 字段收集PDF图层名，
        在DXF中创建对应图层。直接从path中读取layer，无需指纹映射。
        """
        for path in page.get_drawings():
            name = path.get('layer')
            if name:
                layer_mgr.create_pdf_layer(f"PDF_{name}")
        return None, None

    def _ensure_linetypes(self, dwg):
        """Ensure basic linetypes exist for merged dashed lines."""
        definitions = {
            "DASHED": [0.6, 0.5, -0.1],
            "DOT": [0.2, 0.0, -0.2],
            "DASHDOT": [1.2, 0.6, -0.2, 0.0, -0.2],
        }
        for name, pattern in definitions.items():
            if name in dwg.linetypes:
                continue
            try:
                dwg.linetypes.add(name=name, pattern=pattern)
            except ezdxf.DXFTableEntryError:
                pass

    def _transform_point(self, page, x: float, y: float, y_offset: float = 0.0) -> tuple:
        """Map page coordinates into DXF display coordinates."""
        point = fitz.Point(x, y) * page.rotation_matrix
        return (float(point.x), float(page.rect.height - point.y + y_offset))

    def _transform_rect_to_points(self, page, rect, y_offset: float) -> list[tuple]:
        corners = [
            (rect.x0, rect.y0),
            (rect.x1, rect.y0),
            (rect.x1, rect.y1),
            (rect.x0, rect.y1),
        ]
        points = [self._transform_point(page, x, y, y_offset) for x, y in corners]
        points.append(points[0])
        return points

    def _path_bbox(self, page, path: dict, y_offset: float) -> Optional[tuple[float, float, float, float]]:
        xs = []
        ys = []
        for item in path.get("items", []):
            cmd = item[0]
            if cmd == "l":
                points = [item[1], item[2]]
            elif cmd == "c":
                points = [item[1], item[2], item[3], item[4]]
            elif cmd == "re":
                rect_points = self._transform_rect_to_points(page, item[1], y_offset)
                xs.extend(point[0] for point in rect_points)
                ys.extend(point[1] for point in rect_points)
                continue
            elif cmd == "qu":
                quad = item[1]
                points = [quad.ul, quad.ur, quad.lr, quad.ll]
            else:
                continue

            transformed = [
                self._transform_point(page, point.x, point.y, y_offset)
                for point in points
            ]
            xs.extend(point[0] for point in transformed)
            ys.extend(point[1] for point in transformed)

        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def _dedupe_points(self, points: list[tuple], tolerance: float = 1.0) -> list[tuple]:
        unique = []
        for point in points:
            if not unique or not _is_close_point(unique[-1], point, tolerance):
                unique.append(point)
        if len(unique) > 1 and _is_close_point(unique[0], unique[-1], tolerance):
            unique.pop()
        return unique

    def _path_line_segments(self, page, items: list, y_offset: float) -> list[tuple]:
        segments = []
        for item in items:
            if item[0] != "l":
                return []
            p1 = self._transform_point(page, item[1].x, item[1].y, y_offset)
            p2 = self._transform_point(page, item[2].x, item[2].y, y_offset)
            if _distance(p1, p2) > 1e-6:
                segments.append((p1, p2))
        return segments

    def _chain_segments(self, segments: list[tuple], closed: bool,
                        tolerance: float = 1.0) -> list[tuple]:
        if not segments:
            return []

        remaining = list(segments[1:])
        points = [segments[0][0], segments[0][1]]
        current = segments[0][1]

        while remaining:
            match_index = None
            next_point = None
            for idx, (seg_start, seg_end) in enumerate(remaining):
                if _is_close_point(current, seg_start, tolerance):
                    match_index = idx
                    next_point = seg_end
                    break
                if _is_close_point(current, seg_end, tolerance):
                    match_index = idx
                    next_point = seg_start
                    break
            if match_index is None:
                return []
            points.append(next_point)
            current = next_point
            remaining.pop(match_index)

        if closed:
            if not _is_close_point(points[0], points[-1], tolerance):
                return []
            return self._dedupe_points(points[:-1], tolerance)

        return self._dedupe_points(points, tolerance)

    def _ordered_loop_points(self, segments: list[tuple], tolerance: float = 1.0) -> list[tuple]:
        if len(segments) < 3:
            return []
        for index in range(len(segments)):
            ordered = self._chain_segments(
                segments[index:] + segments[:index],
                closed=True,
                tolerance=tolerance,
            )
            if ordered:
                return ordered
        return []

    def _ordered_path_points(self, segments: list[tuple], tolerance: float = 1.0) -> list[tuple]:
        if not segments:
            return []
        for index in range(len(segments)):
            ordered = self._chain_segments(
                segments[index:] + segments[:index],
                closed=False,
                tolerance=tolerance,
            )
            if ordered:
                return ordered
        return []

    def _scaled_lineweight(self, width_pt: float) -> float:
        return _linewidth_mm_to_dxf(width_pt * self.STROKE_WIDTH_SCALE)

    def _add_polyline(self, msp, points: list[tuple], attribs: dict,
                      tolerance: float = 1.0, close: Optional[bool] = None):
        is_closed = len(points) > 2 and _is_close_point(points[0], points[-1], tolerance)
        if close is not None:
            is_closed = close
        emit_points = self._dedupe_points(points, tolerance)
        if len(emit_points) < 2:
            return
        msp.add_lwpolyline(emit_points, close=is_closed, dxfattribs=attribs)

    def _try_add_connected_polyline(self, page, path: dict, msp, attribs: dict,
                                    y_offset: float) -> bool:
        """Merge connected straight segments into a polyline when safe."""
        if path.get("fill") or path.get("dashes") not in (None, "[] 0"):
            return False

        segments = self._path_line_segments(page, path.get("items", []), y_offset)
        if len(segments) < 2:
            return False

        points = self._ordered_path_points(segments)
        if len(points) < 3:
            return False

        poly_attribs = dict(attribs)
        self._add_polyline(msp, points, poly_attribs)
        return True

    def _polygon_area(self, points: list[tuple]) -> float:
        if len(points) < 3:
            return 0.0
        area = 0.0
        for index, point in enumerate(points):
            next_point = points[(index + 1) % len(points)]
            area += point[0] * next_point[1] - next_point[0] * point[1]
        return abs(area) * 0.5

    def _try_collapse_fill_to_line(self, page, path: dict, attribs: dict,
                                   y_offset: float) -> Optional[dict]:
        """将细长填充多边形折叠为带线宽的中心线。

        统一处理 PDF 中用填充区域表示的粗线：
        - 6边自交叉条带
        - 4边闭合矩形条带
        - 通用细长多边形
        """
        items = path.get("items", [])
        if len(items) < 4 or any(item[0] != "l" for item in items):
            return None

        # --- 模式1: 6边自交叉条带 ---
        if len(items) == 6:
            result = self._try_6edge_strip(page, items, attribs, y_offset)
            if result:
                return result

        # --- 模式2: 4边闭合矩形条带 ---
        if len(items) == 4:
            result = self._try_4edge_strip(page, items, attribs, y_offset)
            if result:
                return result

        # --- 模式3: 通用细长多边形 ---
        return self._try_elongated_polygon(page, items, attribs, y_offset)

    def _try_6edge_strip(self, page, items: list, attribs: dict,
                         y_offset: float) -> Optional[dict]:
        """6边自交叉填充条带 → 中心线。"""
        transformed = []
        for item in items:
            start = self._transform_point(page, item[1].x, item[1].y, y_offset)
            end = self._transform_point(page, item[2].x, item[2].y, y_offset)
            transformed.append((start, end))

        a0, a1 = transformed[0]
        b0, b1 = transformed[1]
        c0, c1 = transformed[2]
        d0, d1 = transformed[3]
        e0, e1 = transformed[4]
        f0, f1 = transformed[5]

        if not (
            _is_close_point(a1, b0, 0.5)
            and _is_close_point(b1, c0, 0.5)
            and _is_close_point(c1, a0, 0.5)
            and _is_close_point(d0, b0, 0.5)
            and _is_close_point(d1, b1, 0.5)
            and _is_close_point(e0, d1, 0.5)
            and _is_close_point(f0, e1, 0.5)
            and _is_close_point(f1, d0, 0.5)
        ):
            return None

        start = ((a0[0] + a1[0]) * 0.5, (a0[1] + a1[1]) * 0.5)
        end = ((e0[0] + e1[0]) * 0.5, (e0[1] + e1[1]) * 0.5)
        thickness = max(_distance(a0, a1), _distance(e0, e1))
        center_length = _distance(start, end)
        if center_length <= 1e-6 or thickness <= 1e-6:
            return None
        if thickness / center_length >= 0.12:
            return None

        line_attribs = dict(attribs)
        line_attribs["lineweight"] = self._scaled_lineweight(thickness / 0.3528)
        return self._collect_line_entry(start, end, line_attribs)

    def _try_4edge_strip(self, page, items: list, attribs: dict,
                         y_offset: float) -> Optional[dict]:
        """4边闭合矩形填充条带 → 中心线。"""
        segments = self._path_line_segments(page, items, y_offset)
        if len(segments) != 4:
            return None

        points = self._ordered_loop_points(segments, tolerance=0.5)
        if len(points) != 4:
            return None

        edges = []
        for idx, point in enumerate(points):
            next_point = points[(idx + 1) % len(points)]
            edges.append((point, next_point, _distance(point, next_point)))
        lengths = [edge[2] for edge in edges]
        long_edge = max(lengths)
        short_edge = min(lengths)
        if short_edge <= 1e-6 or long_edge <= 1e-6:
            return None
        if short_edge / long_edge >= 0.12:
            return None

        long_edges = [edge for edge in edges if edge[2] >= long_edge * 0.7]
        if len(long_edges) != 2:
            return None

        start = (
            (long_edges[0][0][0] + long_edges[1][0][0]) / 2,
            (long_edges[0][0][1] + long_edges[1][0][1]) / 2,
        )
        end = (
            (long_edges[0][1][0] + long_edges[1][1][0]) / 2,
            (long_edges[0][1][1] + long_edges[1][1][1]) / 2,
        )
        if _distance(start, end) <= 1e-6:
            return None

        line_attribs = dict(attribs)
        line_attribs["lineweight"] = max(
            attribs.get("lineweight", 0) or 0,
            self._scaled_lineweight(short_edge / 0.3528),
        )
        return self._collect_line_entry(start, end, line_attribs)

    def _try_elongated_polygon(self, page, items: list, attribs: dict,
                               y_offset: float) -> Optional[dict]:
        """通用细长填充多边形 → 中心线。"""
        if len(items) < 6:
            return None

        points = []
        for item in items:
            points.append(self._transform_point(page, item[1].x, item[1].y, y_offset))
            points.append(self._transform_point(page, item[2].x, item[2].y, y_offset))
        points = self._dedupe_points(points, tolerance=0.25)
        if len(points) < 4:
            return None

        longest = 0.0
        start = end = None
        for idx, p1 in enumerate(points):
            for p2 in points[idx + 1:]:
                length = _distance(p1, p2)
                if length > longest:
                    longest = length
                    start, end = p1, p2
        if longest <= 1e-6:
            return None

        ux = (end[0] - start[0]) / longest
        uy = (end[1] - start[1]) / longest
        nx, ny = -uy, ux

        projected = [point[0] * ux + point[1] * uy for point in points]
        offsets = [point[0] * nx + point[1] * ny for point in points]
        long_dim = max(projected) - min(projected)
        short_dim = max(offsets) - min(offsets)
        if long_dim <= 8.0 or short_dim <= 0.1:
            return None
        if short_dim / long_dim >= 0.18 or short_dim > 20.0:
            return None

        edge_window = max(short_dim * 1.5, 1.0)
        min_proj, max_proj = min(projected), max(projected)
        start_pts = [pt for pt, val in zip(points, projected) if val - min_proj <= edge_window]
        end_pts = [pt for pt, val in zip(points, projected) if max_proj - val <= edge_window]
        if not start_pts or not end_pts:
            return None

        start_center = (
            sum(pt[0] for pt in start_pts) / len(start_pts),
            sum(pt[1] for pt in start_pts) / len(start_pts),
        )
        end_center = (
            sum(pt[0] for pt in end_pts) / len(end_pts),
            sum(pt[1] for pt in end_pts) / len(end_pts),
        )
        if _distance(start_center, end_center) <= 1e-6:
            return None

        line_attribs = dict(attribs)
        line_attribs["lineweight"] = self._scaled_lineweight(short_dim / 0.3528)
        return self._collect_line_entry(start_center, end_center, line_attribs)

    def _emit_fill_as_hatch(self, page, path: dict, msp, attribs: dict,
                            y_offset: float):
        """对所有填充路径无条件输出 HATCH，与 AutoCAD 一致。"""
        if not path.get("fill"):
            return

        # 尝试结构化边界 HATCH（处理曲线等复杂路径）
        if self._try_add_fill_hatch(page, path, msp, attribs, y_offset):
            return

        # 降级：收集所有点并构建多边形 HATCH
        items = path.get("items", [])
        if not items:
            return

        points = []
        for item in items:
            cmd = item[0]
            if cmd == 'l':
                points.append(self._transform_point(page, item[1].x, item[1].y, y_offset))
                points.append(self._transform_point(page, item[2].x, item[2].y, y_offset))
            elif cmd == 'c':
                points.append(self._transform_point(page, item[1].x, item[1].y, y_offset))
                points.append(self._transform_point(page, item[4].x, item[4].y, y_offset))
            elif cmd == 're':
                points.extend(self._transform_rect_to_points(page, item[1], y_offset))
            elif cmd == 'qu':
                quad = item[1]
                for corner in [quad.ul, quad.ur, quad.lr, quad.ll]:
                    points.append(self._transform_point(page, corner.x, corner.y, y_offset))

        unique = self._dedupe_points(points, tolerance=0.25)
        if len(unique) < 3:
            return

        cx = sum(p[0] for p in unique) / len(unique)
        cy = sum(p[1] for p in unique) / len(unique)
        ordered = sorted(unique, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
        ordered = self._dedupe_points(ordered, tolerance=0.25)
        if len(ordered) < 3:
            return

        hatch_attribs = dict(attribs)
        fill_color = _fitz_color_to_rgb_int(path.get("fill"))
        if self.preserve_colors and fill_color:
            hatch_attribs["color"] = _rgb_to_dxf_color(fill_color)
        hatch_attribs.pop("lineweight", None)

        hatch = msp.add_hatch(dxfattribs=hatch_attribs)
        hatch.paths.add_polyline_path(ordered, is_closed=True)

    def _try_add_fill_polygon_hatch(self, page, path: dict, msp, attribs: dict,
                                    y_offset: float) -> bool:
        """Approximate line-only fill strips as a solid hatch polygon."""
        if not path.get("fill"):
            return False

        items = path.get("items", [])
        if len(items) < 4 or any(item[0] != "l" for item in items):
            return False

        points = []
        for item in items:
            points.append(self._transform_point(page, item[1].x, item[1].y, y_offset))
            points.append(self._transform_point(page, item[2].x, item[2].y, y_offset))
        points = self._dedupe_points(points, tolerance=0.25)
        if len(points) < 3:
            return False

        cx = sum(point[0] for point in points) / len(points)
        cy = sum(point[1] for point in points) / len(points)
        ordered = sorted(points, key=lambda point: math.atan2(point[1] - cy, point[0] - cx))
        ordered = self._dedupe_points(ordered, tolerance=0.25)
        if len(ordered) < 3:
            return False

        area = self._polygon_area(ordered)
        xs = [point[0] for point in ordered]
        ys = [point[1] for point in ordered]
        long_dim = max(max(xs) - min(xs), max(ys) - min(ys))
        if area <= 0.2 or long_dim <= 1.0:
            return False

        hatch_attribs = dict(attribs)
        fill_color = _fitz_color_to_rgb_int(path.get("fill"))
        if self.preserve_colors and fill_color:
            hatch_attribs["color"] = _rgb_to_dxf_color(fill_color)
        hatch_attribs.pop("lineweight", None)

        hatch = msp.add_hatch(dxfattribs=hatch_attribs)
        hatch.paths.add_polyline_path(ordered, is_closed=True)
        return True

    def _try_add_fill_hatch(self, page, path: dict, msp, attribs: dict,
                            y_offset: float) -> bool:
        """Convert a filled path into a single-boundary SOLID hatch."""
        if not path.get("fill"):
            return False

        items = path.get("items", [])
        if not items:
            return False

        boundary = []
        for item in items:
            cmd = item[0]
            if cmd == 'l':
                start = self._transform_point(page, item[1].x, item[1].y, y_offset)
                end = self._transform_point(page, item[2].x, item[2].y, y_offset)
                if not boundary:
                    boundary.extend([start, end])
                else:
                    if not _is_close_point(boundary[-1], start, 0.5):
                        return False
                    boundary.append(end)
                continue

            if cmd == 'c':
                start = self._transform_point(page, item[1].x, item[1].y, y_offset)
                end = self._transform_point(page, item[4].x, item[4].y, y_offset)
                if not boundary:
                    boundary.extend([start, end])
                else:
                    if not _is_close_point(boundary[-1], start, 0.5):
                        return False
                    boundary.append(end)
                continue

            if cmd == 're':
                if boundary:
                    return False
                boundary.extend(self._transform_rect_to_points(page, item[1], y_offset))
                continue

            if cmd == 'qu':
                if boundary:
                    return False
                quad = item[1]
                boundary.extend([
                    self._transform_point(page, quad.ul.x, quad.ul.y, y_offset),
                    self._transform_point(page, quad.ur.x, quad.ur.y, y_offset),
                    self._transform_point(page, quad.lr.x, quad.lr.y, y_offset),
                    self._transform_point(page, quad.ll.x, quad.ll.y, y_offset),
                    self._transform_point(page, quad.ul.x, quad.ul.y, y_offset),
                ])
                continue

            return False

        if not boundary:
            return False

        is_closed = _is_close_point(boundary[0], boundary[-1], 0.5)
        points = self._dedupe_points(boundary)
        if len(points) < 3 or not is_closed:
            return False

        hatch_attribs = dict(attribs)
        fill_color = _fitz_color_to_rgb_int(path.get("fill"))
        if self.preserve_colors and fill_color:
            hatch_attribs["color"] = _rgb_to_dxf_color(fill_color)
        hatch_attribs.pop("lineweight", None)

        hatch = msp.add_hatch(dxfattribs=hatch_attribs)
        hatch.paths.add_polyline_path(points, is_closed=True)
        return True

    def _classify_dash_linetype(self, lengths: list[float], gaps: list[float]) -> str:
        avg_length = sum(lengths) / len(lengths)
        avg_gap = sum(gaps) / len(gaps)
        if avg_length <= max(avg_gap * 0.35, 1.0):
            return "DOT"
        if max(lengths) >= max(min(lengths) * 2.5, min(lengths) + 1.0):
            return "DASHDOT"
        return "DASHED"

    def _try_merge_dashed_segments(self, page, path: dict, attribs: dict,
                                   y_offset: float) -> Optional[dict]:
        """Merge collinear short segments from expanded PDF dashes."""
        if path.get("fill"):
            return None

        segments = self._path_line_segments(page, path.get("items", []), y_offset)
        if len(segments) < 2:
            return None

        p_start, p_end = segments[0]
        direction = (p_end[0] - p_start[0], p_end[1] - p_start[1])
        base_length = math.hypot(direction[0], direction[1])
        if base_length <= 1e-6:
            return None

        ux = direction[0] / base_length
        uy = direction[1] / base_length
        nx = -uy
        ny = ux
        line_ref = p_start[0] * nx + p_start[1] * ny

        projected = []
        for seg_start, seg_end in segments:
            seg_dx = seg_end[0] - seg_start[0]
            seg_dy = seg_end[1] - seg_start[1]
            seg_len = math.hypot(seg_dx, seg_dy)
            if seg_len <= 1e-6:
                return None
            cross = abs(seg_dx * uy - seg_dy * ux) / seg_len
            if cross > 0.02:
                return None
            for point in (seg_start, seg_end):
                if abs(point[0] * nx + point[1] * ny - line_ref) > 1.0:
                    return None
            t0 = seg_start[0] * ux + seg_start[1] * uy
            t1 = seg_end[0] * ux + seg_end[1] * uy
            if t1 < t0:
                t0, t1 = t1, t0
            projected.append((t0, t1))

        projected.sort(key=lambda item: item[0])
        gaps = []
        lengths = []
        prev_end = projected[0][1]
        for start_t, end_t in projected:
            lengths.append(end_t - start_t)
            gap = start_t - prev_end
            if gap > 0.5:
                gaps.append(gap)
            prev_end = max(prev_end, end_t)

        if not gaps:
            return None

        start_t = min(item[0] for item in projected)
        end_t = max(item[1] for item in projected)
        merged_start = (ux * start_t - nx * line_ref, uy * start_t - ny * line_ref)
        merged_end = (ux * end_t - nx * line_ref, uy * end_t - ny * line_ref)

        line_attribs = dict(attribs)
        line_attribs["linetype"] = self._classify_dash_linetype(lengths, gaps)
        return self._collect_line_entry(merged_start, merged_end, line_attribs)

    def _line_style_key(self, attribs: dict) -> tuple:
        return (
            attribs.get("layer"),
            attribs.get("color"),
            attribs.get("linetype"),
        )

    def _collect_line_entry(self, p1: tuple, p2: tuple, attribs: dict) -> Optional[dict]:
        length = _distance(p1, p2)
        if length <= 1e-6:
            return None

        start, end = p1, p2
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        ux = dx / length
        uy = dy / length
        if ux < 0 or (abs(ux) < 1e-9 and uy < 0):
            start, end = end, start
            ux = -ux
            uy = -uy

        nx = -uy
        ny = ux
        return {
            "start": start,
            "end": end,
            "length": length,
            "ux": ux,
            "uy": uy,
            "offset": ((start[0] + end[0]) * 0.5) * nx + ((start[1] + end[1]) * 0.5) * ny,
            "t0": start[0] * ux + start[1] * uy,
            "t1": end[0] * ux + end[1] * uy,
            "attribs": dict(attribs),
            "used": False,
        }

    def _emit_line_clusters(self, entries: list[dict], msp):
        if not entries:
            return

        entries = self._dedupe_overlapping_entries(entries)
        grouped = {}
        for entry in entries:
            key = self._line_style_key(entry["attribs"])
            grouped.setdefault(key, []).append(entry)

        tolerance = 2.0
        for group in grouped.values():
            polylines = self._merge_entry_group(group, tolerance)
            rectangles, leftovers = self._reconstruct_rectangles(polylines)
            for polyline in rectangles + leftovers:
                if len(polyline["points"]) >= 2:
                    msp.add_lwpolyline(
                        polyline["points"],
                        close=polyline["closed"],
                        dxfattribs=polyline["attribs"],
                    )

    def _merge_entry_group(self, entries: list[dict], tolerance: float) -> list[dict]:
        polylines = []
        remaining = list(entries)
        while remaining:
            current = remaining.pop(0)
            points = [current["start"], current["end"]]
            merged_entries = [current]

            extended = True
            while extended:
                extended = False
                for idx, candidate in enumerate(remaining):
                    if _is_close_point(points[-1], candidate["start"], tolerance):
                        points.append(candidate["end"])
                    elif _is_close_point(points[-1], candidate["end"], tolerance):
                        points.append(candidate["start"])
                    elif _is_close_point(points[0], candidate["end"], tolerance):
                        points.insert(0, candidate["start"])
                    elif _is_close_point(points[0], candidate["start"], tolerance):
                        points.insert(0, candidate["end"])
                    else:
                        continue
                    merged_entries.append(candidate)
                    remaining.pop(idx)
                    extended = True
                    break

            is_closed = _is_close_point(points[0], points[-1], tolerance)
            emit_points = self._dedupe_points(points, tolerance)
            if len(emit_points) < 2:
                continue

            emit_attribs = dict(current["attribs"])
            lineweights = [
                entry["attribs"].get("lineweight", 0) or 0
                for entry in merged_entries
            ]
            emit_attribs["lineweight"] = max(lineweights) if lineweights else emit_attribs.get("lineweight")
            polylines.append({
                "points": emit_points,
                "closed": is_closed,
                "attribs": emit_attribs,
            })
        return polylines

    def _reconstruct_rectangles(self, polylines: list[dict]) -> tuple[list[dict], list[dict]]:
        candidates = []
        leftovers = []
        for polyline in polylines:
            points = polyline["points"]
            if polyline["closed"]:
                leftovers.append(polyline)
                continue
            if len(points) > 6:
                leftovers.append(polyline)
                continue

            path_length = sum(
                _distance(points[idx], points[idx + 1])
                for idx in range(len(points) - 1)
            )
            span = _distance(points[0], points[-1])
            if span <= 1e-6 or path_length / span > 1.08:
                leftovers.append(polyline)
                continue

            candidates.append({
                "start": points[0],
                "end": points[-1],
                "points": points,
                "attribs": polyline["attribs"],
                "length": span,
            })

        if len(candidates) < 4:
            return [], polylines

        node_tol = 10.0
        nodes = []

        def find_node(point: tuple[float, float]) -> int:
            for idx, node in enumerate(nodes):
                if _is_close_point(point, node, node_tol):
                    return idx
            nodes.append(point)
            return len(nodes) - 1

        edges = []
        for idx, candidate in enumerate(candidates):
            start_node = find_node(candidate["start"])
            end_node = find_node(candidate["end"])
            if start_node == end_node:
                continue
            edges.append({
                "index": idx,
                "start_node": start_node,
                "end_node": end_node,
                "candidate": candidate,
            })

        adjacency = {}
        for edge in edges:
            adjacency.setdefault(edge["start_node"], []).append(edge)
            adjacency.setdefault(edge["end_node"], []).append(edge)

        used = set()
        rectangles = []
        for edge in edges:
            if edge["index"] in used:
                continue

            component_edges = []
            stack = [edge]
            component_nodes = set()
            while stack:
                current = stack.pop()
                if current["index"] in {item["index"] for item in component_edges}:
                    continue
                component_edges.append(current)
                component_nodes.add(current["start_node"])
                component_nodes.add(current["end_node"])
                for node_id in (current["start_node"], current["end_node"]):
                    for neighbor in adjacency.get(node_id, []):
                        if neighbor["index"] not in {item["index"] for item in component_edges}:
                            stack.append(neighbor)

            if len(component_nodes) != 4 or len(component_edges) != 4:
                continue

            degrees = {node_id: 0 for node_id in component_nodes}
            for component_edge in component_edges:
                degrees[component_edge["start_node"]] += 1
                degrees[component_edge["end_node"]] += 1
            if any(degree != 2 for degree in degrees.values()):
                continue

            ordered_nodes = [next(iter(component_nodes))]
            prev_node = None
            while len(ordered_nodes) < 5:
                current_node = ordered_nodes[-1]
                neighbors = []
                for component_edge in component_edges:
                    if component_edge["start_node"] == current_node:
                        neighbors.append(component_edge["end_node"])
                    elif component_edge["end_node"] == current_node:
                        neighbors.append(component_edge["start_node"])
                next_nodes = [node for node in neighbors if node != prev_node]
                if not next_nodes:
                    break
                next_node = next_nodes[0]
                if next_node == ordered_nodes[0]:
                    ordered_nodes.append(next_node)
                    break
                ordered_nodes.append(next_node)
                prev_node = current_node

            if len(ordered_nodes) != 5 or ordered_nodes[-1] != ordered_nodes[0]:
                continue

            points = [nodes[node_id] for node_id in ordered_nodes[:-1]]
            emit_attribs = dict(component_edges[0]["candidate"]["attribs"])
            emit_attribs["lineweight"] = max(
                edge_item["candidate"]["attribs"].get("lineweight", 0) or 0
                for edge_item in component_edges
            )
            rectangles.append({
                "points": points,
                "closed": True,
                "attribs": emit_attribs,
            })
            used.update(edge_item["index"] for edge_item in component_edges)

        leftovers.extend(
            {
                "points": candidate["points"],
                "closed": False,
                "attribs": candidate["attribs"],
            }
            for candidate in candidates
            if candidate not in [edge["candidate"] for edge in edges if edge["index"] in used]
        )
        return rectangles, leftovers

    def _dedupe_overlapping_entries(self, entries: list[dict]) -> list[dict]:
        if len(entries) < 2:
            return entries

        kept = []
        for entry in sorted(entries, key=lambda item: item["length"], reverse=True):
            duplicate = False
            for other in kept:
                if self._line_style_key(entry["attribs"]) != self._line_style_key(other["attribs"]):
                    continue

                if abs(entry["length"] - other["length"]) > max(8.0, other["length"] * 0.08):
                    continue

                dot = abs(entry["ux"] * other["ux"] + entry["uy"] * other["uy"])
                if dot < math.cos(math.radians(2.0)):
                    continue

                offset_gap = abs(entry["offset"] - other["offset"])
                if offset_gap > 12.0:
                    continue

                start_gap = min(
                    _distance(entry["start"], other["start"]),
                    _distance(entry["start"], other["end"]),
                )
                end_gap = min(
                    _distance(entry["end"], other["start"]),
                    _distance(entry["end"], other["end"]),
                )
                if start_gap > 12.0 or end_gap > 12.0:
                    continue

                entry_weight = entry["attribs"].get("lineweight", 0) or 0
                other_weight = other["attribs"].get("lineweight", 0) or 0
                if entry_weight <= other_weight:
                    duplicate = True
                    break

            if not duplicate:
                kept.append(entry)

        return kept

    def _build_rectangle_overrides(self, page, layer_mgr: LayerManager,
                                   page_num: int, y_offset: float) -> tuple[set[int], list[dict]]:
        paths = page.get_drawings()
        candidates = []
        for index, path in enumerate(paths):
            items = path.get("items", [])
            is_line_only = items and all(item[0] == "l" for item in items)
            if not is_line_only:
                continue
            include = path.get("fill") and len(items) >= 4
            include = include or ((path.get("color") is not None) and path.get("width", 0) > 0 and len(items) <= 3)
            if not include:
                continue

            bbox = self._path_bbox(page, path, y_offset)
            if not bbox:
                continue

            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            if max(width, height) < 20.0:
                continue

            base_attribs = layer_mgr.get_dxf_attribs("vector", page_num)
            pdf_layer = path.get("layer")
            if self.layer_strategy == LAYER_STRATEGY_PDF and pdf_layer:
                base_attribs["layer"] = f"PDF_{pdf_layer}"
            stroke_color = _fitz_color_to_rgb_int(path.get("color"))
            if self.preserve_colors and stroke_color:
                base_attribs["color"] = _rgb_to_dxf_color(stroke_color)

            candidates.append({
                "index": index,
                "path": path,
                "bbox": bbox,
                "attribs": base_attribs,
                "layer": path.get("layer") or "",
            })

        overrides = []
        grouped_by_layer = {}
        for candidate in candidates:
            grouped_by_layer.setdefault(candidate["layer"], []).append(candidate)

        for layer_candidates in grouped_by_layer.values():
            remaining = list(layer_candidates)
            components = []
            while remaining:
                current = remaining.pop(0)
                component = [current]
                changed = True
                while changed:
                    changed = False
                    for idx in range(len(remaining) - 1, -1, -1):
                        candidate = remaining[idx]
                        if any(self._bbox_intersects(candidate["bbox"], item["bbox"], 6.0) for item in component):
                            component.append(candidate)
                            remaining.pop(idx)
                            changed = True
                components.append(component)

            for component in components:
                if len(component) < 6:
                    continue
                rectangle = self._fit_rectangle_component(page, component, y_offset)
                if not rectangle:
                    continue
                overrides.append(rectangle)

        overrides, consumed = self._dedupe_override_rectangles(overrides)
        for override in overrides:
            consumed.update(
                candidate["index"]
                for candidate in candidates
                if candidate["index"] not in consumed
                and self._candidate_matches_override(candidate, override)
            )
        return consumed, overrides

    def _bbox_intersects(self, bbox1: tuple[float, float, float, float],
                         bbox2: tuple[float, float, float, float],
                         padding: float = 0.0) -> bool:
        return not (
            bbox1[2] + padding < bbox2[0]
            or bbox1[0] - padding > bbox2[2]
            or bbox1[3] + padding < bbox2[1]
            or bbox1[1] - padding > bbox2[3]
        )

    def _fit_rectangle_component(self, page, component: list[dict],
                                 y_offset: float) -> Optional[dict]:
        entries = []
        for item in component:
            attribs = item["attribs"]
            path = item["path"]
            if path.get("fill"):
                entry = self._try_collapse_fill_to_line(page, path, attribs, y_offset)
                if entry:
                    entries.append(entry)
            else:
                segments = self._path_line_segments(page, path.get("items", []), y_offset)
                ordered = self._ordered_path_points(segments, tolerance=1.5)
                if len(ordered) >= 2:
                    entry = self._collect_line_entry(ordered[0], ordered[-1], attribs)
                    if entry:
                        entries.append(entry)

        if len(entries) < 4:
            return None

        # 直接用端点连接构建闭合多边形，而非线性拟合+交线计算
        polylines = self._merge_entry_group(entries, tolerance=15.0)
        best = None
        for polyline in polylines:
            if not polyline["closed"]:
                continue
            pts = polyline["points"]
            if len(pts) < 3:
                continue
            area = self._polygon_area(pts)
            if area <= 1.0:
                continue
            if best is None or area > self._polygon_area(best):
                best = pts

        if best is None:
            return None

        points = list(best)
        area = self._polygon_area(points)

        attribs = dict(component[0]["attribs"])
        attribs["lineweight"] = max(
            entry["attribs"].get("lineweight", 0) or 0
            for entry in entries
        )
        estimated_lineweight = False
        if attribs["lineweight"] <= 0 and area >= 1000.0 and len(component) >= 4:
            attribs["lineweight"] = 25
            estimated_lineweight = True
        return {
            "points": points,
            "closed": True,
            "attribs": attribs,
            "source_indices": {item["index"] for item in component},
            "source_count": len(component),
            "estimated_lineweight": estimated_lineweight,
        }

    def _fit_parallel_sides(self, entries: list[dict]) -> Optional[list[tuple[float, float, float]]]:
        if len(entries) < 2:
            return None

        avg_ux = sum(entry["ux"] for entry in entries) / len(entries)
        avg_uy = sum(entry["uy"] for entry in entries) / len(entries)
        length = math.hypot(avg_ux, avg_uy)
        if length <= 1e-6:
            return None
        ux = avg_ux / length
        uy = avg_uy / length
        nx = -uy
        ny = ux

        projections = []
        for entry in entries:
            offset = ((entry["start"][0] + entry["end"][0]) * 0.5) * nx + ((entry["start"][1] + entry["end"][1]) * 0.5) * ny
            projections.append((offset, entry))

        low = min(projections, key=lambda item: item[0])[0]
        high = max(projections, key=lambda item: item[0])[0]
        if abs(high - low) <= 1.0:
            return None

        low_group = [entry for offset, entry in projections if abs(offset - low) <= max(4.0, abs(high - low) * 0.2)]
        high_group = [entry for offset, entry in projections if abs(offset - high) <= max(4.0, abs(high - low) * 0.2)]
        if not low_group or not high_group:
            return None

        low_offset = sum(
            ((entry["start"][0] + entry["end"][0]) * 0.5) * nx + ((entry["start"][1] + entry["end"][1]) * 0.5) * ny
            for entry in low_group
        ) / len(low_group)
        high_offset = sum(
            ((entry["start"][0] + entry["end"][0]) * 0.5) * nx + ((entry["start"][1] + entry["end"][1]) * 0.5) * ny
            for entry in high_group
        ) / len(high_group)

        return [
            (nx, ny, low_offset),
            (nx, ny, high_offset),
        ]

    def _intersect_lines(self, line1: tuple[float, float, float],
                         line2: tuple[float, float, float]) -> Optional[tuple[float, float]]:
        a1, b1, c1 = line1
        a2, b2, c2 = line2
        det = a1 * b2 - a2 * b1
        if abs(det) <= 1e-6:
            return None
        x = (c1 * b2 - c2 * b1) / det
        y = (a1 * c2 - a2 * c1) / det
        return (x, y)

    def _rectangle_meta(self, override: dict) -> dict:
        points = override["points"]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        edges = [
            _distance(points[idx], points[(idx + 1) % len(points)])
            for idx in range(len(points))
        ]
        longest_idx = max(range(len(edges)), key=lambda idx: edges[idx])
        start = points[longest_idx]
        end = points[(longest_idx + 1) % len(points)]
        angle = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0])) % 180.0
        bbox = (min(xs), min(ys), max(xs), max(ys))
        return {
            "bbox": bbox,
            "center": ((bbox[0] + bbox[2]) * 0.5, (bbox[1] + bbox[3]) * 0.5),
            "diag": math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]),
            "area": self._polygon_area(points),
            "angle": angle,
        }

    def _rectangle_similarity(self, left: dict, right: dict) -> bool:
        left_meta = self._rectangle_meta(left)
        right_meta = self._rectangle_meta(right)

        if not self._bbox_intersects(left_meta["bbox"], right_meta["bbox"], 20.0):
            return False

        max_diag = max(left_meta["diag"], right_meta["diag"], 1.0)
        center_gap = _distance(left_meta["center"], right_meta["center"])
        if center_gap > max(18.0, max_diag * 0.08):
            return False

        area_ratio = min(left_meta["area"], right_meta["area"]) / max(left_meta["area"], right_meta["area"], 1.0)
        if area_ratio < 0.7:
            return False

        angle_gap = abs(((left_meta["angle"] - right_meta["angle"] + 90.0) % 180.0) - 90.0)
        if angle_gap > 6.0:
            return False

        return True

    def _rectangle_layer_penalty(self, layer_name: str) -> int:
        name = (layer_name or "").lower()
        penalties = (
            "中心线",
            "center",
            "road-cent",
            "红线",
        )
        return sum(1 for token in penalties if token in name)

    def _rectangle_priority(self, override: dict) -> tuple:
        attribs = override["attribs"]
        meta = self._rectangle_meta(override)
        lineweight = attribs.get("lineweight", 0) or 0
        source_count = override.get("source_count", len(override.get("source_indices", ())))
        return (
            1 if lineweight > 0 else 0,
            0 if override.get("estimated_lineweight") else 1,
            source_count,
            lineweight,
            -self._rectangle_layer_penalty(attribs.get("layer", "")),
            meta["area"],
        )

    def _candidate_matches_override(self, candidate: dict, override: dict) -> bool:
        meta = self._rectangle_meta(override)
        bbox = candidate["bbox"]
        if not self._bbox_intersects(bbox, meta["bbox"], 18.0):
            return False

        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        diag = math.hypot(width, height)
        if diag <= 4.0 or diag > max(meta["diag"] * 0.9, 40.0):
            return False

        center = ((bbox[0] + bbox[2]) * 0.5, (bbox[1] + bbox[3]) * 0.5)
        edge_gap = min(
            _point_to_segment_distance(
                center,
                override["points"][idx],
                override["points"][(idx + 1) % len(override["points"])],
            )
            for idx in range(len(override["points"]))
        )
        if edge_gap > max(10.0, diag * 0.6):
            return False

        return True

    def _dedupe_override_rectangles(self, overrides: list[dict]) -> tuple[list[dict], set[int]]:
        if not overrides:
            return [], set()

        families = []
        for override in overrides:
            matched_family = None
            for family in families:
                if any(self._rectangle_similarity(override, member) for member in family["members"]):
                    matched_family = family
                    break
            if matched_family is None:
                matched_family = {"members": []}
                families.append(matched_family)
            matched_family["members"].append(override)

        kept = []
        consumed = set()
        for family in families:
            members = family["members"]
            best = max(members, key=self._rectangle_priority)
            merged_indices = set()
            merged_count = 0
            for member in members:
                merged_indices.update(member.get("source_indices", set()))
                merged_count += member.get("source_count", len(member.get("source_indices", ())))
            best["source_indices"] = merged_indices
            best["source_count"] = merged_count
            consumed.update(merged_indices)
            kept.append(best)

        return kept, consumed

    def _extract_vectors(self, page, msp, layer_mgr: LayerManager,
                         page_num: int, page_height: float, y_offset: float):
        """提取页面中的矢量图形"""
        paths = page.get_drawings()
        deferred_lines = []
        consumed_paths, rectangle_overrides = self._build_rectangle_overrides(
            page, layer_mgr, page_num, y_offset
        )
        for override in rectangle_overrides:
            self._add_polyline(
                msp,
                override["points"],
                override["attribs"],
                close=override.get("closed", False),
            )

        for path_index, path in enumerate(paths):
            if path_index in consumed_paths:
                continue
            items = path.get("items", [])
            # 提取样式属性
            stroke_color = _fitz_color_to_rgb_int(path.get("color"))
            fill_color = _fitz_color_to_rgb_int(path.get("fill"))
            width = path.get("width", 0)

            base_attribs = layer_mgr.get_dxf_attribs("vector", page_num)
            # 直接从path的layer字段获取PDF图层
            if self.layer_strategy == LAYER_STRATEGY_PDF:
                pdf_layer = path.get('layer')
                if pdf_layer:
                    base_attribs["layer"] = f"PDF_{pdf_layer}"
            if self.preserve_colors and stroke_color:
                base_attribs["color"] = _rgb_to_dxf_color(stroke_color)
            if width > 0:
                base_attribs["lineweight"] = self._scaled_lineweight(width)
            # Phase 1: 填充路径→始终输出 HATCH（与 AutoCAD 一致）
            if path.get("fill"):
                self._emit_fill_as_hatch(page, path, msp, base_attribs, y_offset)

            # Phase 2: 描边处理（不跳过，填充路径也继续处理以生成 LWPOLYLINE）
            entry = self._try_collapse_fill_to_line(page, path, base_attribs, y_offset)
            if entry:
                deferred_lines.append(entry)
                continue
            entry = self._try_merge_dashed_segments(page, path, base_attribs, y_offset)
            if entry:
                deferred_lines.append(entry)
                continue
            if self._try_add_connected_polyline(page, path, msp, base_attribs, y_offset):
                continue

            # 收集连续贝塞尔曲线用于圆检测
            consecutive_curves = []

            for item in items:
                if self._cancel_flag:
                    return

                if item[0] == 'l':  # 线段
                    self._flush_curves(consecutive_curves, msp, base_attribs,
                                       page_height, y_offset)
                    consecutive_curves = []

                    p1, p2 = item[1], item[2]
                    x1, y1 = self._transform_point(page, p1.x, p1.y, y_offset)
                    x2, y2 = self._transform_point(page, p2.x, p2.y, y_offset)
                    entry = self._collect_line_entry((x1, y1), (x2, y2), base_attribs)
                    if entry:
                        deferred_lines.append(entry)

                elif item[0] == 're':  # 矩形
                    self._flush_curves(consecutive_curves, msp, base_attribs,
                                       page_height, y_offset)
                    consecutive_curves = []

                    rect = item[1]
                    points = self._transform_rect_to_points(page, rect, y_offset)
                    self._add_polyline(msp, points, base_attribs)

                elif item[0] == 'c':  # 三次贝塞尔曲线
                    p0, p1, p2, p3 = item[1], item[2], item[3], item[4]
                    # 转换坐标
                    c0 = self._transform_point(page, p0.x, p0.y, y_offset)
                    c1 = self._transform_point(page, p1.x, p1.y, y_offset)
                    c2 = self._transform_point(page, p2.x, p2.y, y_offset)
                    c3 = self._transform_point(page, p3.x, p3.y, y_offset)
                    consecutive_curves.append((c0, c1, c2, c3))

                elif item[0] == 'qu':  # 四边形 (Quad)
                    self._flush_curves(consecutive_curves, msp, base_attribs,
                                       page_height, y_offset)
                    consecutive_curves = []

                    quad = item[1]
                    points = [
                        self._transform_point(page, quad.ul.x, quad.ul.y, y_offset),
                        self._transform_point(page, quad.ur.x, quad.ur.y, y_offset),
                        self._transform_point(page, quad.lr.x, quad.lr.y, y_offset),
                        self._transform_point(page, quad.ll.x, quad.ll.y, y_offset),
                        self._transform_point(page, quad.ul.x, quad.ul.y, y_offset),
                    ]
                    self._add_polyline(msp, points, base_attribs)

            # 处理路径末尾的曲线
            self._flush_curves(consecutive_curves, msp, base_attribs,
                               page_height, y_offset)

        self._emit_line_clusters(deferred_lines, msp)

    def _flush_curves(self, curves: list, msp, attribs: dict,
                      page_height: float, y_offset: float):
        """处理积累的连续贝塞尔曲线"""
        if not curves:
            return

        # 1. 尝试识别为完整圆
        if len(curves) >= 3:
            circle = detect_circle_from_beziers(curves)
            if circle:
                msp.add_circle(
                    circle["center"], circle["radius"],
                    dxfattribs=attribs
                )
                curves.clear()
                return

        # 2. 逐段处理
        for c0, c1, c2, c3 in curves:
            if self.curve_mode == CURVE_MODE_SPLINE:
                # 尝试识别为圆弧
                arc = detect_arc_from_bezier(c0, c1, c2, c3)
                if arc:
                    msp.add_arc(
                        arc["center"], arc["radius"],
                        arc["start_angle"], arc["end_angle"],
                        dxfattribs=attribs
                    )
                else:
                    # 用样条曲线
                    try:
                        msp.add_spline(
                            fit_points=[Vec3(*c0), Vec3(*c1), Vec3(*c2), Vec3(*c3)],
                            dxfattribs=attribs
                        )
                    except Exception:
                        # 降级为多段线
                        pts = bezier_to_polyline_points(c0, c1, c2, c3, 20)
                        self._add_polyline(msp, pts, attribs)

            elif self.curve_mode == CURVE_MODE_POLYLINE:
                pts = bezier_to_polyline_points(c0, c1, c2, c3, 20)
                self._add_polyline(msp, pts, attribs)

            else:  # CURVE_MODE_LINE
                self._add_polyline(msp, [c0, c3], attribs)

        curves.clear()

    def _extract_text(self, page, msp, layer_mgr: LayerManager,
                      page_num: int, page_height: float, y_offset: float):
        """提取页面文字（输出 MTEXT，与 AutoCAD 一致）"""
        text_dict = page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)

        for block in text_dict.get('blocks', []):
            if block.get('type') != 0:  # 非文本块
                continue

            for line in block.get('lines', []):
                for span in line.get('spans', []):
                    text = span.get('text', '').strip()
                    if not text:
                        continue

                    bbox = span.get('bbox', [0, 0, 0, 0])
                    font_size = span.get('size', 12)

                    x, y = self._transform_point(page, bbox[0], bbox[1], y_offset)

                    # 文字颜色
                    color_int = span.get('color', 0)
                    r = (color_int >> 16) & 0xFF
                    g = (color_int >> 8) & 0xFF
                    b = color_int & 0xFF

                    extra = {
                        'char_height': font_size * 0.35,
                        'style': 'CHINESE',
                        'attachment_point': 7,
                    }
                    if self.preserve_colors and (r + g + b) > 0:
                        extra['color'] = _rgb_to_dxf_color((r, g, b))

                    attribs = layer_mgr.get_dxf_attribs(
                        "text", page_num, extra)
                    mtext = msp.add_mtext(text, dxfattribs=attribs)
                    mtext.set_location(insert=(x, y - font_size))

    def _extract_images(self, doc, page, msp, layer_mgr: LayerManager,
                        page_num: int, page_height: float, y_offset: float):
        """提取页面中嵌入的位图图片"""
        image_list = page.get_images()

        for img_idx, img_info in enumerate(image_list):
            try:
                xref = img_info[0]
                # 获取图片在页面上的位置
                rects = page.get_image_rects(xref)
                if not rects:
                    continue

                rect = rects[0]  # 取第一个位置

                # 提取图片数据
                pix = fitz.Pixmap(doc, xref)
                if pix.n > 4:  # CMYK
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                # 保存为临时png
                img_dir = tempfile.mkdtemp(prefix="pdf2dxf_img_")
                img_path = os.path.join(img_dir, f"page{page_num}_img{img_idx}.png")
                pix.save(img_path)

                # 在DXF中添加IMAGE引用
                # 注意：DXF IMAGE实体需要外部文件引用
                corners = [
                    self._transform_point(page, rect.x0, rect.y0, y_offset),
                    self._transform_point(page, rect.x1, rect.y0, y_offset),
                    self._transform_point(page, rect.x1, rect.y1, y_offset),
                    self._transform_point(page, rect.x0, rect.y1, y_offset),
                ]
                xs = [point[0] for point in corners]
                ys = [point[1] for point in corners]
                x0 = min(xs)
                y0 = min(ys)
                width = max(xs) - x0
                height = max(ys) - y0

                attribs = layer_mgr.get_dxf_attribs("image", page_num)

                # 用矩形标记图片区域（并添加注释）
                points = [
                    (x0, y0), (x0 + width, y0),
                    (x0 + width, y0 + height), (x0, y0 + height),
                    (x0, y0)
                ]
                msp.add_lwpolyline(
                    points,
                    dxfattribs={**attribs, "color": 3}  # 绿色边框表示图片
                )
                # 在图片区域中心添加标注
                cx = x0 + width / 2
                cy = y0 + height / 2
                msp.add_text(
                    f"[图片: {os.path.basename(img_path)}]",
                    dxfattribs={
                        **attribs,
                        'insert': (cx - width * 0.3, cy),
                        'height': min(height * 0.1, 12),
                        'style': 'CHINESE',
                        'color': 3,
                    }
                )

                pix = None  # 释放

            except Exception:
                continue


def convert_pdf_to_dxf(
    input_path: str,
    output_path: str,
    curve_mode: str = CURVE_MODE_SPLINE,
    layer_strategy: str = LAYER_STRATEGY_NONE,
    dxf_version: str = "R2018",
    preserve_colors: bool = True,
    extract_images: bool = False,
    page_range: str = None,
    progress_callback=None,
) -> str:
    """
    便捷函数：转换PDF为DXF

    Args:
        input_path: PDF输入路径
        output_path: DXF输出路径
        curve_mode: 曲线模式(spline/polyline/line)
        layer_strategy: 图层策略(none/content/page)
        dxf_version: DXF版本
        preserve_colors: 保留颜色
        extract_images: 提取图片
        page_range: 页码范围(如"1-5,8")
        progress_callback: 进度回调

    Returns:
        输出文件路径
    """
    converter = PdfToDxfConverter(
        curve_mode=curve_mode,
        layer_strategy=layer_strategy,
        dxf_version=dxf_version,
        preserve_colors=preserve_colors,
        extract_images=extract_images,
        page_range=page_range,
    )
    if progress_callback:
        converter.set_progress_callback(progress_callback)

    converter.convert(input_path, output_path)
    return output_path
