# -*- coding: utf-8 -*-
"""离线渲染 Live2D Cubism 模型为透明 GIF 皮肤。
用法: python tools/render_live2d.py <model3.json> <out.gif> [帧数]
动作组默认取 'CAT_motion', 可改下面对应变量。依赖: live2d-py glfw pyopengl pillow
"""
import os
import sys
import glfw
from OpenGL import GL
from PIL import Image
import live2d.v3 as live2d

W = H = 800
MOTION_GROUP = 'CAT_motion'
FRAMES = 40
DURATION_MS = 100


def main(model3, out, frames):
    glfw.init()
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glfw.window_hint(glfw.ALPHA_BITS, 8)
    win = glfw.create_window(W, H, 'x', None, None)
    glfw.make_context_current(win)
    live2d.init()
    live2d.glInit()
    m = live2d.LAppModel()
    m.LoadModelJson(os.path.abspath(model3))
    m.Resize(W, H)
    m.StartRandomMotion(MOTION_GROUP, 2)
    imgs = []
    for _ in range(frames):
        live2d.clearBuffer(0, 0, 0, 0)
        m.Update()
        m.Draw()
        buf = GL.glReadPixels(0, 0, W, H, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        imgs.append(Image.frombytes('RGBA', (W, H), buf, 'raw', 'RGBA', 0, -1))
    imgs[0].save(out, save_all=True, append_images=imgs[1:],
                 duration=DURATION_MS, loop=0, disposal=2)
    print('saved %d frames -> %s' % (len(imgs), out))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else FRAMES)