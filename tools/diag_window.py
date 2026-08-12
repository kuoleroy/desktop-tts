# -*- coding: utf-8 -*-
"""诊断：打开窗口后 evaluate_js 读取页面状态"""
import os
import sys
import time

os.chdir(r"E:\kuoleroy\desktop-tts")
sys.path.insert(0, os.getcwd())

import pet3d

srv, port = pet3d.start_server()
print("server", port)

import webview

class DiagApi(pet3d.Api):
    pass

win = None

def on_loaded():
    time.sleep(3)
    js = (
        "JSON.stringify({"
        "errs: (window.__errs||[]).slice(0,8),"
        "status: (document.getElementById('status')||{}).textContent||'',"
        "bodyBg: getComputedStyle(document.body).backgroundColor,"
        "hasCanvas: !!document.getElementById('stage'),"
        "vrmReady: !!window.__vrmReady,"
        "model: window.__currentModel||''"
        "})"
    )
    try:
        r = win.evaluate_js(js)
        print("JS STATE:", r)
    except Exception as e:
        print("eval fail:", repr(e))
    time.sleep(1)
    os._exit(0)

win = webview.create_window(
    "Diag",
    "http://127.0.0.1:%d/index.html" % port,
    width=420, height=560,
    transparent=True, frameless=True,
    on_top=False, easy_drag=False,
    js_api=DiagApi(),
)
webview.start(on_loaded, debug=False)