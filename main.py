"""PDF2DXF - PDF转DXF桌面转换工具"""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# 确保能找到同目录模块
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import (
    APP_NAME, APP_VERSION, TRIAL_DAYS,
    LAYER_STRATEGY_NONE, LAYER_STRATEGY_CONTENT, LAYER_STRATEGY_PAGE,
    LAYER_STRATEGY_PDF,
    CURVE_MODE_SPLINE, CURVE_MODE_POLYLINE, CURVE_MODE_LINE,
    DXF_VERSIONS,
)
from converter import PdfToDxfConverter
from trial_guard import check_trial


# ─── 主题色 ───
BG_COLOR = "#f5f5f5"
CARD_BG = "#ffffff"
PRIMARY = "#c54b20"
PRIMARY_HOVER = "#a83e18"
TEXT_PRIMARY = "#333333"
TEXT_SECONDARY = "#666666"
BORDER_COLOR = "#e0e0e0"
SUCCESS_COLOR = "#52c41a"
WARNING_COLOR = "#faad14"
DANGER_COLOR = "#ff4d4f"


class FileListItem(tk.Frame):
    """文件列表项"""

    def __init__(self, parent, filepath: str, on_remove):
        super().__init__(parent, bg=CARD_BG, padx=10, pady=6)
        self.filepath = filepath
        filename = os.path.basename(filepath)
        size_mb = os.path.getsize(filepath) / 1024 / 1024

        tk.Label(self, text="📄", font=("Segoe UI Emoji", 12),
                 bg=CARD_BG).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(self, text=filename, font=("Microsoft YaHei", 10),
                 fg=TEXT_PRIMARY, bg=CARD_BG, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(self, text=f"{size_mb:.1f} MB", font=("Microsoft YaHei", 9),
                 fg=TEXT_SECONDARY, bg=CARD_BG).pack(side=tk.LEFT, padx=10)
        remove_btn = tk.Button(
            self, text="✕", font=("Segoe UI", 9), fg=DANGER_COLOR,
            bg=CARD_BG, relief="flat", cursor="hand2", bd=0,
            command=lambda: on_remove(self))
        remove_btn.pack(side=tk.RIGHT)


class App(tk.Tk):
    """主应用窗口"""

    def __init__(self):
        super().__init__()

        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("720x680")
        self.minsize(600, 580)
        self.configure(bg=BG_COLOR)

        # 加载logo/icon
        self._set_icon()

        # 试用期检查
        self.trial_valid, self.trial_remaining, self.trial_msg = check_trial()

        # 文件列表
        self.file_items: list[FileListItem] = []
        self._converter: PdfToDxfConverter | None = None
        self._converting = False

        self._build_ui()

    def _set_icon(self):
        """设置窗口图标"""
        try:
            logo_path = BASE_DIR / "logo.png"
            if logo_path.exists():
                from PIL import Image, ImageTk
                img = Image.open(logo_path)
                img = img.resize((32, 32), Image.LANCZOS)
                self._icon_photo = ImageTk.PhotoImage(img)
                self.iconphoto(True, self._icon_photo)

                # 保留大logo供界面使用
                logo_large = Image.open(logo_path).resize((48, 48), Image.LANCZOS)
                self._logo_large = ImageTk.PhotoImage(logo_large)
        except Exception:
            self._logo_large = None

    def _build_ui(self):
        """构建界面"""
        # ─── 顶部标题栏 ───
        header = tk.Frame(self, bg=PRIMARY, height=56)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        header_inner = tk.Frame(header, bg=PRIMARY)
        header_inner.pack(fill=tk.BOTH, expand=True, padx=16)

        if hasattr(self, '_logo_large') and self._logo_large:
            tk.Label(header_inner, image=self._logo_large,
                     bg=PRIMARY).pack(side=tk.LEFT, padx=(0, 10), pady=4)

        tk.Label(header_inner, text=APP_NAME,
                 font=("Microsoft YaHei", 16, "bold"),
                 fg="white", bg=PRIMARY).pack(side=tk.LEFT)
        tk.Label(header_inner, text=f"v{APP_VERSION}",
                 font=("Microsoft YaHei", 9),
                 fg="#ffccbb", bg=PRIMARY).pack(side=tk.LEFT, padx=(6, 0), pady=(6, 0))

        # 试用期状态
        trial_color = SUCCESS_COLOR if self.trial_remaining > 7 else (
            WARNING_COLOR if self.trial_remaining > 0 else DANGER_COLOR)
        tk.Label(header_inner,
                 text=f"试用期剩余 {self.trial_remaining} 天" if self.trial_valid
                 else "试用期已到期",
                 font=("Microsoft YaHei", 9),
                 fg=trial_color, bg=PRIMARY).pack(side=tk.RIGHT, pady=4)

        # ─── 主体内容 ───
        main_frame = tk.Frame(self, bg=BG_COLOR)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        # ── 文件选择区 ──
        file_section = tk.LabelFrame(
            main_frame, text=" 文件选择 ", font=("Microsoft YaHei", 10, "bold"),
            fg=TEXT_PRIMARY, bg=CARD_BG, bd=1, relief="solid",
            labelanchor="nw", padx=12, pady=8)
        file_section.pack(fill=tk.X, pady=(0, 12))

        btn_row = tk.Frame(file_section, bg=CARD_BG)
        btn_row.pack(fill=tk.X, pady=(0, 8))

        self._make_button(btn_row, "📁 选择PDF文件", self._select_files).pack(
            side=tk.LEFT, padx=(0, 8))
        self._make_button(btn_row, "📂 批量添加", self._select_folder).pack(
            side=tk.LEFT)

        # 文件列表容器
        self.file_list_frame = tk.Frame(file_section, bg=CARD_BG)
        self.file_list_frame.pack(fill=tk.X)

        self.file_count_label = tk.Label(
            file_section, text="未选择文件",
            font=("Microsoft YaHei", 9), fg=TEXT_SECONDARY, bg=CARD_BG)
        self.file_count_label.pack(anchor="w", pady=(4, 0))

        # ── 转换设置 ──
        settings_section = tk.LabelFrame(
            main_frame, text=" 转换设置 ", font=("Microsoft YaHei", 10, "bold"),
            fg=TEXT_PRIMARY, bg=CARD_BG, bd=1, relief="solid",
            labelanchor="nw", padx=12, pady=8)
        settings_section.pack(fill=tk.X, pady=(0, 12))

        settings_grid = tk.Frame(settings_section, bg=CARD_BG)
        settings_grid.pack(fill=tk.X)

        # 页码范围
        row = 0
        tk.Label(settings_grid, text="页码范围:", font=("Microsoft YaHei", 9),
                 fg=TEXT_PRIMARY, bg=CARD_BG).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.page_range_var = tk.StringVar(value="")
        page_entry = tk.Entry(settings_grid, textvariable=self.page_range_var,
                              width=20, font=("Microsoft YaHei", 9))
        page_entry.grid(row=row, column=1, sticky="w", pady=4)
        tk.Label(settings_grid, text="留空=全部，格式如: 1-5,8,10",
                 font=("Microsoft YaHei", 8), fg=TEXT_SECONDARY,
                 bg=CARD_BG).grid(row=row, column=2, sticky="w", padx=8)

        # 曲线精度
        row = 1
        tk.Label(settings_grid, text="曲线精度:", font=("Microsoft YaHei", 9),
                 fg=TEXT_PRIMARY, bg=CARD_BG).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.curve_mode_var = tk.StringVar(value=CURVE_MODE_SPLINE)
        curve_combo = ttk.Combobox(
            settings_grid, textvariable=self.curve_mode_var, width=18,
            values=[
                f"{CURVE_MODE_SPLINE}  (样条曲线-精确)",
                f"{CURVE_MODE_POLYLINE}  (多段线-中等)",
                f"{CURVE_MODE_LINE}  (直线-最快)",
            ], state="readonly", font=("Microsoft YaHei", 9))
        curve_combo.current(0)
        curve_combo.grid(row=row, column=1, sticky="w", pady=4, columnspan=2)

        # 图层策略
        row = 2
        tk.Label(settings_grid, text="图层策略:", font=("Microsoft YaHei", 9),
                 fg=TEXT_PRIMARY, bg=CARD_BG).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.layer_var = tk.StringVar(value=LAYER_STRATEGY_PDF)
        layer_combo = ttk.Combobox(
            settings_grid, textvariable=self.layer_var, width=18,
            values=[
                f"{LAYER_STRATEGY_NONE}  (不分层)",
                f"{LAYER_STRATEGY_CONTENT}  (按内容类型)",
                f"{LAYER_STRATEGY_PAGE}  (按页码)",
                f"{LAYER_STRATEGY_PDF}  (使用PDF图层)",
            ], state="readonly", font=("Microsoft YaHei", 9))
        layer_combo.current(3)
        layer_combo.grid(row=row, column=1, sticky="w", pady=4, columnspan=2)

        # DXF版本
        row = 3
        tk.Label(settings_grid, text="DXF版本:", font=("Microsoft YaHei", 9),
                 fg=TEXT_PRIMARY, bg=CARD_BG).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.dxf_version_var = tk.StringVar(value="R2018")
        version_combo = ttk.Combobox(
            settings_grid, textvariable=self.dxf_version_var, width=18,
            values=list(DXF_VERSIONS.keys()),
            state="readonly", font=("Microsoft YaHei", 9))
        version_combo.current(0)
        version_combo.grid(row=row, column=1, sticky="w", pady=4, columnspan=2)

        # 复选框
        checkbox_row = tk.Frame(settings_section, bg=CARD_BG)
        checkbox_row.pack(fill=tk.X, pady=(8, 0))

        self.colors_var = tk.BooleanVar(value=True)
        tk.Checkbutton(checkbox_row, text="保留颜色和线宽",
                       variable=self.colors_var,
                       font=("Microsoft YaHei", 9), bg=CARD_BG,
                       fg=TEXT_PRIMARY, activebackground=CARD_BG).pack(
            side=tk.LEFT, padx=(0, 20))

        self.images_var = tk.BooleanVar(value=False)
        tk.Checkbutton(checkbox_row, text="提取嵌入图片",
                       variable=self.images_var,
                       font=("Microsoft YaHei", 9), bg=CARD_BG,
                       fg=TEXT_PRIMARY, activebackground=CARD_BG).pack(
            side=tk.LEFT)

        # ── 转换按钮 ──
        btn_frame = tk.Frame(main_frame, bg=BG_COLOR)
        btn_frame.pack(fill=tk.X, pady=(0, 12))

        self.convert_btn = tk.Button(
            btn_frame, text="▶  开始转换", font=("Microsoft YaHei", 13, "bold"),
            fg="white", bg=PRIMARY, activebackground=PRIMARY_HOVER,
            relief="flat", cursor="hand2", bd=0, padx=30, pady=10,
            command=self._start_convert)
        self.convert_btn.pack(fill=tk.X, ipady=4)
        self.convert_btn.bind("<Enter>",
                              lambda e: self.convert_btn.config(bg=PRIMARY_HOVER))
        self.convert_btn.bind("<Leave>",
                              lambda e: self.convert_btn.config(bg=PRIMARY))

        # ── 进度区 ──
        progress_section = tk.Frame(main_frame, bg=CARD_BG, bd=1,
                                    relief="solid", padx=12, pady=10)
        progress_section.pack(fill=tk.X)

        self.progress_label = tk.Label(
            progress_section, text="就绪",
            font=("Microsoft YaHei", 9), fg=TEXT_SECONDARY, bg=CARD_BG,
            anchor="w")
        self.progress_label.pack(fill=tk.X, pady=(0, 6))

        self.progress_bar = ttk.Progressbar(
            progress_section, mode='determinate', length=400)
        self.progress_bar.pack(fill=tk.X)

        # 如果试用期已过期，禁用转换
        if not self.trial_valid:
            self.convert_btn.config(state="disabled", bg="#cccccc")
            self.progress_label.config(text=self.trial_msg, fg=DANGER_COLOR)

    def _make_button(self, parent, text, command):
        """创建样式统一的按钮"""
        btn = tk.Button(
            parent, text=text, font=("Microsoft YaHei", 9),
            fg=TEXT_PRIMARY, bg="#f0f0f0", activebackground="#e0e0e0",
            relief="flat", cursor="hand2", bd=0, padx=12, pady=4,
            command=command)
        btn.bind("<Enter>", lambda e: btn.config(bg="#e0e0e0"))
        btn.bind("<Leave>", lambda e: btn.config(bg="#f0f0f0"))
        return btn

    def _select_files(self):
        """选择PDF文件"""
        filepaths = filedialog.askopenfilenames(
            title="选择PDF文件",
            filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")])
        for fp in filepaths:
            self._add_file(fp)

    def _select_folder(self):
        """批量选择文件夹内的PDF"""
        folder = filedialog.askdirectory(title="选择包含PDF文件的文件夹")
        if folder:
            for f in Path(folder).glob("*.pdf"):
                self._add_file(str(f))

    def _add_file(self, filepath: str):
        """添加文件到列表"""
        # 去重
        for item in self.file_items:
            if item.filepath == filepath:
                return

        item = FileListItem(self.file_list_frame, filepath, self._remove_file)
        item.pack(fill=tk.X, pady=1)
        self.file_items.append(item)
        self._update_file_count()

    def _remove_file(self, item: FileListItem):
        """移除文件"""
        item.pack_forget()
        item.destroy()
        self.file_items.remove(item)
        self._update_file_count()

    def _update_file_count(self):
        n = len(self.file_items)
        if n == 0:
            self.file_count_label.config(text="未选择文件")
        else:
            total_size = sum(
                os.path.getsize(it.filepath) for it in self.file_items
            ) / 1024 / 1024
            self.file_count_label.config(
                text=f"已添加 {n} 个文件，共 {total_size:.1f} MB")

    def _get_curve_mode(self) -> str:
        val = self.curve_mode_var.get()
        return val.split(" ")[0].strip()

    def _get_layer_strategy(self) -> str:
        val = self.layer_var.get()
        return val.split(" ")[0].strip()

    def _start_convert(self):
        """开始转换"""
        if not self.trial_valid:
            messagebox.showerror("试用期已到期", self.trial_msg)
            return

        if not self.file_items:
            messagebox.showwarning("提示", "请先选择PDF文件")
            return

        if self._converting:
            return

        # 选择输出目录
        output_dir = filedialog.askdirectory(title="选择输出目录")
        if not output_dir:
            return

        self._converting = True
        self.convert_btn.config(state="disabled", text="转换中...", bg="#999999")

        # 在后台线程执行转换
        thread = threading.Thread(
            target=self._do_convert, args=(output_dir,), daemon=True)
        thread.start()

    def _do_convert(self, output_dir: str):
        """后台转换任务"""
        total_files = len(self.file_items)
        success_count = 0
        fail_count = 0

        for file_idx, item in enumerate(self.file_items):
            try:
                input_path = item.filepath
                filename = Path(input_path).stem
                output_path = os.path.join(output_dir, f"{filename}.dxf")

                # 避免覆盖
                counter = 1
                while os.path.exists(output_path):
                    output_path = os.path.join(
                        output_dir, f"{filename}_{counter}.dxf")
                    counter += 1

                def progress_cb(current, total, msg):
                    overall = (file_idx * 100 + (current / max(total, 1)) * 100) / total_files
                    self.after(0, self._update_progress,
                              overall,
                              f"[{file_idx + 1}/{total_files}] {msg}")

                converter = PdfToDxfConverter(
                    curve_mode=self._get_curve_mode(),
                    layer_strategy=self._get_layer_strategy(),
                    dxf_version=self.dxf_version_var.get(),
                    preserve_colors=self.colors_var.get(),
                    extract_images=self.images_var.get(),
                    page_range=self.page_range_var.get() or None,
                )
                converter.set_progress_callback(progress_cb)
                converter.convert(input_path, output_path)
                success_count += 1

            except Exception as e:
                fail_count += 1
                self.after(0, self._update_progress, 0,
                           f"转换失败: {Path(item.filepath).name} - {str(e)}")

        # 完成
        self.after(0, self._convert_done, success_count, fail_count, output_dir)

    def _update_progress(self, value: float, text: str):
        """更新进度"""
        self.progress_bar['value'] = value
        self.progress_label.config(text=text, fg=TEXT_PRIMARY)

    def _convert_done(self, success: int, fail: int, output_dir: str):
        """转换完成"""
        self._converting = False
        self.convert_btn.config(
            state="normal", text="▶  开始转换", bg=PRIMARY)
        self.progress_bar['value'] = 100

        if fail == 0:
            self.progress_label.config(
                text=f"全部完成！成功转换 {success} 个文件",
                fg=SUCCESS_COLOR)
            messagebox.showinfo(
                "转换完成",
                f"成功转换 {success} 个文件\n输出目录: {output_dir}")
        else:
            self.progress_label.config(
                text=f"完成。成功 {success} 个，失败 {fail} 个",
                fg=WARNING_COLOR)
            messagebox.showwarning(
                "转换完成",
                f"成功 {success} 个，失败 {fail} 个\n输出目录: {output_dir}")

        # 打开输出目录
        if sys.platform == "win32":
            os.startfile(output_dir)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
