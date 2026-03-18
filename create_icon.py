"""生成ico图标文件"""
from PIL import Image
import os

def create_icon():
    logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
    ico_path = os.path.join(os.path.dirname(__file__), "icon.ico")

    img = Image.open(logo_path)
    # 生成多尺寸ico
    img.save(ico_path, format='ICO',
             sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"图标已生成: {ico_path}")

if __name__ == "__main__":
    create_icon()
