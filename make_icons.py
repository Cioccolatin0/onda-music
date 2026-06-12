"""Generate Onda app icons (gradient + sound wave) for PWA/iOS."""
from PIL import Image, ImageDraw

def make_icon(size):
    img = Image.new("RGB", (size, size))
    px = img.load()
    # diagonal gradient: deep indigo -> violet -> hot pink
    c1 = (88, 28, 135)   # purple-900
    c2 = (236, 72, 153)  # pink-500
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

    d = ImageDraw.Draw(img)
    # sound wave bars
    bars = [0.30, 0.55, 0.80, 0.55, 0.30]
    n = len(bars)
    bw = size * 0.085
    gap = size * 0.06
    total = n * bw + (n - 1) * gap
    x0 = (size - total) / 2
    cy = size / 2
    for i, h in enumerate(bars):
        bh = size * h * 0.62
        x = x0 + i * (bw + gap)
        d.rounded_rectangle([x, cy - bh / 2, x + bw, cy + bh / 2], radius=bw / 2, fill=(255, 255, 255))
    return img

for s in (180, 192, 512):
    make_icon(s).save(f"/home/ubuntu/musicapp/static/icons/icon-{s}.png")
print("icons done")
