"""图层管理模块 - 管理DXF图层创建和分配"""

import ezdxf
from config import LAYER_STRATEGY_NONE, LAYER_STRATEGY_CONTENT, LAYER_STRATEGY_PAGE, LAYER_STRATEGY_PDF


# DXF 标准颜色索引
COLOR_WHITE = 7
COLOR_RED = 1
COLOR_YELLOW = 2
COLOR_GREEN = 3
COLOR_CYAN = 4
COLOR_BLUE = 5
COLOR_MAGENTA = 6

# 图层配置: (图层名前缀, 颜色索引)
CONTENT_LAYERS = {
    "vector": ("矢量图形", COLOR_WHITE),
    "text": ("文字", COLOR_CYAN),
    "image": ("图片", COLOR_GREEN),
}

# 每页颜色循环
PAGE_COLORS = [COLOR_WHITE, COLOR_RED, COLOR_YELLOW, COLOR_GREEN,
               COLOR_CYAN, COLOR_BLUE, COLOR_MAGENTA]


class LayerManager:
    """管理DXF图层创建和分配"""

    def __init__(self, doc: ezdxf.document.Drawing, strategy: str = LAYER_STRATEGY_NONE):
        self.doc = doc
        self.strategy = strategy
        self._created_layers: set = set()

    def _ensure_layer(self, name: str, color: int = COLOR_WHITE):
        """确保图层存在"""
        if name not in self._created_layers:
            try:
                self.doc.layers.add(name, color=color)
            except ezdxf.DXFTableEntryError:
                pass  # 图层已存在
            self._created_layers.add(name)

    def get_layer_name(self, content_type: str, page_num: int) -> str:
        """
        获取图层名

        Args:
            content_type: "vector" | "text" | "image"
            page_num: 页码(0-based)

        Returns:
            图层名称
        """
        if self.strategy == LAYER_STRATEGY_NONE:
            return "0"  # 默认图层

        elif self.strategy == LAYER_STRATEGY_CONTENT:
            prefix, color = CONTENT_LAYERS.get(
                content_type, ("其他", COLOR_WHITE))
            layer_name = prefix
            self._ensure_layer(layer_name, color)
            return layer_name

        elif self.strategy == LAYER_STRATEGY_PAGE:
            layer_name = f"页{page_num + 1}"
            color = PAGE_COLORS[page_num % len(PAGE_COLORS)]
            self._ensure_layer(layer_name, color)
            return layer_name

        elif self.strategy == LAYER_STRATEGY_PDF:
            return "0"

        return "0"

    def get_dxf_attribs(self, content_type: str, page_num: int,
                        extra: dict = None) -> dict:
        """
        获取完整的DXF实体属性字典

        Args:
            content_type: 内容类型
            page_num: 页码
            extra: 额外属性(如insert, height等)

        Returns:
            属性字典
        """
        attribs = {"layer": self.get_layer_name(content_type, page_num)}
        if extra:
            attribs.update(extra)
        return attribs

    def create_pdf_layer(self, name: str, color: int = COLOR_WHITE):
        """根据PDF OCG名称创建DXF图层"""
        self._ensure_layer(name, color)
