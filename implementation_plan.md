# 按AutoCAD方式重构PDF转DXF输出

## AutoCAD分析结论

通过分析 `Drawing1.dxf`（AutoCAD从同一PDF导出），发现如下关键模式：

| 方面 | AutoCAD做法 | 我们当前做法 |
|------|------------|------------|
| **主力实体** | LWPOLYLINE (3041个) | LINE + HATCH |
| **粗线表示** | LWPOLYLINE + lineweight属性 | HATCH填充（已移除） |
| **LINE实体** | **0个**（完全不用LINE） | 大量使用 |
| **填充区域** | HATCH SOLID模式（1088个，单path） | 多子路径HATCH |
| **图层前缀** | `PDF_` + 原PDF图层名 | 直接使用原名 |
| **颜色** | 256 (ByLayer) | 逐实体指定 |
| **WALL图层** | LWPOLYLINE lw=40, 无HATCH | 混合LINE+HATCH |

> [!IMPORTANT]
> AutoCAD **完全不使用 LINE 实体**，所有线段都用 LWPOLYLINE。
> 粗细通过 lineweight 控制，不用 const_width。
> HATCH 仅用于纯填充区域（如柱子阴影、标注箭头等），不用来表示粗线。

## Proposed Changes

### 矢量输出重构

#### [MODIFY] [converter.py](file:///g:/0ai/pdf2dxf/converter.py)

1. **将所有 LINE 输出改为 LWPOLYLINE**：连续线段合并为一条 LWPOLYLINE（已有 [_try_add_connected_polyline](file:///g:/0ai/pdf2dxf/converter.py#367-384)），单独线段也用 2点 LWPOLYLINE 代替 LINE
2. **fill 路径用 HATCH（单path）**：仅对有 fill 的路径创建 HATCH，用原始路径顺序收集边界点（单个闭合多边形），不拆分子路径
3. **图层名加 `PDF_` 前缀**

## Verification Plan

### Manual Verification
- 用户手动运行 `python main.py`，转换同一 PDF，在 AutoCAD 中打开结果
- 验证：1) 粗线矩形是完整的LWPOLYLINE/HATCH 2) 图层名带PDF_前缀 3) 移动边不会出现重叠双线
