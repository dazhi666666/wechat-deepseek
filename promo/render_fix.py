# -*- coding: utf-8 -*-
"""渲染宣传片受影响的帧区间,供拼接替换用。

用法:
  python render_fix.py --range 984-1252 --out seg1.mp4          # 壁纸背景(v13 风格)
  python render_fix.py --range 984-1252 --out seg1_plain.mp4 --plainbg   # 旧纯色背景(v12 风格)
"""
import argparse
import os
import subprocess
import sys

FILM_DIR = r'C:\Users\user\AppData\Local\Temp\claude\D--wechat-deepseek\3e6ea8e8-e796-400f-8f07-af9aadbe4030\scratchpad\film'
sys.path.insert(0, FILM_DIR)


def make_plain_bg():
    """还原 v12 的旧版背景:纯色底 + 光斑 + 点阵(不走 bg_wall 壁纸)。"""
    import numpy as np
    from PIL import Image, ImageDraw
    film = sys.modules['film']

    def make_bg(blobs, base=film.NAVY, size=(2200, 1240)):
        w, h = size
        im = Image.new('RGBA', size, film.hexc(base))
        for x, y, r, c, a in blobs:
            g = film.radial_pil(int(r * 2), c, a, 1.6)
            im.alpha_composite(g, (int(x * w - r), int(y * h - r)))
        d = ImageDraw.Draw(im)
        for yy in range(0, h, 44):
            for xx in range(0, w, 44):
                d.ellipse((xx - 1, yy - 1, xx + 1, yy + 1), fill=(120, 150, 220, 26))
        return im

    film.make_bg = make_bg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--range', required=True, help='帧区间 a-b (含头不含尾)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--plainbg', action='store_true', help='用旧版纯色背景(v12 风格)')
    args = ap.parse_args()

    import film
    if args.plainbg:
        make_plain_bg()

    a, b = map(int, args.range.split('-'))
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    p = subprocess.Popen([ff, '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                          '-s', f'{film.W}x{film.H}', '-r', str(film.FPS), '-i', '-',
                          '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p', args.out],
                         stdin=subprocess.PIPE)
    import time
    t0 = time.time()
    for fi in range(a, b):
        p.stdin.write(film.render(fi).tobytes())
        if (fi - a) % 30 == 0:
            print(f'{fi}/{b} {time.time() - t0:.0f}s', flush=True)
    p.stdin.close()
    p.wait()
    print('done', args.out)


if __name__ == '__main__':
    main()
