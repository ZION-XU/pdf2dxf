# PDF2DXF 配置常量

APP_NAME = "PDF2DXF"
APP_VERSION = "1.0.0"

# 试用期天数
TRIAL_DAYS = 30

# NTP 服务器列表（按优先级）
NTP_SERVERS = [
    "ntp.aliyun.com",
    "ntp.tencent.com",
    "cn.ntp.org.cn",
    "time.windows.com",
]

# 本地缓存文件名
TRIAL_DATA_FILE = ".pdf2dxf_trial"

# DXF 版本选项
DXF_VERSIONS = {
    "R2018": "R2018",
    "R2013": "R2013",
    "R2010": "R2010",
    "R2007": "R2007",
    "R2004": "R2004",
    "R2000": "R2000",
    "R12": "R12",
}

# 图层策略
LAYER_STRATEGY_NONE = "none"           # 不分图层
LAYER_STRATEGY_CONTENT = "content"     # 按内容类型(矢量/文字/图片)
LAYER_STRATEGY_PAGE = "page"           # 按页码
LAYER_STRATEGY_PDF = "pdf"             # 使用PDF原生图层(OCG)

# 曲线精度
CURVE_MODE_SPLINE = "spline"           # 样条曲线(精确)
CURVE_MODE_POLYLINE = "polyline"       # 多段线近似
CURVE_MODE_LINE = "line"               # 直线简化(最快)

# 中文字体
DEFAULT_FONT = "宋体"
