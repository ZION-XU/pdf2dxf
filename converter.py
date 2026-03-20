"""PDF转DXF转换引擎核心

策略（与 AutoCAD PDF 导入一致）：
  - 逐路径独立处理，不做跨路径合并
  - 填充 → HATCH + 描边 LWPOLYLINE
  - 描边 → LWPOLYLINE（逐path内连通链输出）
  - 贝塞尔 → ARC / CIRCLE 识别，未识别则折线逼近
"""

import math
import os
import tempfile
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


# ═══════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════

# 完整 ACI 256 色表（索引 → RGB）
_ACI_TABLE = {
    1: (255,0,0), 2: (255,255,0), 3: (0,255,0), 4: (0,255,255),
    5: (0,0,255), 6: (255,0,255), 7: (255,255,255),
    8: (128,128,128), 9: (192,192,192),
    10: (255,0,0), 11: (255,127,127), 12: (204,0,0), 13: (204,102,102),
    14: (153,0,0), 15: (153,76,76), 16: (127,0,0), 17: (127,63,63),
    18: (76,0,0), 19: (76,38,38),
    20: (255,63,0), 21: (255,159,127), 22: (204,51,0), 23: (204,127,102),
    24: (153,38,0), 25: (153,95,76), 26: (127,31,0), 27: (127,79,63),
    28: (76,19,0), 29: (76,47,38),
    30: (255,127,0), 31: (255,191,127), 32: (204,102,0), 33: (204,153,102),
    34: (153,76,0), 35: (153,114,76), 36: (127,63,0), 37: (127,95,63),
    38: (76,38,0), 39: (76,57,38),
    40: (255,191,0), 41: (255,223,127), 42: (204,153,0), 43: (204,178,102),
    44: (153,114,0), 45: (153,133,76), 46: (127,95,0), 47: (127,111,63),
    48: (76,57,0), 49: (76,66,38),
    50: (255,255,0), 51: (255,255,127), 52: (204,204,0), 53: (204,204,102),
    54: (153,153,0), 55: (153,153,76), 56: (127,127,0), 57: (127,127,63),
    58: (76,76,0), 59: (76,76,38),
    60: (191,255,0), 61: (223,255,127), 62: (153,204,0), 63: (178,204,102),
    70: (127,255,0), 71: (191,255,127), 72: (102,204,0), 73: (153,204,102),
    80: (63,255,0), 81: (159,255,127), 82: (51,204,0), 83: (127,204,102),
    90: (0,255,0), 91: (127,255,127), 92: (0,204,0), 93: (102,204,102),
    100: (0,255,63), 101: (127,255,159), 102: (0,204,51), 103: (102,204,127),
    110: (0,255,127), 111: (127,255,191), 112: (0,204,102), 113: (102,204,153),
    120: (0,255,191), 121: (127,255,223), 122: (0,204,153), 123: (102,204,178),
    130: (0,255,255), 131: (127,255,255), 132: (0,204,204), 133: (102,204,204),
    140: (0,191,255), 141: (127,223,255), 142: (0,153,204), 143: (102,178,204),
    150: (0,127,255), 151: (127,191,255), 152: (0,102,204), 153: (102,153,204),
    160: (0,63,255), 161: (127,159,255), 162: (0,51,204), 163: (102,127,204),
    170: (0,0,255), 171: (127,127,255), 172: (0,0,204), 173: (102,102,204),
    180: (63,0,255), 181: (159,127,255), 182: (51,0,204), 183: (127,102,204),
    190: (127,0,255), 191: (191,127,255), 192: (102,0,204), 193: (153,102,204),
    200: (191,0,255), 201: (223,127,255), 202: (153,0,204), 203: (178,102,204),
    210: (255,0,255), 211: (255,127,255), 212: (204,0,204), 213: (204,102,204),
    220: (255,0,191), 221: (255,127,223), 222: (204,0,153), 223: (204,102,178),
    230: (255,0,127), 231: (255,127,191), 232: (204,0,102), 233: (204,102,153),
    240: (255,0,63), 241: (255,127,159), 242: (204,0,51), 243: (204,102,127),
    250: (51,51,51), 251: (91,91,91), 252: (132,132,132),
    253: (173,173,173), 254: (214,214,214), 255: (242,242,242),
}


def _rgb_to_dxf_color(rgb: tuple) -> int:
    """将RGB颜色映射到最接近的DXF ACI颜色索引(1-255)"""
    if rgb is None or len(rgb) < 3:
        return 7
    r, g, b = rgb[0], rgb[1], rgb[2]
    if r + g + b < 30:
        return 7
    best, min_dist = 7, float('inf')
    for idx, (cr, cg, cb) in _ACI_TABLE.items():
        dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if dist < min_dist:
            min_dist = dist
            best = idx
    return best


def _fitz_color_to_rgb_int(color: tuple) -> Optional[tuple]:
    """将fitz颜色(0-1浮点)转为(0-255整数)"""
    if color is None or len(color) < 3:
        return None
    return (int(color[0] * 255), int(color[1] * 255), int(color[2] * 255))


def _linewidth_mm_to_dxf(width_pt: float) -> float:
    """将点(pt)线宽转为DXF线宽（百分之一毫米）"""
    mm = width_pt * 0.3528
    standard = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50,
                53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
    mm_100 = int(mm * 100)
    return min(standard, key=lambda x: abs(x - mm_100))


def _distance(p1, p2):
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def _is_close(p1, p2, tol=1.0):
    return _distance(p1, p2) <= tol


# ═══════════════════════════════════════════════════════════════
#  转换器
# ═══════════════════════════════════════════════════════════════

class PdfToDxfConverter:
    """PDF转DXF转换器"""

    STROKE_WIDTH_SCALE = 1.8
    PDF_PT_TO_MM = 25.4 / 72.0  # PDF点转毫米

    def __init__(
        self,
        curve_mode: str = CURVE_MODE_SPLINE,
        layer_strategy: str = LAYER_STRATEGY_NONE,
        dxf_version: str = "R2018",
        preserve_colors: bool = True,
        extract_images: bool = False,
        page_range: Optional[str] = None,
        dxf_scale: Optional[float] = None,
    ):
        self.curve_mode = curve_mode
        self.layer_strategy = layer_strategy
        self.dxf_version = dxf_version
        self.preserve_colors = preserve_colors
        self.extract_images = extract_images
        self.page_range = page_range
        # 缩放因子：默认 2.956 匹配 AutoCAD PDF 导入缩放
        self.dxf_scale = dxf_scale if dxf_scale is not None else 2.956
        self._progress_callback = None
        self._cancel_flag = False

    def set_progress_callback(self, callback):
        self._progress_callback = callback

    def cancel(self):
        self._cancel_flag = True

    def _report_progress(self, current: int, total: int, message: str):
        if self._progress_callback:
            self._progress_callback(current, total, message)

    def _parse_page_range(self, total_pages: int) -> list:
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

    # ─── 主入口 ───

    def convert(self, input_path: str, output_path: str):
        self._cancel_flag = False
        doc = fitz.open(input_path)
        total_pages = doc.page_count
        pages_to_convert = self._parse_page_range(total_pages)

        dwg = ezdxf.new(self.dxf_version)
        msp = dwg.modelspace()
        self._ensure_linetypes(dwg)

        try:
            dwg.styles.add("CHINESE", font=DEFAULT_FONT)
        except ezdxf.DXFTableEntryError:
            pass

        layer_mgr = LayerManager(dwg, self.layer_strategy)

        total = len(pages_to_convert)
        for idx, page_num in enumerate(pages_to_convert):
            if self._cancel_flag:
                doc.close()
                return

            self._report_progress(idx, total, f"正在转换第 {page_num + 1} 页...")
            page = doc[page_num]
            page_height = page.rect.height
            y_offset = idx * page_height

            if self.layer_strategy == LAYER_STRATEGY_PDF:
                self._build_ocg_layer_maps(doc, page, layer_mgr)

            self._extract_vectors(page, msp, layer_mgr, page_num, page_height, y_offset)
            self._extract_text(page, msp, layer_mgr, page_num, page_height, y_offset)

            if self.extract_images:
                self._extract_images(doc, page, msp, layer_mgr, page_num, page_height, y_offset)

        self._report_progress(total, total, "正在保存DXF文件...")
        doc.close()
        dwg.saveas(output_path)
        self._report_progress(total, total, "转换完成")

    # ─── 图层与线型 ───

    def _build_ocg_layer_maps(self, doc, page, layer_mgr: LayerManager):
        """构建 PDF OCG 图层映射，统计主要描边色作为图层颜色"""
        layer_color_stats = {}  # layer_name -> {aci_color: count}
        for path in page.get_drawings():
            name = path.get('layer')
            if not name:
                continue
            dxf_layer = f"PDF_{name}"
            if dxf_layer not in layer_color_stats:
                layer_color_stats[dxf_layer] = {}
            stroke_color = _fitz_color_to_rgb_int(path.get('color'))
            if stroke_color:
                aci = _rgb_to_dxf_color(stroke_color)
                layer_color_stats[dxf_layer][aci] = layer_color_stats[dxf_layer].get(aci, 0) + 1

        for dxf_layer, color_counts in layer_color_stats.items():
            if color_counts:
                dominant_color = max(color_counts, key=color_counts.get)
            else:
                dominant_color = 7
            layer_mgr.create_pdf_layer(dxf_layer, color=dominant_color)

        # AutoCAD 把 PDF 文字统一放到 PDF_文字 图层
        layer_mgr.create_pdf_layer("PDF_文字", color=5)

    def _ensure_linetypes(self, dwg):
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

    # ─── 坐标变换 ───

    def _transform_point(self, page, x: float, y: float, y_offset: float = 0.0):
        point = fitz.Point(x, y) * page.rotation_matrix
        s = self.dxf_scale
        return (float(point.x * s), float((page.rect.height - point.y) * s + y_offset))

    def _transform_rect(self, page, rect, y_offset: float):
        """矩形 → 5个点的闭合多边形"""
        corners = [(rect.x0, rect.y0), (rect.x1, rect.y0),
                   (rect.x1, rect.y1), (rect.x0, rect.y1)]
        pts = [self._transform_point(page, x, y, y_offset) for x, y in corners]
        pts.append(pts[0])
        return pts

    def _transform_quad(self, page, quad, y_offset: float):
        """四边形 → 5个点的闭合多边形"""
        return [
            self._transform_point(page, quad.ul.x, quad.ul.y, y_offset),
            self._transform_point(page, quad.ur.x, quad.ur.y, y_offset),
            self._transform_point(page, quad.lr.x, quad.lr.y, y_offset),
            self._transform_point(page, quad.ll.x, quad.ll.y, y_offset),
            self._transform_point(page, quad.ul.x, quad.ul.y, y_offset),
        ]

    # ─── 工具方法 ───

    def _dedupe_points(self, points, tolerance=1.0):
        unique = []
        for p in points:
            if not unique or not _is_close(unique[-1], p, tolerance):
                unique.append(p)
        if len(unique) > 1 and _is_close(unique[0], unique[-1], tolerance):
            unique.pop()
        return unique

    def _add_polyline(self, msp, points, attribs, tolerance=0.01, close=None):
        is_closed = len(points) > 2 and _is_close(points[0], points[-1], tolerance)
        if close is not None:
            is_closed = close
        # 只有 2 点的线段不做去重（保留极短的虚线段等）
        if len(points) <= 2:
            if len(points) < 2:
                return
            msp.add_lwpolyline(points, close=False, dxfattribs=attribs)
            return
        pts = self._dedupe_points(points, tolerance)
        if len(pts) < 2:
            return
        msp.add_lwpolyline(pts, close=is_closed, dxfattribs=attribs)

    # ═══════════════════════════════════════════════════════════
    #  核心：矢量提取（逐路径独立处理）
    # ═══════════════════════════════════════════════════════════

    def _extract_vectors(self, page, msp, layer_mgr: LayerManager,
                         page_num: int, page_height: float, y_offset: float):
        """提取矢量图形 — 每条 PDF path 独立处理"""

        for path in page.get_drawings():
            if self._cancel_flag:
                return

            items = path.get("items", [])
            if not items:
                continue

            # 提取属性
            base_attribs = layer_mgr.get_dxf_attribs("vector", page_num)
            if self.layer_strategy == LAYER_STRATEGY_PDF:
                pdf_layer = path.get('layer')
                if pdf_layer:
                    base_attribs["layer"] = f"PDF_{pdf_layer}"

            # 颜色策略：与图层色相同则 BYLAYER，不同则设具体色
            stroke_color = _fitz_color_to_rgb_int(path.get("color"))

            width = path.get("width", 0)
            if width > 0:
                base_attribs["lineweight"] = _linewidth_mm_to_dxf(
                    width * self.STROKE_WIDTH_SCALE)

            has_fill = path.get("fill") is not None
            has_stroke = path.get("color") is not None

            # 填充 → HATCH（有 stroke 时 HATCH 用 stroke_color 代替 BYLAYER）
            if has_fill:
                hatch_attribs = dict(base_attribs)
                if has_stroke and self.preserve_colors and stroke_color:
                    aci = _rgb_to_dxf_color(stroke_color)
                    layer_color = layer_mgr.get_layer_color(hatch_attribs.get("layer", "0"))
                    if aci != layer_color:
                        hatch_attribs["_hatch_color"] = aci
                self._emit_hatch(page, path, msp, hatch_attribs, y_offset, layer_mgr)

            # 仅描边（无填充） → LWPOLYLINE / ARC / CIRCLE
            elif has_stroke:
                if self.preserve_colors and stroke_color:
                    aci = _rgb_to_dxf_color(stroke_color)
                    layer_color = layer_mgr.get_layer_color(base_attribs.get("layer", "0"))
                    if aci != layer_color:
                        base_attribs["color"] = aci
                self._emit_stroke(page, path, msp, base_attribs,
                                  page_height, y_offset)

    # ─── 填充 → HATCH ───

    def _emit_hatch(self, page, path, msp, attribs, y_offset, layer_mgr=None):
        """填充路径 → SOLID HATCH（与AutoCAD一致：每个碎片独立EdgePath）"""
        items = path.get("items", [])
        if not items:
            return

        item_types = set(item[0] for item in items)

        # 矩形/四边形 → 直接构建边界
        if item_types <= {'re'} and len(items) == 1:
            pts = self._transform_rect(page, items[0][1], y_offset)
            pts_d = self._dedupe_points(pts, 0.25)
            if len(pts_d) >= 3:
                self._create_hatch(msp, pts_d, path, attribs, layer_mgr)
            return
        if item_types <= {'qu'} and len(items) == 1:
            pts = self._transform_quad(page, items[0][1], y_offset)
            pts_d = self._dedupe_points(pts, 0.25)
            if len(pts_d) >= 3:
                self._create_hatch(msp, pts_d, path, attribs, layer_mgr)
            return

        # 收集所有端点并全局去重
        all_pts = []
        for item in items:
            cmd = item[0]
            if cmd == 'l':
                all_pts.append(self._transform_point(page, item[1].x, item[1].y, y_offset))
                all_pts.append(self._transform_point(page, item[2].x, item[2].y, y_offset))
            elif cmd == 'c':
                all_pts.append(self._transform_point(page, item[1].x, item[1].y, y_offset))
                all_pts.append(self._transform_point(page, item[4].x, item[4].y, y_offset))
            elif cmd == 're':
                all_pts.extend(self._transform_rect(page, item[1], y_offset))
            elif cmd == 'qu':
                q = item[1]
                for c in [q.ul, q.ur, q.lr, q.ll]:
                    all_pts.append(self._transform_point(page, c.x, c.y, y_offset))

        unique = []
        for p in all_pts:
            if not any(_is_close(p, u, 0.1) for u in unique):
                unique.append(p)
        if len(unique) < 3:
            return

        # 尝试粗线矩形检测：8+点配对→中心线+线宽LWPOLYLINE
        if len(unique) >= 8 and len(unique) % 2 == 0:
            if self._try_thick_polyline(unique, msp, path, attribs, layer_mgr):
                return

        # 普通碎片填充 → EdgePath+LineEdge HATCH
        cx = sum(p[0] for p in unique) / len(unique)
        cy = sum(p[1] for p in unique) / len(unique)
        ordered = sorted(unique, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
        self._create_hatch_edge(msp, ordered, path, attribs, layer_mgr)

    def _try_thick_polyline(self, points, msp, path, attribs, layer_mgr=None):
        """检测内外角点对 → 中心线+线宽的LWPOLYLINE"""
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)

        # 按角度排序
        sorted_pts = sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

        # 贪心配对最近邻
        n = len(sorted_pts)
        used = [False] * n
        pairs = []
        for i in range(n):
            if used[i]:
                continue
            best_j, best_d = -1, float('inf')
            for j in range(n):
                if i == j or used[j]:
                    continue
                d = _distance(sorted_pts[i], sorted_pts[j])
                if d < best_d:
                    best_d = d
                    best_j = j
            if best_j < 0 or best_d > 5.0:
                return False
            pairs.append((sorted_pts[i], sorted_pts[best_j], best_d))
            used[i] = True
            used[best_j] = True

        if len(pairs) < 4:
            return False

        # 计算每对的中心点和宽度
        centers = []
        widths = []
        for p1, p2, d in pairs:
            centers.append(((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2))
            widths.append(d)

        # 按角度排序中心点（保持对应的宽度）
        center_angle = [(math.atan2(c[1] - cy, c[0] - cx), c, w) for c, w in zip(centers, widths)]
        center_angle.sort(key=lambda x: x[0])
        centers = [ca[1] for ca in center_angle]
        widths = [ca[2] for ca in center_angle]

        # 创建带线宽的LWPOLYLINE
        ha = dict(attribs)
        hatch_color = ha.pop("_hatch_color", None)
        ha.pop("color", None)
        ha.pop("lineweight", None)
        poly = msp.add_lwpolyline(centers, close=True, dxfattribs=ha)
        if hatch_color:
            poly.dxf.color = hatch_color
        else:
            poly.dxf.discard('color')
        # 设置每个顶点的线宽（start_width = end_width = 对应角点间距）
        pts_data = []
        for i, (c, w) in enumerate(zip(centers, widths)):
            pts_data.append((c[0], c[1], w, w, 0))
        poly.set_points(pts_data, format='xyseb')
        poly.close()
        return True

    def _create_hatch_edge(self, msp, points, path, attribs, layer_mgr=None):
        """创建 SOLID HATCH - 用EdgePath+LineEdge（与AutoCAD一致）"""
        if len(points) < 3:
            return
        ha = dict(attribs)
        hatch_color = ha.pop("_hatch_color", None)
        ha.pop("color", None)
        ha.pop("lineweight", None)
        hatch = msp.add_hatch(dxfattribs=ha)
        if hatch_color:
            hatch.dxf.color = hatch_color
        else:
            hatch.dxf.discard('color')
        edge_path = hatch.paths.add_edge_path()
        for i in range(len(points)):
            p1 = points[i]
            p2 = points[(i + 1) % len(points)]
            edge_path.add_line(p1, p2)

    def _create_hatch(self, msp, points, path, attribs, layer_mgr=None):
        """创建 SOLID HATCH 实体（polyline边界）"""
        ha = dict(attribs)
        hatch_color = ha.pop("_hatch_color", None)
        ha.pop("color", None)
        ha.pop("lineweight", None)
        hatch = msp.add_hatch(dxfattribs=ha)
        if hatch_color:
            hatch.dxf.color = hatch_color
        else:
            hatch.dxf.discard('color')
        hatch.paths.add_polyline_path(points, is_closed=True)

    # ─── 描边 → LWPOLYLINE / ARC / CIRCLE ───

    def _emit_stroke(self, page, path, msp, attribs,
                     page_height, y_offset):
        """描边路径 → 逐path构建连通链直接输出"""
        items = path.get("items", [])
        if not items:
            return

        line_pts = []          # 正在积累的线段链
        curves = []            # 正在积累的贝塞尔链

        for item in items:
            cmd = item[0]

            if cmd == 'l':
                # 遇到线段 → 先刷新曲线
                if curves:
                    self._flush_curves(curves, msp, attribs, page_height, y_offset)
                    curves = []

                s = self._transform_point(page, item[1].x, item[1].y, y_offset)
                e = self._transform_point(page, item[2].x, item[2].y, y_offset)

                if not line_pts:
                    line_pts = [s, e]
                elif _is_close(line_pts[-1], s, 0.5):
                    line_pts.append(e)
                else:
                    # 不连通，输出当前链，开始新链
                    self._add_polyline(msp, line_pts, attribs)
                    line_pts = [s, e]

            elif cmd == 'c':
                # 遇到曲线 → 先刷新线段链
                if line_pts:
                    self._add_polyline(msp, line_pts, attribs)
                    line_pts = []

                c0 = self._transform_point(page, item[1].x, item[1].y, y_offset)
                c1 = self._transform_point(page, item[2].x, item[2].y, y_offset)
                c2 = self._transform_point(page, item[3].x, item[3].y, y_offset)
                c3 = self._transform_point(page, item[4].x, item[4].y, y_offset)
                curves.append((c0, c1, c2, c3))

            elif cmd == 're':
                if line_pts:
                    self._add_polyline(msp, line_pts, attribs)
                    line_pts = []
                if curves:
                    self._flush_curves(curves, msp, attribs, page_height, y_offset)
                    curves = []
                self._add_polyline(msp, self._transform_rect(page, item[1], y_offset), attribs)

            elif cmd == 'qu':
                if line_pts:
                    self._add_polyline(msp, line_pts, attribs)
                    line_pts = []
                if curves:
                    self._flush_curves(curves, msp, attribs, page_height, y_offset)
                    curves = []
                self._add_polyline(msp, self._transform_quad(page, item[1], y_offset), attribs)

        # 刷新尾部
        if line_pts:
            self._add_polyline(msp, line_pts, attribs)
        if curves:
            self._flush_curves(curves, msp, attribs, page_height, y_offset)

    # ─── 曲线处理 ───

    def _flush_curves(self, curves: list, msp, attribs: dict,
                      page_height: float, y_offset: float):
        """处理连续贝塞尔曲线 → CIRCLE / ARC / SPLINE"""
        if not curves:
            return

        if len(curves) >= 3:
            circle = detect_circle_from_beziers(curves)
            if circle:
                msp.add_circle(circle["center"], circle["radius"],
                               dxfattribs=attribs)
                curves.clear()
                return

        for c0, c1, c2, c3 in curves:
            if self.curve_mode == CURVE_MODE_SPLINE:
                arc = detect_arc_from_bezier(c0, c1, c2, c3)
                if arc:
                    msp.add_arc(arc["center"], arc["radius"],
                                arc["start_angle"], arc["end_angle"],
                                dxfattribs=attribs)
                else:
                    pts = bezier_to_polyline_points(c0, c1, c2, c3, 20)
                    self._add_polyline(msp, pts, attribs)
            elif self.curve_mode == CURVE_MODE_POLYLINE:
                pts = bezier_to_polyline_points(c0, c1, c2, c3, 20)
                self._add_polyline(msp, pts, attribs)
            else:
                self._add_polyline(msp, [c0, c3], attribs)

        curves.clear()

    # ─── 文字提取 ───

    def _extract_text(self, page, msp, layer_mgr: LayerManager,
                      page_num: int, page_height: float, y_offset: float):
        """提取文字 → MTEXT"""
        text_dict = page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)

        for block in text_dict.get('blocks', []):
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                for span in line.get('spans', []):
                    text = span.get('text', '').strip()
                    if not text:
                        continue

                    bbox = span.get('bbox', [0, 0, 0, 0])
                    font_size = span.get('size', 12)
                    # 使用 origin（文字基线原点）定位，比 bbox 更精确
                    origin = span.get('origin')
                    if origin:
                        x, y = self._transform_point(page, origin[0], origin[1], y_offset)
                    else:
                        x, y = self._transform_point(page, bbox[0], bbox[1], y_offset)

                    color_int = span.get('color', 0)
                    r = (color_int >> 16) & 0xFF
                    g = (color_int >> 8) & 0xFF
                    b = color_int & 0xFF

                    extra = {
                        'char_height': font_size * 0.35 * self.dxf_scale,
                        'style': 'CHINESE',
                        'attachment_point': 7,
                    }
                    text_attribs = layer_mgr.get_dxf_attribs("text", page_num, extra)
                    # PDF 策略下文字放入 PDF_文字 图层（与 AutoCAD 一致）
                    if self.layer_strategy == LAYER_STRATEGY_PDF:
                        text_attribs["layer"] = "PDF_文字"

                    # 颜色策略：与图层色相同则 BYLAYER
                    if self.preserve_colors:
                        aci = _rgb_to_dxf_color((r, g, b))
                        layer_color = layer_mgr.get_layer_color(
                            text_attribs.get("layer", "0"))
                        if aci != layer_color:
                            text_attribs['color'] = aci

                    mtext = msp.add_mtext(text, dxfattribs=text_attribs)
                    mtext.set_location(insert=(x, y))

    # ─── 图片提取 ───

    def _extract_images(self, doc, page, msp, layer_mgr: LayerManager,
                        page_num: int, page_height: float, y_offset: float):
        """提取嵌入位图"""
        for img_idx, img_info in enumerate(page.get_images()):
            try:
                xref = img_info[0]
                rects = page.get_image_rects(xref)
                if not rects:
                    continue

                rect = rects[0]
                pix = fitz.Pixmap(doc, xref)
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                img_dir = tempfile.mkdtemp(prefix="pdf2dxf_img_")
                img_path = os.path.join(img_dir, f"page{page_num}_img{img_idx}.png")
                pix.save(img_path)

                corners = [
                    self._transform_point(page, rect.x0, rect.y0, y_offset),
                    self._transform_point(page, rect.x1, rect.y0, y_offset),
                    self._transform_point(page, rect.x1, rect.y1, y_offset),
                    self._transform_point(page, rect.x0, rect.y1, y_offset),
                ]
                xs = [p[0] for p in corners]
                ys = [p[1] for p in corners]
                x0, y0 = min(xs), min(ys)
                w, h = max(xs) - x0, max(ys) - y0

                img_attribs = layer_mgr.get_dxf_attribs("image", page_num)
                border = [(x0, y0), (x0+w, y0), (x0+w, y0+h), (x0, y0+h), (x0, y0)]
                msp.add_lwpolyline(border, dxfattribs={**img_attribs, "color": 3})

                msp.add_text(
                    f"[图片: {os.path.basename(img_path)}]",
                    dxfattribs={
                        **img_attribs,
                        'insert': (x0 + w * 0.2, y0 + h * 0.5),
                        'height': min(h * 0.1, 12),
                        'style': 'CHINESE',
                        'color': 3,
                    })
                pix = None
            except Exception:
                continue


# ═══════════════════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════════════════

def convert_pdf_to_dxf(
    input_path: str,
    output_path: str,
    curve_mode: str = CURVE_MODE_SPLINE,
    layer_strategy: str = LAYER_STRATEGY_NONE,
    dxf_version: str = "R2018",
    preserve_colors: bool = True,
    extract_images: bool = False,
    page_range: str = None,
    dxf_scale: float = None,
    progress_callback=None,
) -> str:
    converter = PdfToDxfConverter(
        curve_mode=curve_mode,
        layer_strategy=layer_strategy,
        dxf_version=dxf_version,
        preserve_colors=preserve_colors,
        extract_images=extract_images,
        page_range=page_range,
        dxf_scale=dxf_scale,
    )
    if progress_callback:
        converter.set_progress_callback(progress_callback)
    converter.convert(input_path, output_path)
    return output_path
