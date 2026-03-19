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

def _rgb_to_dxf_color(rgb: tuple) -> int:
    """将RGB颜色映射到最接近的DXF颜色索引(1-7)"""
    if rgb is None or len(rgb) < 3:
        return 7
    r, g, b = rgb[0], rgb[1], rgb[2]
    colors = {
        1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0),
        4: (0, 255, 255), 5: (0, 0, 255), 6: (255, 0, 255),
        7: (255, 255, 255),
    }
    if r + g + b < 30:
        return 7
    best, min_dist = 7, float('inf')
    for idx, (cr, cg, cb) in colors.items():
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
        for path in page.get_drawings():
            name = path.get('layer')
            if name:
                layer_mgr.create_pdf_layer(f"PDF_{name}")

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
        return (float(point.x), float(page.rect.height - point.y + y_offset))

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

    def _add_polyline(self, msp, points, attribs, tolerance=1.0, close=None):
        is_closed = len(points) > 2 and _is_close(points[0], points[-1], tolerance)
        if close is not None:
            is_closed = close
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

            stroke_color = _fitz_color_to_rgb_int(path.get("color"))
            if self.preserve_colors and stroke_color:
                base_attribs["color"] = _rgb_to_dxf_color(stroke_color)

            width = path.get("width", 0)
            if width > 0:
                base_attribs["lineweight"] = _linewidth_mm_to_dxf(
                    width * self.STROKE_WIDTH_SCALE)

            has_fill = path.get("fill") is not None
            has_stroke = path.get("color") is not None

            # 填充 → HATCH（AutoCAD行为：有fill时只输出HATCH，不输出描边）
            if has_fill:
                self._emit_hatch(page, path, msp, base_attribs, y_offset)

            # 仅描边（无填充） → LWPOLYLINE / ARC / CIRCLE
            elif has_stroke:
                self._emit_stroke(page, path, msp, base_attribs,
                                  page_height, y_offset)

    # ─── 填充 → HATCH ───

    def _emit_hatch(self, page, path, msp, attribs, y_offset):
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
                self._create_hatch(msp, pts_d, path, attribs)
            return
        if item_types <= {'qu'} and len(items) == 1:
            pts = self._transform_quad(page, items[0][1], y_offset)
            pts_d = self._dedupe_points(pts, 0.25)
            if len(pts_d) >= 3:
                self._create_hatch(msp, pts_d, path, attribs)
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
            if self._try_thick_polyline(unique, msp, path, attribs):
                return

        # 普通碎片填充 → EdgePath+LineEdge HATCH
        cx = sum(p[0] for p in unique) / len(unique)
        cy = sum(p[1] for p in unique) / len(unique)
        ordered = sorted(unique, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
        self._create_hatch_edge(msp, ordered, path, attribs)

    def _try_thick_polyline(self, points, msp, path, attribs):
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
        fill_color = _fitz_color_to_rgb_int(path.get("fill"))
        if self.preserve_colors and fill_color:
            ha["color"] = _rgb_to_dxf_color(fill_color)
        ha.pop("lineweight", None)
        poly = msp.add_lwpolyline(centers, close=True, dxfattribs=ha)
        # 设置每个顶点的线宽（start_width = end_width = 对应角点间距）
        pts_data = []
        for i, (c, w) in enumerate(zip(centers, widths)):
            pts_data.append((c[0], c[1], w, w, 0))
        poly.set_points(pts_data, format='xyseb')
        poly.close()
        return True

    def _create_hatch_edge(self, msp, points, path, attribs):
        """创建 SOLID HATCH - 用EdgePath+LineEdge（与AutoCAD一致）"""
        if len(points) < 3:
            return
        ha = dict(attribs)
        fill_color = _fitz_color_to_rgb_int(path.get("fill"))
        if self.preserve_colors and fill_color:
            ha["color"] = _rgb_to_dxf_color(fill_color)
        ha.pop("lineweight", None)
        hatch = msp.add_hatch(dxfattribs=ha)
        edge_path = hatch.paths.add_edge_path()
        for i in range(len(points)):
            p1 = points[i]
            p2 = points[(i + 1) % len(points)]
            edge_path.add_line(p1, p2)

    def _create_hatch(self, msp, points, path, attribs):
        """创建 SOLID HATCH 实体（polyline边界）"""
        ha = dict(attribs)
        fill_color = _fitz_color_to_rgb_int(path.get("fill"))
        if self.preserve_colors and fill_color:
            ha["color"] = _rgb_to_dxf_color(fill_color)
        ha.pop("lineweight", None)
        hatch = msp.add_hatch(dxfattribs=ha)
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
                    self._add_polyline(msp, line_pts, attribs, tolerance=0.5)
                    line_pts = [s, e]

            elif cmd == 'c':
                # 遇到曲线 → 先刷新线段链
                if line_pts:
                    self._add_polyline(msp, line_pts, attribs, tolerance=0.5)
                    line_pts = []

                c0 = self._transform_point(page, item[1].x, item[1].y, y_offset)
                c1 = self._transform_point(page, item[2].x, item[2].y, y_offset)
                c2 = self._transform_point(page, item[3].x, item[3].y, y_offset)
                c3 = self._transform_point(page, item[4].x, item[4].y, y_offset)
                curves.append((c0, c1, c2, c3))

            elif cmd == 're':
                if line_pts:
                    self._add_polyline(msp, line_pts, attribs, tolerance=0.5)
                    line_pts = []
                if curves:
                    self._flush_curves(curves, msp, attribs, page_height, y_offset)
                    curves = []
                self._add_polyline(msp, self._transform_rect(page, item[1], y_offset), attribs)

            elif cmd == 'qu':
                if line_pts:
                    self._add_polyline(msp, line_pts, attribs, tolerance=0.5)
                    line_pts = []
                if curves:
                    self._flush_curves(curves, msp, attribs, page_height, y_offset)
                    curves = []
                self._add_polyline(msp, self._transform_quad(page, item[1], y_offset), attribs)

        # 刷新尾部
        if line_pts:
            self._add_polyline(msp, line_pts, attribs, tolerance=0.5)
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
                    x, y = self._transform_point(page, bbox[0], bbox[1], y_offset)

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

                    text_attribs = layer_mgr.get_dxf_attribs("text", page_num, extra)
                    mtext = msp.add_mtext(text, dxfattribs=text_attribs)
                    mtext.set_location(insert=(x, y - font_size))

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
    progress_callback=None,
) -> str:
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
